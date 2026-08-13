"""pytest + diff-cover runner — Layer 3: exec tier (§5.1 of the plan).

Runs the target repo's tests with coverage, then gates on **diff coverage**
only (never global coverage). Artifacts are written to a temp dir, never the
repo root. Optionally executes inside the docker sandbox (§5.2).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding
from gauntlet.sandbox import DockerSandbox, Sandbox, select_sandbox

_EVIDENCE_LIMIT = 2_000


def parse_junit(junit_xml: Path) -> list[Finding]:
    """Failed tests → error/auto-fix; collection/import errors → error/ask-user."""
    findings: list[Finding] = []
    try:
        # nosec B314: junit_xml is written by our own pytest subprocess into a
        # temp dir we created; gauntlet core is stdlib-only (no defusedxml),
        # and the exec tier's threat model already executes the repo's code.
        tree = ET.parse(junit_xml)  # nosec
    except (ET.ParseError, OSError):
        return findings
    for case in tree.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        node_id = f"{classname}::{name}" if classname else name
        file = case.get("file", "") or ""
        line = int(case.get("line", 0) or 0)
        for kind, action, code in (
            ("failure", Action.AUTO_FIX, "failed"),
            ("error", Action.ASK_USER, "error"),
        ):
            for element in case.findall(kind):
                message = element.get("message", "") or f"test {kind}"
                findings.append(
                    Finding(
                        id=make_id("pytest", code, file, node_id),
                        layer=Layer.EXEC,
                        tool="pytest",
                        severity=Severity.ERROR,
                        action=action,
                        file=file,
                        line=line,
                        message=f"{node_id}: {message[:300]}",
                        evidence=(element.text or "")[:_EVIDENCE_LIMIT],
                        fix_hint=(
                            "Fix src/ so this test passes (tests are locked in "
                            "fix rounds)."
                            if kind == "failure"
                            else "Collection/import error — the diff likely broke "
                            "the package layout; needs a human look."
                        ),
                    )
                )
    return findings


def coverage_info_finding(pct: float) -> Finding:
    """Machine-readable diff-coverage stat (info/no-op; evidence = the pct).

    The pi extension's test-author validation (§9.2.3) reads this to check
    that generated tests *strictly increase* diff coverage.
    """
    return Finding(
        id=make_id("diff-cover", "coverage", ""),
        layer=Layer.EXEC,
        tool="diff-cover",
        severity=Severity.INFO,
        action=Action.NO_OP,
        file="",
        line=0,
        message=f"diff coverage: {pct:.1f}%",
        evidence=f"{pct}",
    )


def remap_coverage_sources(cov_xml: Path, from_prefix: str, to_prefix: str) -> None:
    """Translate container source paths in coverage.xml back to host paths.

    The docker sandbox mounts the repo at ``/work``; coverage records that
    prefix in ``<source>`` elements, which the host-side diff-cover cannot
    map to the repo — every changed line then reads "unmeasurable" and the
    gate would silently pass. The mount defines the translation exactly.
    """
    # nosec B314: file produced by our own pytest run in a dir we created.
    tree = ET.parse(cov_xml)  # nosec
    changed = False
    for source in tree.iter("source"):
        text = source.text or ""
        if text == from_prefix or text.startswith(f"{from_prefix}/"):
            source.text = to_prefix + text[len(from_prefix) :]
            changed = True
    if changed:
        tree.write(cov_xml)


def parse_diff_cover(dc_json: Path, threshold: float) -> tuple[float, list[Finding]]:
    """Diff-coverage percentage + the below-threshold finding, if any."""
    doc = json.loads(dc_json.read_text())
    pct = float(doc.get("total_percent_covered", 0.0))
    if pct >= threshold:
        return pct, []
    uncovered: list[str] = []
    for path, stats in sorted(doc.get("src_stats", {}).items()):
        lines = stats.get("violation_lines", [])
        if lines:
            uncovered.append(f"{path}:{','.join(str(n) for n in lines)}")
    return pct, [
        Finding(
            id=make_id("diff-cover", "below-threshold", ""),
            layer=Layer.EXEC,
            tool="diff-cover",
            severity=Severity.ERROR,
            action=Action.AUTO_FIX,
            file="",
            line=0,
            message=(
                f"diff coverage {pct:.1f}% is below the required "
                f"{threshold:.0f}% — uncovered changed lines: "
                + ("; ".join(uncovered) or "(none reported)")
            ),
            evidence="; ".join(uncovered)[:_EVIDENCE_LIMIT],
            fix_hint="add tests covering the uncovered changed lines",
        )
    ]


class PytestCovRunner:
    """Runs pytest with coverage, then gates on diff coverage."""

    name = "pytest"
    layer = Layer.EXEC
    tier = "exec"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """pytest + pytest-cov importable in the target interpreter.

        In docker mode the container provides them via ``.[test]``, so only
        the docker CLI matters (its absence degrades inside ``run``).
        """
        if self.ctx.config.sandbox == "docker":
            return True
        probe = self.ctx.run_cmd(
            [str(self.ctx.repo_python()), "-c", "import pytest, pytest_cov"],
            timeout=20,
        )
        return probe.code == 0

    def build_pytest_args(self, cov_xml: Path, junit_xml: Path) -> list[str]:
        """The pytest argument list (backend-independent)."""
        args = ["-q"]
        for src in self.ctx.config.src_paths:
            if (self.ctx.root / src).exists():
                args.append(f"--cov={src}")
        args += [
            f"--cov-report=xml:{cov_xml}",
            f"--junitxml={junit_xml}",
        ]
        args += self.ctx.config.runner_args(self.name)
        return args

    def build_diff_cover_argv(self, cov_xml: Path, dc_json: Path) -> list[str]:
        """Argv for the host-side diff-cover invocation."""
        tool = self.ctx.find_tool("diff-cover") or "diff-cover"
        return [
            tool,
            str(cov_xml),
            f"--compare-branch={self.ctx.merge_base}",
            "--json-report",
            str(dc_json),
        ]

    def _make_tmp(self, sandbox: Sandbox) -> tuple[Path, Path]:
        """(host tmp dir, path-as-seen-by-pytest). Never the repo root.

        Docker can only write inside the mount, so its artifacts live under
        ``.gauntlet/`` (git-ignored) and are removed afterwards.
        """
        if isinstance(sandbox, DockerSandbox):
            rel = Path(".gauntlet") / f"exec-tmp-{uuid.uuid4().hex[:8]}"
            host = self.ctx.root / rel
            host.mkdir(parents=True, exist_ok=True)
            return host, rel
        host = Path(tempfile.mkdtemp(prefix="gauntlet-exec-"))
        return host, host

    def run(self, ctx: RunContext) -> list[Finding]:
        """Execute tests (+ coverage), then the diff-coverage gate."""
        sandbox, findings = select_sandbox(ctx)
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        tmp_host, tmp_for_pytest = self._make_tmp(sandbox)
        try:
            cov_xml = tmp_host / "coverage.xml"
            junit_xml = tmp_host / "junit.xml"
            pytest_args = self.build_pytest_args(
                tmp_for_pytest / "coverage.xml", tmp_for_pytest / "junit.xml"
            )
            env = {"COVERAGE_FILE": str(tmp_for_pytest / ".coverage")}
            argv = sandbox.build_pytest_argv(ctx, pytest_args, env=env)
            result = ctx.run_cmd(argv, timeout=timeout, env=env)
            if result.timed_out:
                return [*findings, timeout_finding(self.name, self.layer, timeout)]

            test_findings = parse_junit(junit_xml)
            findings.extend(test_findings)
            if result.code not in (0, 1, 5) and not test_findings:
                raise RuntimeError(
                    f"pytest exited {result.code}: "
                    f"{(result.stderr or result.stdout).strip()[:800]}"
                )

            if cov_xml.is_file() and isinstance(sandbox, DockerSandbox):
                remap_coverage_sources(cov_xml, "/work", str(ctx.root))
            if not cov_xml.is_file():
                findings.append(
                    Finding(
                        id=make_id("pytest", "no-coverage", ""),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.ERROR,
                        action=Action.AUTO_FIX,
                        file="",
                        line=0,
                        message=(
                            "pytest produced no coverage data "
                            "(no tests collected, or pytest-cov missing)"
                        ),
                        evidence=(result.stdout or "")[-_EVIDENCE_LIMIT:],
                        fix_hint="add tests exercising the changed lines",
                    )
                )
                return findings

            findings.extend(self._diff_cover(ctx, cov_xml, tmp_host, timeout))
            if ctx.verify_fails_on:
                findings.extend(
                    self._verify_fails_on(ctx, ctx.verify_fails_on, timeout)
                )
            return findings
        finally:
            shutil.rmtree(tmp_host, ignore_errors=True)

    def _changed_test_files(self, ctx: RunContext) -> list[Path]:
        """Changed .py files under the configured test paths."""
        return [
            f
            for f in ctx.changed_py_files
            if any(f == Path(t) or f.is_relative_to(t) for t in ctx.config.test_paths)
        ]

    def build_worktree_pytest_argv(
        self, worktree: Path, test_files: list[Path]
    ) -> list[str]:
        """Argv that runs only the new tests inside the base worktree."""
        return [
            str(self.ctx.repo_python()),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *[str(worktree / f) for f in test_files],
        ]

    def _verify_fails_on(
        self, ctx: RunContext, ref: str, timeout: float
    ) -> list[Finding]:
        """Fail-before-fix discipline (§9.2.4, misguidance-effect guard).

        Check out ``ref`` into a temp worktree, copy the new/changed test
        files in, and run only them there: at least one must FAIL on the
        pre-fix code (they already passed on the current tree in the main
        pytest run above).
        """
        test_files = self._changed_test_files(ctx)
        if not test_files:
            return [
                Finding(
                    id=make_id("pytest", "no-new-tests", ""),
                    layer=self.layer,
                    tool=self.name,
                    severity=Severity.ERROR,
                    action=Action.ASK_USER,
                    file="",
                    line=0,
                    message=(
                        "--verify-fails-on: no new/changed tests found — the fix "
                        "is not demonstrated by any test (misguidance-effect guard)"
                    ),
                )
            ]
        worktree = Path(tempfile.mkdtemp(prefix="gauntlet-worktree-"))
        added = ctx.run_cmd(
            ["git", "worktree", "add", "--detach", str(worktree), ref],
            timeout=60,
        )
        try:
            if added.code != 0:
                raise RuntimeError(
                    f"git worktree add failed: {added.stderr.strip()[:300]}"
                )
            for file in test_files:
                target = worktree / file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((ctx.root / file).read_text(errors="replace"))
            env = {
                "PYTHONPATH": os.pathsep.join(
                    [
                        *(
                            str(worktree / src)
                            for src in ctx.config.src_paths
                            if (worktree / src).is_dir()
                        ),
                        str(worktree),
                    ]
                ),
                "COVERAGE_FILE": str(worktree / ".coverage"),
            }
            result = ctx.run_cmd(
                self.build_worktree_pytest_argv(worktree, test_files),
                cwd=worktree,
                timeout=timeout,
                env=env,
            )
            if result.timed_out:
                return [timeout_finding(self.name, self.layer, timeout)]
            if result.code == 0:
                return [
                    Finding(
                        id=make_id("pytest", "fails-on-base", "", ref),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.ERROR,
                        action=Action.ASK_USER,
                        file="",
                        line=0,
                        message=(
                            f"fix is not demonstrated by any test: the new tests "
                            f"also PASS on {ref} (misguidance-effect guard)"
                        ),
                        evidence=result.stdout[-_EVIDENCE_LIMIT:],
                        fix_hint=(
                            "write a test that fails on the pre-fix code and "
                            "passes on the fixed code"
                        ),
                    )
                ]
            return [
                Finding(
                    id=make_id("pytest", "fails-on-base-ok", "", ref),
                    layer=self.layer,
                    tool=self.name,
                    severity=Severity.INFO,
                    action=Action.NO_OP,
                    file="",
                    line=0,
                    message=(
                        f"fail-before-fix verified: new tests fail on {ref} "
                        "and pass on the current tree"
                    ),
                )
            ]
        finally:
            ctx.run_cmd(
                ["git", "worktree", "remove", "--force", str(worktree)],
                timeout=60,
            )
            shutil.rmtree(worktree, ignore_errors=True)

    def _diff_cover(
        self, ctx: RunContext, cov_xml: Path, tmp: Path, timeout: float
    ) -> list[Finding]:
        if ctx.find_tool("diff-cover") is None:
            return [
                Finding(
                    id=make_id("diff-cover", "not-installed", ""),
                    layer=self.layer,
                    tool="diff-cover",
                    severity=Severity.INFO,
                    action=Action.NO_OP,
                    file="",
                    line=0,
                    message="diff-cover not installed; diff-coverage gate skipped",
                )
            ]
        dc_json = tmp / "dc.json"
        result = ctx.run_cmd(
            self.build_diff_cover_argv(cov_xml, dc_json), timeout=timeout
        )
        if result.timed_out:
            return [timeout_finding("diff-cover", self.layer, timeout)]
        if not dc_json.is_file():
            raise RuntimeError(
                f"diff-cover exited {result.code} without a JSON report: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )
        pct, dc_findings = parse_diff_cover(dc_json, ctx.config.diff_cover_min)
        return [coverage_info_finding(pct), *dc_findings]
