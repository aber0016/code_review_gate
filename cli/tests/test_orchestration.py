"""Tier orchestration: parallel execution, crash containment, exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

import gauntlet.runners
from gauntlet.cli import execute_tier
from gauntlet.config import Config, ConfigError
from gauntlet.findings import (
    EXIT_BLOCKING,
    EXIT_CRASH,
    EXIT_OK,
    Action,
    Finding,
    Layer,
    Severity,
    make_id,
)
from gauntlet.runners import RunContext


def make_ctx(tmp_path: Path, raw_config: dict[str, object] | None = None) -> RunContext:
    return RunContext(
        root=tmp_path,
        base_ref="main",
        merge_base="a" * 40,
        head="b" * 7,
        changed_files=[Path("src/a.py")],
        changed_lines={Path("src/a.py"): {1}},
        config=Config(raw=raw_config or {}),
        tier="fast",
        timeout=10.0,
    )


class FakeRunner:
    layer = Layer.STATIC
    tier = "fast"

    def __init__(
        self,
        name: str,
        findings: list[Finding] | None = None,
        crash: bool = False,
        is_available: bool = True,
    ) -> None:
        self.name = name
        self._findings = findings or []
        self._crash = crash
        self._available = is_available

    def available(self) -> bool:
        return self._available

    def run(self, ctx: RunContext) -> list[Finding]:
        if self._crash:
            raise RuntimeError("kaboom")
        return self._findings


def blocking_finding(tool: str) -> Finding:
    return Finding(
        id=make_id(tool, "X", "src/a.py", 1),
        layer=Layer.STATIC,
        tool=tool,
        severity=Severity.ERROR,
        action=Action.AUTO_FIX,
        file="src/a.py",
        line=1,
        message="bad",
    )


def install_fakes(monkeypatch: pytest.MonkeyPatch, runners: list[FakeRunner]) -> None:
    monkeypatch.setattr(gauntlet.runners, "all_runners", lambda ctx: list(runners))


class TestExecuteTier:
    def test_green_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch, [FakeRunner("clean")])
        report = execute_tier(make_ctx(tmp_path))
        assert report.runners == {"clean": "ok"}
        assert report.exit_code() == EXIT_OK

    def test_blocking_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(monkeypatch, [FakeRunner("finder", [blocking_finding("finder")])])
        report = execute_tier(make_ctx(tmp_path))
        assert report.exit_code() == EXIT_BLOCKING

    def test_crash_is_contained_and_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(
            monkeypatch, [FakeRunner("boomer", crash=True), FakeRunner("clean")]
        )
        report = execute_tier(make_ctx(tmp_path))
        assert report.runners["clean"] == "ok"
        assert report.runners["boomer"].startswith("crashed: ")
        assert report.exit_code() == EXIT_CRASH
        crash_findings = [f for f in report.findings if f.tool == "boomer"]
        assert len(crash_findings) == 1
        assert crash_findings[0].severity is Severity.ERROR
        assert "kaboom" in crash_findings[0].message

    def test_unavailable_runner_degrades_visibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(monkeypatch, [FakeRunner("ghost", is_available=False)])
        report = execute_tier(make_ctx(tmp_path))
        assert report.runners == {"ghost": "skipped"}
        assert len(report.findings) == 1
        skip = report.findings[0]
        assert skip.severity is Severity.INFO
        assert skip.action is Action.NO_OP
        assert "not installed; layer skipped" in skip.message
        assert report.exit_code() == EXIT_OK

    def test_disabled_runner_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(monkeypatch, [FakeRunner("noisy", [blocking_finding("noisy")])])
        ctx = make_ctx(tmp_path, {"runners": {"noisy": {"enabled": False}}})
        report = execute_tier(ctx)
        assert report.runners == {}
        assert report.exit_code() == EXIT_OK

    def test_runner_selection_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(
            monkeypatch,
            [FakeRunner("a", [blocking_finding("a")]), FakeRunner("b")],
        )
        report = execute_tier(make_ctx(tmp_path), only="b")
        assert report.runners == {"b": "ok"}

    def test_unknown_runner_name_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fakes(monkeypatch, [FakeRunner("a")])
        with pytest.raises(ConfigError, match="no runner named"):
            execute_tier(make_ctx(tmp_path), only="zzz")


class TestExcludePaths:
    def test_excluded_prefixes(self) -> None:
        from gauntlet.cli import excluded

        assert excluded(Path("fixture/src/a.py"), ["fixture"])
        assert excluded(Path("fixture"), ["fixture"])
        assert not excluded(Path("fixtures/other.py"), ["fixture"])
        assert not excluded(Path("cli/src/a.py"), ["fixture"])
        assert not excluded(Path("a.py"), [])
