"""lockfile discipline runner — Layer 2 (§4.6 of the plan).

For every project directory touched by the diff (plus the repo root):
a stale ``uv.lock`` is an error with a mechanical fix; a changed
``pyproject.toml`` without any lockfile is an ask-user warning.
"""

from __future__ import annotations

from pathlib import Path

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding


class LockfileRunner:
    """Verifies uv.lock freshness / presence for diff-touched projects."""

    name = "lockfile"
    layer = Layer.SUPPLY_CHAIN
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: presence checks need no tool; uv degradation
        is reported from ``run`` with an accurate message."""
        return True

    def build_argv(self, check: bool = True) -> list[str]:
        """Argv for uv lock verification (or the explicit --fix refresh)."""
        tool = self.ctx.find_tool("uv") or "uv"
        argv = [tool, "lock"]
        if check:
            argv.append("--check")
        argv += self.ctx.config.runner_args(self.name)
        return argv

    def _project_dirs(self, ctx: RunContext) -> list[Path]:
        """Repo root plus parents of changed dependency manifests."""
        dirs = {Path()}
        for file in ctx.changed_files:
            if file.name in ("pyproject.toml", "uv.lock"):
                dirs.add(file.parent)
        return sorted(dirs)

    def run(self, ctx: RunContext) -> list[Finding]:
        """Check lockfile freshness/presence per touched project directory."""
        findings: list[Finding] = []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        for rel_dir in self._project_dirs(ctx):
            project = ctx.root / rel_dir
            lock = project / "uv.lock"
            pyproject = project / "pyproject.toml"
            lock_rel = str(rel_dir / "uv.lock")
            if lock.is_file():
                if ctx.find_tool("uv") is None:
                    findings.append(
                        Finding(
                            id=make_id("lockfile", "uv-missing", lock_rel),
                            layer=self.layer,
                            tool=self.name,
                            severity=Severity.INFO,
                            action=Action.NO_OP,
                            file=lock_rel,
                            line=0,
                            message="uv not installed; lockfile check skipped",
                        )
                    )
                    continue
                findings.extend(self._check_freshness(ctx, project, lock_rel, timeout))
            elif pyproject.is_file() and rel_dir / "pyproject.toml" in set(
                ctx.changed_files
            ):
                findings.append(
                    Finding(
                        id=make_id("lockfile", "missing", lock_rel),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.WARNING,
                        action=Action.ASK_USER,
                        file=str(rel_dir / "pyproject.toml"),
                        line=0,
                        message=(
                            "pyproject.toml changed but no lockfile exists; "
                            "dependencies are unpinned"
                        ),
                        fix_hint=(
                            "adopt uv with hash pinning: `uv lock` and commit uv.lock"
                        ),
                    )
                )
        return findings

    def _check_freshness(
        self, ctx: RunContext, project: Path, lock_rel: str, timeout: float
    ) -> list[Finding]:
        result = ctx.run_cmd(self.build_argv(check=True), cwd=project, timeout=timeout)
        if result.timed_out:
            return [timeout_finding(self.name, self.layer, timeout)]
        if result.code == 0:
            return []
        if ctx.fix:
            # --fix is the sanctioned mutation: refresh the lockfile.
            refreshed = ctx.run_cmd(
                self.build_argv(check=False), cwd=project, timeout=timeout
            )
            if refreshed.code == 0:
                return [
                    Finding(
                        id=make_id("lockfile", "refreshed", lock_rel),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.INFO,
                        action=Action.NO_OP,
                        file=lock_rel,
                        line=0,
                        message="uv.lock was stale; refreshed by --fix (run `uv lock`)",
                    )
                ]
        return [
            Finding(
                id=make_id("lockfile", "stale", lock_rel),
                layer=self.layer,
                tool=self.name,
                severity=Severity.ERROR,
                action=Action.AUTO_FIX,
                file=lock_rel,
                line=0,
                message="uv.lock is stale (out of sync with pyproject.toml)",
                evidence=(result.stderr or result.stdout).strip()[:500],
                fix_hint="run uv lock",
            )
        ]
