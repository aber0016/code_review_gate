"""mypy runner — Layer 1: type errors and hallucinated APIs (§4.2 of the plan).

The highest-ROI hallucination catcher (wrong attributes/signatures). All
errors are `severity: error` — do not soften. Findings are filtered to
changed *files*, not lines: type errors *caused* by the diff may surface
elsewhere in a changed file (§2 rationale).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

_TEXT_LINE_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::\d+)?: (?P<kind>error|warning|note): "
    r"(?P<message>.*?)(?:\s+\[(?P<code>[a-z0-9-]+)\])?$"
)


def _finding(file: str, line: int, message: str, code: str) -> Finding:
    return Finding(
        id=make_id("mypy", code, file, line),
        layer=Layer.STATIC,
        tool="mypy",
        severity=Severity.ERROR,
        action=Action.AUTO_FIX,
        file=file,
        line=line,
        message=message,
        evidence=code,
    )


def parse_json_output(stdout: str, changed_files: set[Path]) -> list[Finding]:
    """Parse `mypy --output json` (one JSON object per line)."""
    findings: list[Finding] = []
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        entry = json.loads(raw)
        if entry.get("severity") != "error":
            continue
        file = str(entry.get("file", ""))
        if Path(file) not in changed_files:
            continue
        findings.append(
            _finding(
                file=file,
                line=int(entry.get("line", 0)),
                message=str(entry.get("message", "")),
                code=str(entry.get("code") or "error"),
            )
        )
    return findings


def parse_text_output(stdout: str, changed_files: set[Path]) -> list[Finding]:
    """Fallback parser for mypy versions without `--output json`."""
    findings: list[Finding] = []
    for raw in stdout.splitlines():
        match = _TEXT_LINE_RE.match(raw.strip())
        if match is None or match.group("kind") != "error":
            continue
        file = match.group("file")
        if Path(file) not in changed_files:
            continue
        findings.append(
            _finding(
                file=file,
                line=int(match.group("line")),
                message=match.group("message"),
                code=match.group("code") or "error",
            )
        )
    return findings


class MypyRunner:
    """Runs mypy on changed .py files, filtered to changed files."""

    name = "mypy"
    layer = Layer.STATIC
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """mypy binary present (target venv first, then PATH)."""
        return self.ctx.find_tool("mypy") is not None

    def build_argv(self, files: list[Path], json_output: bool = True) -> list[str]:
        """Argv for the mypy invocation.

        ``--cache-dir=/dev/null`` disables the incremental cache so the
        runner never mutates the working tree (§2 hard rule).
        """
        tool = self.ctx.find_tool("mypy") or "mypy"
        argv = [
            tool,
            "--no-error-summary",
            "--no-color-output",
            "--no-pretty",
            "--show-error-codes",
            "--cache-dir=/dev/null",
        ]
        if json_output:
            argv += ["--output", "json"]
        argv += self.ctx.config.runner_args(self.name)
        argv += [str(f) for f in files]
        return argv

    def run(self, ctx: RunContext) -> list[Finding]:
        """Type-check changed files; keep errors located in changed files."""
        files = ctx.changed_py_files
        if not files:
            return []
        changed = set(files)
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        result = ctx.run_cmd(self.build_argv(files), timeout=timeout)
        if result.timed_out:
            return [timeout_finding(self.name, self.layer, timeout)]
        if result.code == 2 and "--output" in result.stderr:
            # Installed mypy predates `--output json`: fall back to text.
            result = ctx.run_cmd(
                self.build_argv(files, json_output=False), timeout=timeout
            )
            if result.timed_out:
                return [timeout_finding(self.name, self.layer, timeout)]
            findings = parse_text_output(result.stdout, changed)
        else:
            findings = parse_json_output(result.stdout, changed)
        if result.code not in (0, 1) and not findings:
            raise RuntimeError(
                f"mypy exited {result.code}: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )
        return findings
