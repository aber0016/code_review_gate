"""Contract tests for findings.py: blocking semantics, exit codes, JSON shape."""

from __future__ import annotations

import json

import pytest

from gauntlet.findings import (
    EXIT_BLOCKING,
    EXIT_CRASH,
    EXIT_OK,
    Action,
    Finding,
    Layer,
    Report,
    Severity,
    make_id,
)


def _finding(
    severity: Severity = Severity.ERROR,
    action: Action = Action.AUTO_FIX,
    file: str = "src/x.py",
    line: int = 1,
    tool: str = "ruff",
) -> Finding:
    return Finding(
        id=make_id(tool, "X1", file, line),
        layer=Layer.STATIC,
        tool=tool,
        severity=severity,
        action=action,
        file=file,
        line=line,
        message="msg",
    )


class TestBlocking:
    def test_error_blocks(self) -> None:
        assert _finding(Severity.ERROR, Action.AUTO_FIX).blocking()
        assert _finding(Severity.ERROR, Action.ASK_USER).blocking()

    def test_warning_blocks_unless_no_op(self) -> None:
        assert _finding(Severity.WARNING, Action.ASK_USER).blocking()
        assert not _finding(Severity.WARNING, Action.NO_OP).blocking()

    def test_info_never_blocks(self) -> None:
        for action in Action:
            assert not _finding(Severity.INFO, action).blocking()


class TestExitCodes:
    def test_green(self) -> None:
        report = Report(tier="fast", base="a" * 7, head="b" * 7)
        report.findings.append(_finding(Severity.INFO, Action.NO_OP))
        assert report.exit_code() == EXIT_OK

    def test_blocking(self) -> None:
        report = Report(tier="fast", base="a" * 7, head="b" * 7)
        report.findings.append(_finding())
        assert report.exit_code() == EXIT_BLOCKING

    def test_crash_wins_fail_closed(self) -> None:
        report = Report(tier="fast", base="a" * 7, head="b" * 7)
        report.findings.append(_finding())
        report.runners["ruff"] = "crashed: boom"
        assert report.exit_code() == EXIT_CRASH

    def test_crash_without_findings_is_still_red(self) -> None:
        report = Report(tier="fast", base="a" * 7, head="b" * 7)
        report.runners["mypy"] = "crashed: exploded"
        assert report.exit_code() == EXIT_CRASH


class TestJsonShape:
    def test_report_envelope(self) -> None:
        report = Report(
            tier="fast",
            base="a1b2c3d",
            head="d4e5f6a",
            changed_files=["src/fetcher.py"],
            findings=[_finding()],
            runners={"ruff": "ok"},
            duration_s=2.1,
        )
        doc = json.loads(report.to_json())
        assert set(doc) == {
            "version",
            "tier",
            "base",
            "head",
            "changed_files",
            "findings",
            "stats",
        }
        assert doc["version"] == 1
        assert doc["stats"]["runners"] == {"ruff": "ok"}
        assert doc["stats"]["duration_s"] == 2.1

    def test_finding_fields_are_plain_strings(self) -> None:
        doc = _finding().to_dict()
        assert doc == {
            "id": "ruff:X1:src/x.py:1",
            "layer": "static",
            "tool": "ruff",
            "severity": "error",
            "action": "auto-fix",
            "file": "src/x.py",
            "line": 1,
            "message": "msg",
            "evidence": "",
            "fix_hint": "",
        }
        assert all(isinstance(v, str) for k, v in doc.items() if k not in {"line"})

    def test_round_trip(self) -> None:
        report = Report(
            tier="exec",
            base="a1b2c3d",
            head="d4e5f6a",
            changed_files=["a.py"],
            findings=[_finding(Severity.WARNING, Action.ASK_USER)],
            runners={"pytest": "ok"},
            duration_s=1.0,
        )
        restored = Report.from_dict(json.loads(report.to_json()))
        assert restored == report

    def test_from_dict_rejects_out_of_contract_enum(self) -> None:
        raw = _finding().to_dict()
        raw["severity"] = "catastrophic"
        with pytest.raises(ValueError):
            Finding.from_dict(raw)


class TestSorting:
    def test_severity_then_file_then_line(self) -> None:
        report = Report(tier="fast", base="a" * 7, head="b" * 7)
        report.findings = [
            _finding(Severity.INFO, Action.NO_OP, file="a.py", line=1),
            _finding(Severity.ERROR, Action.AUTO_FIX, file="z.py", line=9),
            _finding(Severity.ERROR, Action.AUTO_FIX, file="a.py", line=5),
            _finding(Severity.WARNING, Action.ASK_USER, file="a.py", line=2),
            _finding(Severity.ERROR, Action.AUTO_FIX, file="a.py", line=2),
        ]
        report.sort_findings()
        keys = [(str(f.severity), f.file, f.line) for f in report.findings]
        assert keys == [
            ("error", "a.py", 2),
            ("error", "a.py", 5),
            ("error", "z.py", 9),
            ("warning", "a.py", 2),
            ("info", "a.py", 1),
        ]
