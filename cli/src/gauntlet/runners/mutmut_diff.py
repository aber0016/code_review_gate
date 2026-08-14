"""mutmut runner — Layer 5: mutation testing, deep tier (§10 of the plan).

Mechanical proof that the tests detect injected bugs — scoped to changed
files only (full-repo mutation is prohibitively slow). Runs in a temp copy
of the repo so neither the generated ``setup.cfg`` nor mutmut's ``mutants/``
working directory ever touches the target tree.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import tempfile
from pathlib import Path

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

_RESULT_LINE_RE = re.compile(r"^\s*(?P<name>[\w.]+):\s*(?P<status>\w+)\s*$")
_HUNK_RE = re.compile(r"^@@ -(?P<start>\d+)")
_EVIDENCE_LIMIT = 2_000


def parse_results(stdout: str) -> list[str]:
    """Names of surviving mutants from `mutmut results` output."""
    survivors: list[str] = []
    for line in stdout.splitlines():
        match = _RESULT_LINE_RE.match(line)
        if match and match.group("status") == "survived":
            survivors.append(match.group("name"))
    return survivors


def mutant_module(name: str) -> str:
    """``pkg.mod.x_func__mutmut_3`` → ``pkg.mod``."""
    parts = name.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else name


def module_to_file(module: str, src_paths: list[str], root: Path) -> str:
    """Best-effort map of a module path back to a repo-relative file."""
    rel = Path(*module.split("."))
    for src in src_paths:
        candidate = Path(src) / rel.with_suffix(".py")
        if (root / candidate).is_file():
            return str(candidate)
    candidate = rel.with_suffix(".py")
    return str(candidate) if (root / candidate).is_file() else ""


def diff_first_changed_line(diff: str) -> int:
    """Old-file line number of the first '-' line in a unified diff."""
    old_line = 0
    for line in diff.splitlines():
        hunk = _HUNK_RE.match(line)
        if hunk:
            old_line = int(hunk.group("start"))
            continue
        if old_line == 0:
            continue
        if line.startswith("-") and not line.startswith("---"):
            return old_line
        if line.startswith("+") and not line.startswith("+++"):
            continue
        old_line += 1
    return 0


def survivor_finding(
    name: str, diff: str, file: str, snippet: str, blocking: bool
) -> Finding:
    """The finding for one surviving mutant.

    ``blocking`` follows ``deep.blocking`` (and the survivor budget): when it
    is off, the finding must be a no-op — warning+ask-user would still trip
    ``Finding.blocking()`` and redden headless runs regardless of the config.
    """
    return Finding(
        id=make_id("mutmut", "survived", file, name),
        layer=Layer.MUTATION,
        tool="mutmut",
        severity=Severity.ERROR if blocking else Severity.WARNING,
        action=Action.ASK_USER if blocking else Action.NO_OP,
        file=file,
        line=diff_first_changed_line(diff),
        message=(
            f"surviving mutant {name}: the test suite does not "
            f"detect this injected bug ({snippet or 'see evidence'})"
        ),
        evidence=diff[:_EVIDENCE_LIMIT],
        fix_hint="add a test that kills this mutant",
    )


def render_setup_cfg(
    existing: str | None, src_paths: list[str], only_mutate: list[str]
) -> str:
    """A setup.cfg whose [mutmut] section scopes mutation to the diff."""
    parser = configparser.ConfigParser()
    if existing:
        parser.read_string(existing)
    parser.remove_section("mutmut")
    parser.add_section("mutmut")
    parser.set("mutmut", "source_paths", "\n".join(src_paths))
    parser.set("mutmut", "only_mutate", "\n".join(only_mutate))
    from io import StringIO

    out = StringIO()
    parser.write(out)
    return out.getvalue()


class MutmutDiffRunner:
    """Mutates changed src files and reports surviving mutants."""

    name = "mutmut"
    layer = Layer.MUTATION
    tier = "deep"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """mutmut binary present (target venv first, then PATH)."""
        return self.ctx.find_tool("mutmut") is not None

    def build_argv(self, command: str, extra: list[str] | None = None) -> list[str]:
        """Argv for a mutmut subcommand."""
        tool = self.ctx.find_tool("mutmut") or "mutmut"
        return [tool, command, *(extra or [])]

    def _changed_src_files(self, ctx: RunContext) -> list[Path]:
        return [
            f
            for f in ctx.changed_py_files
            if any(f.is_relative_to(src) for src in ctx.config.src_paths)
        ]

    def _make_workdir(self, ctx: RunContext, changed: list[Path]) -> Path:
        """Copy the mutation-relevant tree into a temp dir with our config."""
        workdir = Path(tempfile.mkdtemp(prefix="gauntlet-mutmut-"))
        for src in ctx.config.src_paths:
            src_dir = ctx.root / src
            if src_dir.is_dir():
                shutil.copytree(src_dir, workdir / src, dirs_exist_ok=True)
        for entry in ctx.config.test_paths:
            source = ctx.root / entry
            if source.is_dir():
                shutil.copytree(source, workdir / entry, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, workdir / entry)
        for manifest in ("pyproject.toml", "setup.py", "conftest.py"):
            source = ctx.root / manifest
            if source.is_file():
                shutil.copy2(source, workdir / manifest)
        existing_cfg = ctx.root / "setup.cfg"
        (workdir / "setup.cfg").write_text(
            render_setup_cfg(
                existing_cfg.read_text() if existing_cfg.is_file() else None,
                ctx.config.src_paths,
                [str(f) for f in changed],
            )
        )
        return workdir

    def run(self, ctx: RunContext) -> list[Finding]:
        """Mutate changed src files; each surviving mutant is a finding."""
        if os.name != "posix":
            return [
                Finding(
                    id=make_id(self.name, "non-posix", ""),
                    layer=self.layer,
                    tool=self.name,
                    severity=Severity.INFO,
                    action=Action.NO_OP,
                    file="",
                    line=0,
                    message=(
                        "deep tier requires Linux/macOS/WSL (mutmut >= 3 needs fork())"
                    ),
                )
            ]
        changed = self._changed_src_files(ctx)
        if not changed:
            return []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        workdir = self._make_workdir(ctx, changed)
        try:
            run_result = ctx.run_cmd(
                self.build_argv("run"), cwd=workdir, timeout=timeout
            )
            if run_result.timed_out:
                return [timeout_finding(self.name, self.layer, timeout)]
            results = ctx.run_cmd(self.build_argv("results"), cwd=workdir, timeout=120)
            if results.code != 0:
                raise RuntimeError(
                    f"mutmut results exited {results.code}: "
                    f"{(results.stderr or run_result.stderr).strip()[:500]}"
                )
            survivors = parse_results(results.stdout)
            blocking = (
                ctx.config.deep_blocking
                and len(survivors) > ctx.config.deep_max_survivors
            )
            findings: list[Finding] = []
            for name in survivors:
                show = ctx.run_cmd(
                    self.build_argv("show", [name]), cwd=workdir, timeout=60
                )
                diff = show.stdout.strip()
                file = module_to_file(
                    mutant_module(name), ctx.config.src_paths, ctx.root
                )
                snippet = "; ".join(
                    line
                    for line in diff.splitlines()
                    if (line.startswith("-") or line.startswith("+"))
                    and not line.startswith(("---", "+++"))
                )[:200]
                findings.append(survivor_finding(name, diff, file, snippet, blocking))
            return findings
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
