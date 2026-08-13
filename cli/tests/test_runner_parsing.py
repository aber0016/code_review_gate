"""Runner output parsing and severity/action mapping, with canned tool output."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.findings import Action, Severity
from gauntlet.runners.bandit import parse_output as bandit_parse
from gauntlet.runners.mypy import parse_json_output, parse_text_output
from gauntlet.runners.pip_audit import (
    normalize,
    touched_packages,
)
from gauntlet.runners.pip_audit import (
    parse_output as pip_audit_parse,
)
from gauntlet.runners.ruff import classify
from gauntlet.runners.ruff import parse_output as ruff_parse
from gauntlet.runners.secrets import (
    parse_detect_secrets_output,
    scan_fallback,
)

ROOT = Path("/repo")


def ruff_diag(
    code: str | None,
    row: int,
    fixable: bool = False,
    filename: str = "src/a.py",
) -> dict[str, object]:
    return {
        "code": code,
        "message": f"msg for {code}",
        "filename": filename,
        "location": {"row": row, "column": 1},
        "end_location": {"row": row, "column": 5},
        "fix": {"message": "fix it"} if fixable else None,
    }


class TestRuff:
    def test_severity_classification(self) -> None:
        assert classify("E722") is Severity.ERROR
        assert classify("F401") is Severity.ERROR
        assert classify("B006") is Severity.ERROR
        assert classify("BLE001") is Severity.ERROR
        assert classify("W291") is Severity.WARNING
        assert classify("E501") is Severity.WARNING

    def test_action_mapping(self) -> None:
        stdout = json.dumps(
            [
                ruff_diag("F401", 1, fixable=True),  # error + fixable
                ruff_diag("E722", 2),  # error, not fixable
                ruff_diag("W291", 3),  # pure style
            ]
        )
        changed = {Path("src/a.py"): {1, 2, 3}}
        findings = ruff_parse(stdout, changed, ROOT)
        by_code = {f.evidence: f for f in findings}
        assert by_code["F401"].action is Action.AUTO_FIX
        assert by_code["E722"].action is Action.ASK_USER
        assert by_code["E722"].severity is Severity.ERROR
        assert by_code["W291"].action is Action.NO_OP

    def test_changed_line_filtering(self) -> None:
        stdout = json.dumps([ruff_diag("E722", 2), ruff_diag("E722", 40)])
        findings = ruff_parse(stdout, {Path("src/a.py"): {2}}, ROOT)
        assert [f.line for f in findings] == [2]

    def test_absolute_paths_relativized(self) -> None:
        stdout = json.dumps([ruff_diag("E722", 1, filename="/repo/src/a.py")])
        findings = ruff_parse(stdout, {Path("src/a.py"): {1}}, ROOT)
        assert findings[0].file == "src/a.py"
        assert findings[0].id == "ruff:E722:src/a.py:1"


class TestMypy:
    def test_json_output_filtered_to_changed_files(self) -> None:
        lines = [
            json.dumps(
                {
                    "file": "src/a.py",
                    "line": 3,
                    "severity": "error",
                    "message": "has no attribute 'gett'",
                    "code": "attr-defined",
                }
            ),
            json.dumps(
                {
                    "file": "src/other.py",
                    "line": 9,
                    "severity": "error",
                    "message": "elsewhere",
                    "code": "misc",
                }
            ),
            json.dumps(
                {
                    "file": "src/a.py",
                    "line": 4,
                    "severity": "note",
                    "message": "a note",
                    "code": None,
                }
            ),
        ]
        findings = parse_json_output("\n".join(lines), {Path("src/a.py")})
        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity is Severity.ERROR
        assert finding.action is Action.AUTO_FIX
        assert finding.id == "mypy:attr-defined:src/a.py:3"

    def test_text_fallback(self) -> None:
        stdout = (
            "src/a.py:7: error: Name 'x' is not defined  [name-defined]\n"
            "src/a.py:8: note: See docs\n"
            "src/b.py:1: error: other file  [misc]\n"
        )
        findings = parse_text_output(stdout, {Path("src/a.py")})
        assert len(findings) == 1
        assert findings[0].line == 7
        assert findings[0].evidence == "name-defined"


class TestBandit:
    def make_issue(self, severity: str, line: int) -> dict[str, object]:
        return {
            "filename": "./src/a.py",
            "issue_severity": severity,
            "issue_confidence": "HIGH",
            "issue_text": "weak hash",
            "line_number": line,
            "line_range": [line],
            "test_id": "B324",
            "test_name": "hashlib",
            "more_info": "https://bandit.example",
        }

    def test_severity_and_action_mapping(self) -> None:
        stdout = json.dumps(
            {
                "results": [
                    self.make_issue("HIGH", 1),
                    self.make_issue("MEDIUM", 2),
                    self.make_issue("LOW", 3),
                ]
            }
        )
        changed = {Path("src/a.py"): {1, 2, 3}}
        findings = bandit_parse(stdout, changed)
        assert [(str(f.severity), str(f.action)) for f in findings] == [
            ("error", "ask-user"),
            ("warning", "ask-user"),
            ("info", "no-op"),
        ]

    def test_line_range_filtering(self) -> None:
        stdout = json.dumps({"results": [self.make_issue("HIGH", 10)]})
        assert bandit_parse(stdout, {Path("src/a.py"): {3}}) == []


class TestSecrets:
    def test_fallback_patterns_and_line_scope(self, tmp_path: Path) -> None:
        # Build test secrets dynamically so this source file never contains one.
        aws = "AKIA" + "0" * 16
        key_header = "-----BEGIN " + "RSA PRIVATE KEY-----"
        generic = "api_key = " + '"' + "a1" * 12 + '"'
        content = "\n".join(["clean line", aws, key_header, generic, "xoxb-123"])
        (tmp_path / "conf.py").write_text(content + "\n")

        findings = scan_fallback(tmp_path, {Path("conf.py"): {2, 3, 4, 5}})
        patterns = {f.evidence for f in findings}
        assert patterns == {
            "aws-access-key-id",
            "private-key",
            "generic-api-key",
            "slack-token",
        }
        for finding in findings:
            assert finding.severity is Severity.ERROR
            assert finding.action is Action.ASK_USER
            assert aws not in finding.message  # never print the secret

        scoped = scan_fallback(tmp_path, {Path("conf.py"): {3}})
        assert {f.evidence for f in scoped} == {"private-key"}

    def test_detect_secrets_parse(self) -> None:
        stdout = json.dumps(
            {
                "results": {
                    "src/a.py": [
                        {"type": "AWS Access Key", "line_number": 2},
                        {"type": "AWS Access Key", "line_number": 99},
                    ]
                }
            }
        )
        findings = parse_detect_secrets_output(stdout, {Path("src/a.py"): {2}})
        assert len(findings) == 1
        assert findings[0].message == "potential secret detected (AWS Access Key)"


class TestPipAudit:
    def test_touched_vs_preexisting(self) -> None:
        doc = {
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.19.0",
                    "vulns": [
                        {
                            "id": "PYSEC-2018-28",
                            "fix_versions": ["2.20.0"],
                            "description": "CRLF injection",
                        }
                    ],
                },
                {
                    "name": "old-lib",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-X", "fix_versions": [], "description": "d"}],
                },
                {"name": "clean-lib", "version": "1.0", "vulns": []},
            ]
        }
        findings = pip_audit_parse(doc, {"requests"}, "pyproject.toml")
        by_pkg = {f.id: f for f in findings}
        assert by_pkg["pip-audit:PYSEC-2018-28:requests:0"].severity is Severity.ERROR
        assert by_pkg["pip-audit:CVE-X:old-lib:0"].severity is Severity.WARNING
        assert all(f.action is Action.ASK_USER for f in findings)
        assert len(findings) == 2

    def test_touched_packages_from_changed_manifest_lines(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = [\n    "requests>=2",\n'
            '    "Flask-Login==0.6",\n]\n'
        )
        (tmp_path / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.0"\n'
        )
        touched = touched_packages(
            tmp_path,
            {
                Path("pyproject.toml"): {4, 5},
                Path("uv.lock"): {4},
            },
        )
        assert touched == {"requests", "flask-login"}

    def test_normalize(self) -> None:
        assert normalize("Flask_Login") == "flask-login"
        assert normalize("zope.interface") == "zope-interface"
