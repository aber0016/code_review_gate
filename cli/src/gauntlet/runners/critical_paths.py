"""critical-paths runner — Layer 0: the blast-radius red-list.

Oracle quality alone cannot clear a high-blast-radius change: a clean diff on
an auth or migration path still needs a human. Any changed (or deleted) file
matching a ``critical_paths`` pattern parks one mandatory ask-user finding —
a policy trigger, not a defect, so it is a warning that only a human can
dismiss. Headless runs (CI, pre-push) therefore always block on it.
"""

from __future__ import annotations

from gauntlet import gitdiff
from gauntlet.config import matches_any
from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext

_FIX_HINT = (
    "A human must approve this change (gate dialog, or a reviewed PR in CI). "
    "Trim critical_paths in .gauntlet.toml if the pattern is too broad."
)


def _finding(code: str, file: str, pattern: str) -> Finding:
    return Finding(
        id=make_id("critical-paths", code, file, pattern),
        layer=Layer.PRECOMMIT,
        tool="critical-paths",
        severity=Severity.WARNING,
        action=Action.ASK_USER,
        file=file,
        line=0,
        message=(f"critical path {code} — human review required (matches {pattern!r})"),
        evidence=pattern,
        fix_hint=_FIX_HINT,
    )


class CriticalPathsRunner:
    """Parks a mandatory human-review finding per touched red-list file."""

    name = "critical-paths"
    layer = Layer.PRECOMMIT
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: pure config + diff scope, no external tool."""
        return True

    def run(self, ctx: RunContext) -> list[Finding]:
        """One finding per changed or deleted file on a critical path."""
        patterns = ctx.config.critical_paths
        if not patterns:
            return []
        findings: list[Finding] = []
        for file in ctx.changed_files:
            for pattern in patterns:
                if matches_any(file, [pattern]):
                    findings.append(_finding("touched", str(file), pattern))
                    break
        # Deletions never reach ctx.changed_files (a finding cannot anchor to
        # a missing file elsewhere, but deleting an auth module IS the blast
        # radius here), so pull them from name-status — exclusions included.
        for status, old_path, _new_path in gitdiff.name_status(ctx.root, ctx.base_ref):
            if status != "D" or matches_any(old_path, ctx.config.exclude_paths):
                continue
            for pattern in patterns:
                if matches_any(old_path, [pattern]):
                    findings.append(_finding("deleted", str(old_path), pattern))
                    break
        return findings
