"""Runner protocol, execution context, and the runner registry (§4 of the plan).

Every runner:

- scopes findings to the diff (changed lines, or changed files where the
  runner's spec section says so — mypy and imports),
- never mutates the working tree unless ``--fix`` is explicitly set,
- is timeout-bounded (a timeout is a warning/ask-user finding, never a hang),
- degrades visibly when its tool is missing (info finding, never a crash).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from gauntlet.config import Config
from gauntlet.findings import Action, Finding, Layer, Severity, make_id

TIERS = ("fast", "exec", "gen", "deep")

TIER_TIMEOUTS: dict[str, float] = {
    "fast": 120.0,
    "exec": 600.0,
    "gen": 120.0,
    "deep": 1800.0,
}


@dataclass
class CmdResult:
    """Outcome of one timeout-bounded subprocess invocation."""

    argv: list[str]
    stdout: str
    stderr: str
    code: int
    duration_s: float
    timed_out: bool = False

    def ok(self) -> bool:
        """Whether the process exited zero without timing out."""
        return self.code == 0 and not self.timed_out


@dataclass
class RunContext:
    """Everything a runner needs: repo, diff scope, config, and exec helper."""

    root: Path
    base_ref: str
    merge_base: str
    head: str
    changed_files: list[Path]
    changed_lines: dict[Path, set[int]]
    config: Config
    tier: str
    timeout: float
    fix: bool = False
    verify_fails_on: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def changed_py_files(self) -> list[Path]:
        """Changed files with a ``.py`` suffix (repo-relative)."""
        return [p for p in self.changed_files if p.suffix == ".py"]

    def run_cmd(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CmdResult:
        """Run a subprocess with captured output and an enforced timeout.

        Never raises for tool failures: timeouts and missing binaries are
        reported in the result so runners can convert them into findings.
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        full_env = None
        if env is not None or self.env_overrides:
            full_env = {**os.environ, **self.env_overrides, **(env or {})}
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd if cwd is not None else self.root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=effective_timeout,
                env=full_env,
            )
        except subprocess.TimeoutExpired as exc:
            return CmdResult(
                argv=list(argv),
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                code=-1,
                duration_s=time.monotonic() - start,
                timed_out=True,
            )
        except FileNotFoundError:
            return CmdResult(
                argv=list(argv),
                stdout="",
                stderr=f"{argv[0]}: executable not found",
                code=127,
                duration_s=time.monotonic() - start,
            )
        return CmdResult(
            argv=list(argv),
            stdout=proc.stdout,
            stderr=proc.stderr,
            code=proc.returncode,
            duration_s=time.monotonic() - start,
        )

    def venv_bin(self) -> Path | None:
        """The target repo's venv bin directory, if one can be located.

        Resolution order: ``<root>/.venv``, ``<root>/venv``, ``$VIRTUAL_ENV``.
        gauntlet invokes whatever the *target repo's* venv provides — never
        its own environment's tools.
        """
        for name in (".venv", "venv"):
            for bin_name in ("bin", "Scripts"):
                cand = self.root / name / bin_name
                if (cand / "python").exists() or (cand / "python.exe").exists():
                    return cand
        virtual_env = os.environ.get("VIRTUAL_ENV")
        if virtual_env:
            for bin_name in ("bin", "Scripts"):
                cand = Path(virtual_env) / bin_name
                if (cand / "python").exists() or (cand / "python.exe").exists():
                    return cand
        return None

    def repo_python(self) -> Path:
        """The target repo's Python interpreter (falls back to gauntlet's own)."""
        bin_dir = self.venv_bin()
        if bin_dir is not None:
            python = bin_dir / "python"
            return python if python.exists() else bin_dir / "python.exe"
        return Path(sys.executable)

    def find_tool(self, name: str) -> str | None:
        """Locate a tool binary: target venv first, then PATH."""
        bin_dir = self.venv_bin()
        if bin_dir is not None:
            for cand in (bin_dir / name, bin_dir / f"{name}.exe"):
                if cand.exists():
                    return str(cand)
        return shutil.which(name)


def _decode(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data


class Runner(Protocol):
    """The runner protocol from §4 of the plan.

    Concrete runners are constructed with the :class:`RunContext`, so
    ``available()`` may consult both tool presence and repo state.
    """

    name: str
    layer: Layer
    tier: str

    def available(self) -> bool:
        """Whether this runner can execute (tool installed, preconditions met)."""
        ...

    def run(self, ctx: RunContext) -> list[Finding]:
        """Execute and return diff-scoped findings."""
        ...


def timeout_finding(name: str, layer: Layer, timeout: float) -> Finding:
    """The §2-mandated finding for a runner that hit its timeout."""
    return Finding(
        id=make_id(name, "timeout", ""),
        layer=layer,
        tool=name,
        severity=Severity.WARNING,
        action=Action.ASK_USER,
        file="",
        line=0,
        message=f"{name} timed out after {timeout:.0f}s",
        fix_hint=(
            f"Re-run with a higher timeout via [runners.{name}] timeout, "
            "or investigate why the tool hangs."
        ),
    )


def skipped_finding(name: str, layer: Layer) -> Finding:
    """The §2-mandated finding for a missing tool: visible, never a crash."""
    return Finding(
        id=make_id(name, "not-installed", ""),
        layer=layer,
        tool=name,
        severity=Severity.INFO,
        action=Action.NO_OP,
        file="",
        line=0,
        message=f"{name} not installed; layer skipped",
        fix_hint=f"Install {name} in the target repo's venv to enable this layer.",
    )


def all_runners(ctx: RunContext) -> list[Runner]:
    """Instantiate every known runner against ``ctx`` (registry order).

    Remaining tiers (exec/gen/deep) join the registry in Phases 2/6/7.
    """
    from gauntlet.runners import (
        bandit,
        hypothesis_gen,
        imports,
        lockfile,
        mutmut_diff,
        mypy,
        pip_audit,
        pytest_cov,
        ruff,
        secrets,
    )

    return [
        ruff.RuffRunner(ctx),
        mypy.MypyRunner(ctx),
        bandit.BanditRunner(ctx),
        secrets.SecretsRunner(ctx),
        pip_audit.PipAuditRunner(ctx),
        lockfile.LockfileRunner(ctx),
        imports.ImportsRunner(ctx),
        pytest_cov.PytestCovRunner(ctx),
        hypothesis_gen.HypothesisGenRunner(ctx),
        mutmut_diff.MutmutDiffRunner(ctx),
    ]


def runners_for_tier(
    ctx: RunContext, tier: str, only: str | None = None
) -> list[Runner]:
    """Runners selected for a tier, honoring config and ``--runner``.

    ``--runner <name>`` selects by name across all tiers (needed for
    debugging and CI matrix jobs), still subject to ``enabled`` config.
    """
    candidates = all_runners(ctx)
    if only is not None:
        selected = [r for r in candidates if r.name == only]
    else:
        selected = [r for r in candidates if r.tier == tier]
    return [r for r in selected if ctx.config.runner_enabled(r.name)]
