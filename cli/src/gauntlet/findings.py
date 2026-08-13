"""The finding contract (§2 of the plan).

One stable JSON shape for findings, one report envelope, and the CI exit-code
semantics. This module is the interchange format between the CLI, the pi
extension, and CI — do not weaken it to make a check pass.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

REPORT_VERSION = 1

EXIT_OK = 0
"""No blocking findings."""
EXIT_BLOCKING = 1
"""Blocking findings exist."""
EXIT_CRASH = 2
"""A runner itself crashed — fail closed: a crashed gate is a red gate."""


class Severity(enum.StrEnum):
    """Finding severity. Errors and warnings block; info never does."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort rank, most severe first."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


class Action(enum.StrEnum):
    """How a finding may be resolved."""

    AUTO_FIX = "auto-fix"
    """Mechanical; a fixing agent may resolve it without human approval."""
    ASK_USER = "ask-user"
    """Touches intent or risk; must park for a human decision (Layer 7)."""
    NO_OP = "no-op"
    """Informational; shown, never blocks, never triggers a fix round."""


class Layer(enum.StrEnum):
    """Pipeline layer a finding belongs to."""

    PRECOMMIT = "precommit"
    STATIC = "static"
    SUPPLY_CHAIN = "supply-chain"
    EXEC = "exec"
    TEST_GEN = "test-gen"
    MUTATION = "mutation"
    REVIEW = "review"


def make_id(tool: str, code: str, file: str, location: int | str = 0) -> str:
    """Build a finding id that stays stable across runs for the same issue.

    Composition is ``tool:code:file:location``. ``location`` is normally the
    line number; runners whose findings are not line-anchored may use a more
    stable key (e.g. a module or package name) so user dismissals survive
    line drift between fix rounds.
    """
    return f"{tool}:{code}:{file}:{location}"


@dataclass
class Finding:
    """A single gate finding in the stable §2 contract shape."""

    id: str
    layer: Layer
    tool: str
    severity: Severity
    action: Action
    file: str
    line: int
    message: str
    evidence: str = ""
    fix_hint: str = ""

    def blocking(self) -> bool:
        """Whether this finding blocks the gate.

        Errors and warnings block; ``info`` never does, and neither does any
        finding whose action is ``no-op`` (informational by definition).
        """
        return self.severity is not Severity.INFO and self.action is not Action.NO_OP

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the plain-JSON contract shape."""
        return {
            "id": self.id,
            "layer": str(self.layer),
            "tool": self.tool,
            "severity": str(self.severity),
            "action": str(self.action),
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "evidence": self.evidence,
            "fix_hint": self.fix_hint,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Finding:
        """Parse and validate a finding from its JSON shape.

        Raises:
            ValueError: if enum fields carry values outside the contract.
        """
        return cls(
            id=str(raw["id"]),
            layer=Layer(raw["layer"]),
            tool=str(raw["tool"]),
            severity=Severity(raw["severity"]),
            action=Action(raw["action"]),
            file=str(raw.get("file", "")),
            line=int(raw.get("line", 0)),
            message=str(raw["message"]),
            evidence=str(raw.get("evidence", "")),
            fix_hint=str(raw.get("fix_hint", "")),
        )


@dataclass
class Report:
    """The single JSON document `gauntlet run --json` prints to stdout."""

    tier: str
    base: str
    head: str
    changed_files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    runners: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    version: int = REPORT_VERSION

    def sort_findings(self) -> None:
        """Sort findings by (severity desc, file, line)."""
        self.findings.sort(key=lambda f: (f.severity.rank, f.file, f.line))

    def blocking_findings(self) -> list[Finding]:
        """All findings that block the gate."""
        return [f for f in self.findings if f.blocking()]

    def crashed(self) -> bool:
        """Whether any runner crashed (stats value starts with ``crashed``)."""
        return any(status.startswith("crashed") for status in self.runners.values())

    def exit_code(self) -> int:
        """CI contract: 0 = green, 1 = blocking findings, 2 = runner crash."""
        if self.crashed():
            return EXIT_CRASH
        if self.blocking_findings():
            return EXIT_BLOCKING
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the report envelope shape from §2."""
        return {
            "version": self.version,
            "tier": self.tier,
            "base": self.base,
            "head": self.head,
            "changed_files": self.changed_files,
            "findings": [f.to_dict() for f in self.findings],
            "stats": {
                "duration_s": round(self.duration_s, 3),
                "runners": dict(self.runners),
            },
        }

    def to_json(self) -> str:
        """Render the single JSON document for stdout."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Report:
        """Parse a report from its JSON shape (used by tests and tooling)."""
        stats = raw.get("stats", {})
        return cls(
            tier=str(raw["tier"]),
            base=str(raw["base"]),
            head=str(raw["head"]),
            changed_files=[str(p) for p in raw.get("changed_files", [])],
            findings=[Finding.from_dict(f) for f in raw.get("findings", [])],
            runners={str(k): str(v) for k, v in stats.get("runners", {}).items()},
            duration_s=float(stats.get("duration_s", 0.0)),
            version=int(raw.get("version", REPORT_VERSION)),
        )
