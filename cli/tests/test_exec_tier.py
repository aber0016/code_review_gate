"""exec tier: sandbox strategy, docker argv, junit/diff-cover parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.config import Config
from gauntlet.findings import Action, Severity
from gauntlet.runners import RunContext
from gauntlet.runners.pytest_cov import (
    PytestCovRunner,
    parse_diff_cover,
    parse_junit,
    remap_coverage_sources,
)
from gauntlet.sandbox import (
    DockerSandbox,
    HostSandbox,
    select_sandbox,
)


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def make_ctx(root: Path, raw_config: dict[str, object] | None = None) -> RunContext:
    return RunContext(
        root=root,
        base_ref="main",
        merge_base="c" * 40,
        head="b" * 7,
        changed_files=[Path("src/a.py")],
        changed_lines={Path("src/a.py"): {1}},
        config=Config(raw=raw_config or {}),
        tier="exec",
        timeout=600.0,
    )


class TestDockerArgv:
    def test_network_none_and_limits(self, tmp_path: Path) -> None:
        """The plan's Done-when: --network=none must be in the constructed argv."""
        sandbox = DockerSandbox("python:3.12-slim", tmp_path, docker="/usr/bin/docker")
        argv = sandbox.build_pytest_argv(
            make_ctx(tmp_path),
            ["-q", "--cov=src"],
            env={"COVERAGE_FILE": ".gauntlet/tmp/.coverage"},
        )
        assert argv[0] == "/usr/bin/docker"
        assert "--network=none" in argv
        assert "--cpus=2" in argv
        assert "--memory=2g" in argv
        assert f"{tmp_path}:/work" in argv
        assert "python:3.12-slim" in argv
        # env must reach the container process, not just the docker client
        assert "-e" in argv
        assert "COVERAGE_FILE=.gauntlet/tmp/.coverage" in argv
        inner = argv[-1]
        assert "pip install" in inner and "'.[test]'" in inner
        # --network=none means no PyPI: the image provides the build backend
        assert "--no-build-isolation" in inner
        assert "python -m pytest -q --cov=src" in inner

    def test_image_comes_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gauntlet.sandbox as sandbox_mod

        ctx = make_ctx(
            tmp_path, {"sandbox": "docker", "sandbox_image": "python:3.11-slim"}
        )
        monkeypatch.setattr(sandbox_mod, "docker_usable", lambda _ctx: True)
        selected, findings = select_sandbox(ctx)
        assert isinstance(selected, DockerSandbox)
        assert selected.image == "python:3.11-slim"
        assert findings == []


class TestSandboxSelection:
    def test_host_default_emits_no_op_warning(self, tmp_path: Path) -> None:
        sandbox, findings = select_sandbox(make_ctx(tmp_path))
        assert isinstance(sandbox, HostSandbox)
        assert len(findings) == 1
        assert findings[0].severity is Severity.WARNING
        assert findings[0].action is Action.NO_OP  # visible, never blocks
        assert not findings[0].blocking()

    def test_docker_requested_but_unavailable_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gauntlet.sandbox as sandbox_mod

        ctx = make_ctx(tmp_path, {"sandbox": "docker"})
        monkeypatch.setattr(sandbox_mod, "docker_usable", lambda _ctx: False)
        sandbox, findings = select_sandbox(ctx)
        assert isinstance(sandbox, HostSandbox)
        assert len(findings) == 1
        assert findings[0].action is Action.ASK_USER
        assert findings[0].blocking()  # the downgrade must block, not pass silently

    def test_host_pytest_argv_uses_repo_python(self, tmp_path: Path) -> None:
        argv = HostSandbox().build_pytest_argv(make_ctx(tmp_path), ["-q"])
        assert argv[1:] == ["-m", "pytest", "-q"]
        assert argv[0].endswith("python") or argv[0].endswith("python3")


class TestJunitParsing:
    def write_junit(self, tmp_path: Path, body: str) -> Path:
        junit = tmp_path / "junit.xml"
        junit.write_text(
            f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}'
            "</testsuite></testsuites>"
        )
        return junit

    def test_failure_is_auto_fix(self, tmp_path: Path) -> None:
        junit = self.write_junit(
            tmp_path,
            '<testcase classname="tests.test_x" name="test_a" file="tests/test_x.py"'
            ' line="3"><failure message="assert 1 == 2">traceback here</failure>'
            "</testcase>",
        )
        findings = parse_junit(junit)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity is Severity.ERROR
        assert finding.action is Action.AUTO_FIX
        assert "tests.test_x::test_a" in finding.message
        assert finding.evidence == "traceback here"

    def test_collection_error_is_ask_user(self, tmp_path: Path) -> None:
        junit = self.write_junit(
            tmp_path,
            '<testcase classname="" name="tests/test_broken.py">'
            '<error message="collection failure">ImportError: nope</error>'
            "</testcase>",
        )
        findings = parse_junit(junit)
        assert len(findings) == 1
        assert findings[0].action is Action.ASK_USER

    def test_evidence_truncated_to_2000(self, tmp_path: Path) -> None:
        junit = self.write_junit(
            tmp_path,
            '<testcase classname="t" name="n"><failure message="m">'
            + "x" * 5000
            + "</failure></testcase>",
        )
        findings = parse_junit(junit)
        assert len(findings[0].evidence) == 2000

    def test_passing_suite_yields_nothing(self, tmp_path: Path) -> None:
        junit = self.write_junit(tmp_path, '<testcase classname="t" name="ok"/>')
        assert parse_junit(junit) == []


class TestDiffCover:
    def write_report(self, tmp_path: Path, pct: float, stats: dict) -> Path:  # type: ignore[type-arg]
        dc = tmp_path / "dc.json"
        dc.write_text(json.dumps({"total_percent_covered": pct, "src_stats": stats}))
        return dc

    def test_below_threshold_names_uncovered_lines(self, tmp_path: Path) -> None:
        dc = self.write_report(
            tmp_path,
            20.0,
            {"src/fixture_pkg/pricing.py": {"violation_lines": [3, 4]}},
        )
        pct, findings = parse_diff_cover(dc, 80.0)
        assert pct == 20.0
        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity is Severity.ERROR
        assert finding.action is Action.AUTO_FIX
        assert "src/fixture_pkg/pricing.py:3,4" in finding.message
        assert "20.0%" in finding.message

    def test_at_threshold_passes(self, tmp_path: Path) -> None:
        dc = self.write_report(tmp_path, 80.0, {})
        _, findings = parse_diff_cover(dc, 80.0)
        assert findings == []

    def test_threshold_zero_always_passes(self, tmp_path: Path) -> None:
        dc = self.write_report(tmp_path, 0.0, {"a.py": {"violation_lines": [1]}})
        _, findings = parse_diff_cover(dc, 0.0)
        assert findings == []


class TestCoverageRemap:
    def test_container_sources_translated_to_host(self, tmp_path: Path) -> None:
        cov = tmp_path / "coverage.xml"
        cov.write_text(
            '<?xml version="1.0"?><coverage><sources>'
            "<source>/work/src</source><source>/work</source>"
            "<source>/elsewhere</source></sources></coverage>"
        )
        remap_coverage_sources(cov, "/work", "/host/repo")
        content = cov.read_text()
        assert "<source>/host/repo/src</source>" in content
        assert "<source>/host/repo</source>" in content
        assert "<source>/elsewhere</source>" in content  # untouched
        assert "/work" not in content

    def test_workspace_prefix_not_mangled(self, tmp_path: Path) -> None:
        """`/workspace` must not be rewritten by the `/work` rule."""
        cov = tmp_path / "coverage.xml"
        cov.write_text(
            '<?xml version="1.0"?><coverage><sources>'
            "<source>/workspace/src</source></sources></coverage>"
        )
        remap_coverage_sources(cov, "/work", "/host/repo")
        assert "<source>/workspace/src</source>" in cov.read_text()


class TestPytestArgs:
    def test_cov_and_reports(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        runner = PytestCovRunner(make_ctx(tmp_path))
        args = runner.build_pytest_args(Path("/t/coverage.xml"), Path("/t/junit.xml"))
        assert args[0] == "-q"
        assert "--cov=src" in args
        assert "--cov-report=xml:/t/coverage.xml" in args
        assert "--junitxml=/t/junit.xml" in args

    def test_diff_cover_argv(self, tmp_path: Path) -> None:
        runner = PytestCovRunner(make_ctx(tmp_path))
        argv = runner.build_diff_cover_argv(Path("/t/coverage.xml"), Path("/t/dc.json"))
        assert argv[1] == "/t/coverage.xml"
        assert f"--compare-branch={'c' * 40}" in argv
        assert "--json-report" in argv
