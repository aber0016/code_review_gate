"""ruff runner — Layer 0/1: style and obvious bugs (§4.1 of the plan)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

#: Bug-class rule families promoted to `error` severity.
ERROR_PREFIXES = ("F", "B", "BLE")
ERROR_CODES = frozenset({"E722"})


def classify(code: str) -> Severity:
    """Severity for a ruff rule code: bug-class rules are errors."""
    if code in ERROR_CODES or code.startswith(ERROR_PREFIXES):
        return Severity.ERROR
    return Severity.WARNING


def parse_output(
    stdout: str, changed_lines: dict[Path, set[int]], root: Path
) -> list[Finding]:
    """Map ruff JSON diagnostics to findings, filtered to changed lines."""
    findings: list[Finding] = []
    diagnostics: list[dict[str, Any]] = json.loads(stdout or "[]")
    for diag in diagnostics:
        file = _relative(diag.get("filename", ""), root)
        code = diag.get("code") or "syntax-error"
        start = int(diag.get("location", {}).get("row", 0))
        end = int(diag.get("end_location", {}).get("row", start))
        lines = changed_lines.get(Path(file))
        if lines is None or not lines.intersection(range(start, end + 1)):
            continue
        severity = Severity.ERROR if code == "syntax-error" else classify(code)
        fixable = diag.get("fix") is not None
        if fixable:
            action = Action.AUTO_FIX
        elif severity is Severity.ERROR:
            action = Action.ASK_USER
        else:
            action = Action.NO_OP
        findings.append(
            Finding(
                id=make_id("ruff", code, file, start),
                layer=Layer.STATIC,
                tool="ruff",
                severity=severity,
                action=action,
                file=file,
                line=start,
                message=str(diag.get("message", "")),
                evidence=code,
                fix_hint=(diag.get("fix") or {}).get("message") or "",
            )
        )
    return findings


def _relative(filename: str, root: Path) -> str:
    path = Path(filename)
    if path.is_absolute():
        try:
            return str(path.relative_to(root))
        except ValueError:
            return filename
    return filename


class RuffRunner:
    """Runs `ruff check --output-format json` on changed .py files."""

    name = "ruff"
    layer = Layer.STATIC
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """ruff binary present (target venv first, then PATH)."""
        return self.ctx.find_tool("ruff") is not None

    def build_argv(self, files: list[Path], fix_only: bool = False) -> list[str]:
        """Argv for the check (or the explicit --fix-only mutation pass)."""
        tool = self.ctx.find_tool("ruff") or "ruff"
        argv = [tool, "check", "--force-exclude"]
        if fix_only:
            argv += ["--fix-only", "-q"]
        else:
            argv += ["--output-format", "json"]
        argv += self.ctx.config.runner_args(self.name)
        argv += [str(f) for f in files]
        return argv

    def run(self, ctx: RunContext) -> list[Finding]:
        """Check changed files; findings filtered to changed lines."""
        files = ctx.changed_py_files
        if not files:
            return []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        if ctx.fix:
            # --fix is the one sanctioned working-tree mutation (§2). Line
            # filtering afterwards uses the pre-fix diff scope; residual
            # drift is acceptable in an explicit fix pass.
            ctx.run_cmd(self.build_argv(files, fix_only=True), timeout=timeout)
        result = ctx.run_cmd(self.build_argv(files), timeout=timeout)
        if result.timed_out:
            return [timeout_finding(self.name, self.layer, timeout)]
        if result.code not in (0, 1):
            raise RuntimeError(
                f"ruff exited {result.code}: {result.stderr.strip()[:500]}"
            )
        return parse_output(result.stdout, ctx.changed_lines, ctx.root)
