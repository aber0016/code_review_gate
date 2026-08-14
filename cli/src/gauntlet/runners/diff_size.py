"""diff-size runner — Layer 0: oversized-diff advisory.

Loop length is an autonomy modifier: past a certain size every review layer
(human and machine) degrades. The finding is info/no-op — it never blocks and
never opens a dialog; it just puts the number in front of whoever reads the
report. ``[runners.diff-size] max_lines = 0`` (or ``enabled = false``) turns
it off.
"""

from __future__ import annotations

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext

DEFAULT_MAX_LINES = 500


class DiffSizeRunner:
    """Advises when the diff outgrows what one review pass can hold."""

    name = "diff-size"
    layer = Layer.PRECOMMIT
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: the diff scope is already computed."""
        return True

    def run(self, ctx: RunContext) -> list[Finding]:
        """Emit one advisory when new-side changed lines exceed the budget."""
        max_lines = int(
            ctx.config.runner_option(self.name, "max_lines", DEFAULT_MAX_LINES)
        )
        if max_lines <= 0:
            return []
        total = sum(len(lines) for lines in ctx.changed_lines.values())
        if total <= max_lines:
            return []
        return [
            Finding(
                id=make_id(self.name, "over-budget", ""),
                layer=self.layer,
                tool=self.name,
                severity=Severity.INFO,
                action=Action.NO_OP,
                file="",
                line=0,
                message=(
                    f"diff touches {total} changed lines (advisory threshold "
                    f"{max_lines}) — large diffs weaken every review layer; "
                    "consider splitting"
                ),
                evidence=str(total),
                fix_hint=(
                    "Tune via [runners.diff-size] max_lines; 0 disables the check."
                ),
            )
        ]
