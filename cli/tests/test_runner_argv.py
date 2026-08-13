"""Every subprocess argv construction is covered here (plan §13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.config import Config
from gauntlet.runners import RunContext
from gauntlet.runners.bandit import BanditRunner
from gauntlet.runners.imports import ImportsRunner
from gauntlet.runners.lockfile import LockfileRunner
from gauntlet.runners.mypy import MypyRunner
from gauntlet.runners.pip_audit import PipAuditRunner
from gauntlet.runners.ruff import RuffRunner
from gauntlet.runners.secrets import SecretsRunner


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def make_ctx(root: Path, raw_config: dict[str, object] | None = None) -> RunContext:
    return RunContext(
        root=root,
        base_ref="main",
        merge_base="a" * 40,
        head="b" * 7,
        changed_files=[Path("src/a.py")],
        changed_lines={Path("src/a.py"): {1}},
        config=Config(raw=raw_config or {}),
        tier="fast",
        timeout=120.0,
    )


def make_fake_venv(root: Path) -> Path:
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("python", "ruff", "mypy", "bandit", "uv", "detect-secrets"):
        (bin_dir / tool).touch()
    return bin_dir


class TestVenvResolution:
    def test_find_tool_prefers_target_venv(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        ctx = make_ctx(tmp_path)
        assert ctx.find_tool("ruff") == str(bin_dir / "ruff")
        assert ctx.repo_python() == bin_dir / "python"

    def test_no_venv_falls_back(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        assert ctx.venv_bin() is None
        assert ctx.repo_python().name.startswith("python")


class TestArgvConstruction:
    def test_ruff(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        ctx = make_ctx(tmp_path, {"runners": {"ruff": {"args": ["--select", "E"]}}})
        argv = RuffRunner(ctx).build_argv([Path("src/a.py")])
        assert argv == [
            str(bin_dir / "ruff"),
            "check",
            "--force-exclude",
            "--output-format",
            "json",
            "--select",
            "E",
            "src/a.py",
        ]

    def test_ruff_fix_only(self, tmp_path: Path) -> None:
        make_fake_venv(tmp_path)
        argv = RuffRunner(make_ctx(tmp_path)).build_argv(
            [Path("src/a.py")], fix_only=True
        )
        assert "--fix-only" in argv
        assert "--output-format" not in argv

    def test_mypy(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        argv = MypyRunner(make_ctx(tmp_path)).build_argv([Path("src/a.py")])
        assert argv[0] == str(bin_dir / "mypy")
        assert "--output" in argv
        assert "json" in argv
        assert "--cache-dir=/dev/null" in argv  # never mutate the working tree
        assert argv[-1] == "src/a.py"

    def test_mypy_text_fallback(self, tmp_path: Path) -> None:
        make_fake_venv(tmp_path)
        argv = MypyRunner(make_ctx(tmp_path)).build_argv(
            [Path("src/a.py")], json_output=False
        )
        assert "--output" not in argv

    def test_bandit(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        argv = BanditRunner(make_ctx(tmp_path)).build_argv([Path("src/a.py")])
        assert argv == [
            str(bin_dir / "bandit"),
            "-f",
            "json",
            "-q",
            "-r",
            "src/a.py",
        ]

    def test_pip_audit_runs_in_target_interpreter(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        argv = PipAuditRunner(make_ctx(tmp_path)).build_argv()
        assert argv == [
            str(bin_dir / "python"),
            "-m",
            "pip_audit",
            "-f",
            "json",
            "--progress-spinner",
            "off",
        ]

    def test_lockfile(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        runner = LockfileRunner(make_ctx(tmp_path))
        assert runner.build_argv(check=True) == [str(bin_dir / "uv"), "lock", "--check"]
        assert runner.build_argv(check=False) == [str(bin_dir / "uv"), "lock"]

    def test_secrets_detect_secrets_engine(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        argv = SecretsRunner(make_ctx(tmp_path)).build_argv([Path("src/a.py")])
        assert argv == [str(bin_dir / "detect-secrets"), "scan", "src/a.py"]

    def test_imports_probe_runs_in_target_interpreter(self, tmp_path: Path) -> None:
        bin_dir = make_fake_venv(tmp_path)
        argv = ImportsRunner(make_ctx(tmp_path)).build_probe_argv()
        assert argv[0] == str(bin_dir / "python")
        assert argv[1] == "-c"
        assert "packages_distributions" in argv[2]


class TestRunCmd:
    def test_timeout_is_enforced(self, tmp_path: Path) -> None:
        import sys

        ctx = make_ctx(tmp_path)
        result = ctx.run_cmd(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
        )
        assert result.timed_out
        assert result.code == -1

    def test_missing_binary_reports_127(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        result = ctx.run_cmd(["/nonexistent/definitely-not-a-tool"])
        assert result.code == 127
        assert not result.timed_out
