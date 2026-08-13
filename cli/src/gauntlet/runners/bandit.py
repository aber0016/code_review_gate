"""bandit runner — Layer 1: insecure patterns (§4.3 of the plan).

Security decisions are never auto-fixed silently: everything above LOW is
`action: ask-user`; LOW is informational (`no-op`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

_SEVERITY_MAP = {
    "HIGH": Severity.ERROR,
    "MEDIUM": Severity.WARNING,
    "LOW": Severity.INFO,
}


def parse_output(stdout: str, changed_lines: dict[Path, set[int]]) -> list[Finding]:
    """Map bandit JSON results to findings, filtered to changed lines."""
    findings: list[Finding] = []
    doc: dict[str, Any] = json.loads(stdout or "{}")
    for issue in doc.get("results", []):
        file = str(issue.get("filename", "")).removeprefix("./")
        line = int(issue.get("line_number", 0))
        span = {int(n) for n in issue.get("line_range", [line])} or {line}
        lines = changed_lines.get(Path(file))
        if lines is None or not lines.intersection(span):
            continue
        severity = _SEVERITY_MAP.get(
            str(issue.get("issue_severity", "")).upper(), Severity.WARNING
        )
        action = Action.NO_OP if severity is Severity.INFO else Action.ASK_USER
        test_id = str(issue.get("test_id", "B000"))
        findings.append(
            Finding(
                id=make_id("bandit", test_id, file, line),
                layer=Layer.STATIC,
                tool="bandit",
                severity=severity,
                action=action,
                file=file,
                line=line,
                message=str(issue.get("issue_text", "")),
                evidence=(
                    f"{test_id} {issue.get('test_name', '')} "
                    f"(confidence: {issue.get('issue_confidence', '?')})"
                ),
                fix_hint=str(issue.get("more_info", "")),
            )
        )
    return findings


class BanditRunner:
    """Runs `bandit -f json -r` on changed .py files."""

    name = "bandit"
    layer = Layer.STATIC
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """bandit binary present (target venv first, then PATH)."""
        return self.ctx.find_tool("bandit") is not None

    def build_argv(self, files: list[Path]) -> list[str]:
        """Argv for the bandit invocation."""
        tool = self.ctx.find_tool("bandit") or "bandit"
        argv = [tool, "-f", "json", "-q", "-r"]
        argv += self.ctx.config.runner_args(self.name)
        argv += [str(f) for f in files]
        return argv

    def run(self, ctx: RunContext) -> list[Finding]:
        """Scan changed files; findings filtered to changed lines."""
        files = ctx.changed_py_files
        if not files:
            return []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        result = ctx.run_cmd(self.build_argv(files), timeout=timeout)
        if result.timed_out:
            return [timeout_finding(self.name, self.layer, timeout)]
        if result.code not in (0, 1):
            raise RuntimeError(
                f"bandit exited {result.code}: {result.stderr.strip()[:500]}"
            )
        return parse_output(result.stdout, ctx.changed_lines)
