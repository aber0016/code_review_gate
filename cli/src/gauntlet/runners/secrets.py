"""secrets runner — Layer 0: committed credentials (§4.4 of the plan).

Prefers `detect-secrets scan`; falls back to a built-in regex scanner for the
classic high-signal patterns. Never prints the matched secret — findings name
the file, line, and pattern only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

#: Fallback patterns (name → regex). Kept deliberately high-signal.
FALLBACK_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws-access-key-id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH) PRIVATE KEY-----"),
    "slack-token": re.compile(r"xox[baprs]-"),
    "generic-api-key": re.compile(
        r"(?i)(?:api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9/+=_-]{20,}"
    ),
}

_MAX_SCAN_BYTES = 1_000_000  # skip huge blobs; secrets live in small text files


def _finding(pattern_name: str, file: str, line: int) -> Finding:
    return Finding(
        id=make_id("secrets", pattern_name, file, line),
        layer=Layer.PRECOMMIT,
        tool="secrets",
        severity=Severity.ERROR,
        action=Action.ASK_USER,
        file=file,
        line=line,
        message=f"potential secret detected ({pattern_name})",
        evidence=pattern_name,
        fix_hint=(
            "If real: rotate the credential and move it to env/secret storage. "
            "If a false positive: dismiss via the gate."
        ),
    )


def parse_detect_secrets_output(
    stdout: str, changed_lines: dict[Path, set[int]]
) -> list[Finding]:
    """Map a `detect-secrets scan` baseline document to findings."""
    findings: list[Finding] = []
    doc: dict[str, Any] = json.loads(stdout or "{}")
    for file, hits in doc.get("results", {}).items():
        rel = str(file).removeprefix("./")
        lines = changed_lines.get(Path(rel))
        if lines is None:
            continue
        for hit in hits:
            line = int(hit.get("line_number", 0))
            if line not in lines:
                continue
            findings.append(_finding(str(hit.get("type", "unknown")), rel, line))
    return findings


def scan_fallback(root: Path, changed_lines: dict[Path, set[int]]) -> list[Finding]:
    """Built-in regex scan over the changed lines of changed files."""
    findings: list[Finding] = []
    for file, lines in sorted(changed_lines.items()):
        if not lines:
            continue
        full = root / file
        try:
            if full.stat().st_size > _MAX_SCAN_BYTES:
                continue
            content = full.read_text(errors="replace")
        except OSError:
            continue
        for lineno, text in enumerate(content.splitlines(), start=1):
            if lineno not in lines:
                continue
            for name, pattern in FALLBACK_PATTERNS.items():
                if pattern.search(text):
                    findings.append(_finding(name, str(file), lineno))
    return findings


class SecretsRunner:
    """Scans changed files for committed credentials."""

    name = "secrets"
    layer = Layer.PRECOMMIT
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: the regex fallback needs no external tool."""
        return True

    def build_argv(self, files: list[Path]) -> list[str]:
        """Argv for the detect-secrets engine."""
        tool = self.ctx.find_tool("detect-secrets") or "detect-secrets"
        argv = [tool, "scan"]
        argv += self.ctx.config.runner_args(self.name)
        argv += [str(f) for f in files]
        return argv

    def run(self, ctx: RunContext) -> list[Finding]:
        """Scan changed files; findings filtered to changed lines."""
        files = [f for f in ctx.changed_files if ctx.changed_lines.get(f)]
        if not files:
            return []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        if ctx.find_tool("detect-secrets") is not None:
            result = ctx.run_cmd(self.build_argv(files), timeout=timeout)
            if result.timed_out:
                return [timeout_finding(self.name, self.layer, timeout)]
            if result.code == 0:
                return parse_detect_secrets_output(result.stdout, ctx.changed_lines)
            # detect-secrets misbehaved: fall back rather than going blind.
        return scan_fallback(ctx.root, ctx.changed_lines)
