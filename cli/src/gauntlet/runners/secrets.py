"""secrets runner — Layer 0: committed credentials (§4.4 of the plan).

Prefers `detect-secrets scan`; falls back to a built-in regex scanner for the
classic high-signal patterns. Never prints the matched secret — findings name
the file, line, and pattern only.

Opt-in PII scan (``[runners.secrets] pii = true``): personal-data patterns
(emails, IBANs, German social-security/health-insurance numbers), each gated
by a checksum or domain validator so synthetic fixture data stays green. Off
by default — fixture emails are ubiquitous, and an always-on blocking PII
check would only train users to dismiss.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
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

# RFC-2606 reserved names plus the conventional "example.*" second-level label:
# addresses on these domains are synthetic by definition.
_SYNTHETIC_TLDS = {"test", "example", "invalid", "localhost"}


def _real_domain(match: str) -> bool:
    domain = match.rsplit("@", 1)[-1].lower()
    labels = domain.split(".")
    return not (
        domain == "localhost"
        or labels[-1] in _SYNTHETIC_TLDS
        or (len(labels) >= 2 and labels[-2] == "example")
    )


def _iban_mod97_ok(match: str) -> bool:
    rearranged = match[4:] + match[:4]
    digits = "".join(
        str(int(ch, 36))
        for ch in rearranged  # A→10 … Z→35, digits unchanged
    )
    return int(digits) % 97 == 1


def _digit_sum_mod10(digits: str, weights: list[int]) -> int:
    total = 0
    for ch, weight in zip(digits, weights, strict=True):
        product = int(ch) * weight
        total += sum(int(d) for d in str(product))
    return total % 10


def _svnr_check_digit_ok(match: str) -> bool:
    """German Rentenversicherungsnummer: letter→2 digits, weighted digit sums."""
    digits = match[:8] + f"{ord(match[8]) - ord('A') + 1:02d}" + match[9:11]
    weights = [2, 1, 2, 5, 7, 1, 2, 1, 2, 1, 2, 1]
    return _digit_sum_mod10(digits, weights) == int(match[11])


def _kvnr_check_digit_ok(match: str) -> bool:
    """German Krankenversichertennummer: letter→2 digits, weights 1,2,1,2…

    The bare shape (letter + 9 digits) is far too generic to ship unguarded;
    this pattern exists only because the check digit gates it.
    """
    digits = f"{ord(match[0]) - ord('A') + 1:02d}" + match[1:9]
    weights = [1, 2] * 5
    return _digit_sum_mod10(digits, weights) == int(match[9])


#: PII patterns (opt-in): regex plus a validator that must also accept the
#: match. The validator is what keeps fixture/test data from flagging.
PII_PATTERNS: dict[str, tuple[re.Pattern[str], Callable[[str], bool]]] = {
    "pii-email": (
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
        _real_domain,
    ),
    "pii-iban": (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), _iban_mod97_ok),
    "pii-de-svnr": (
        re.compile(r"\b\d{2}[0-3]\d[01]\d\d{2}[A-Z]\d{3}\b"),
        _svnr_check_digit_ok,
    ),
    "pii-de-kvnr": (re.compile(r"\b[A-Z]\d{9}\b"), _kvnr_check_digit_ok),
}


def _finding(
    pattern_name: str,
    file: str,
    line: int,
    *,
    severity: Severity = Severity.ERROR,
    kind: str = "secret",
) -> Finding:
    return Finding(
        id=make_id("secrets", pattern_name, file, line),
        layer=Layer.PRECOMMIT,
        tool="secrets",
        severity=severity,
        action=Action.ASK_USER,
        file=file,
        line=line,
        message=f"potential {kind} detected ({pattern_name})",
        evidence=pattern_name,
        fix_hint=(
            "If real: rotate the credential and move it to env/secret storage. "
            "If a false positive: dismiss via the gate."
        )
        if kind == "secret"
        else (
            "If real personal data: remove it and use synthetic fixtures "
            "(user@example.com, Max Mustermann). If synthetic: dismiss via "
            "the gate."
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


def scan_pii(root: Path, changed_lines: dict[Path, set[int]]) -> list[Finding]:
    """Validator-gated PII scan over the changed lines of changed files."""
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
            for name, (pattern, valid) in PII_PATTERNS.items():
                if any(valid(match.group(0)) for match in pattern.finditer(text)):
                    findings.append(
                        _finding(
                            name,
                            str(file),
                            lineno,
                            severity=Severity.WARNING,
                            kind="personal data",
                        )
                    )
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
        findings: list[Finding] | None = None
        if ctx.find_tool("detect-secrets") is not None:
            result = ctx.run_cmd(self.build_argv(files), timeout=timeout)
            if result.timed_out:
                return [timeout_finding(self.name, self.layer, timeout)]
            if result.code == 0:
                findings = parse_detect_secrets_output(result.stdout, ctx.changed_lines)
            # detect-secrets misbehaved: fall back rather than going blind.
        if findings is None:
            findings = scan_fallback(ctx.root, ctx.changed_lines)
        # detect-secrets has no PII analyzers, so the PII pass runs either way.
        if bool(ctx.config.runner_option(self.name, "pii", False)):
            findings += scan_pii(ctx.root, ctx.changed_lines)
        return findings
