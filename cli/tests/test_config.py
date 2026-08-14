"""Config loading, defaults, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.config import Config, ConfigError, matches_any


def load(tmp_path: Path, toml: str | None = None) -> Config:
    if toml is not None:
        (tmp_path / ".gauntlet.toml").write_text(toml)
    return Config.load(tmp_path)


class TestDefaults:
    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        config = load(tmp_path)
        assert config.base is None
        assert config.critical_paths == []
        assert config.test_paths == ["tests", "test", "conftest.py"]
        assert config.src_paths == ["src"]
        assert config.sandbox == "none"
        assert config.sandbox_image == "python:3.12-slim"
        assert config.diff_cover_min == 80.0
        assert config.fix_max_rounds == 3
        assert config.deep_blocking is False
        assert config.deep_max_survivors == 0
        assert config.review_provider is None
        assert config.runner_enabled("ruff") is True
        assert config.runner_args("ruff") == []
        assert config.runner_timeout("ruff") is None


class TestParsing:
    def test_full_config(self, tmp_path: Path) -> None:
        config = load(
            tmp_path,
            """
            base = "origin/develop"
            test_paths = ["tests"]
            src_paths = ["src", "lib"]
            sandbox = "docker"
            sandbox_image = "python:3.11-slim"

            [review]
            provider = "openai"
            model = "gpt-5.6-sol"

            [exec]
            diff_cover_min = 65

            [fix]
            max_rounds = 5

            [deep]
            blocking = true
            max_survivors = 2

            [runners.ruff]
            enabled = false
            args = ["--select", "E"]
            timeout = 30

            [runners.imports]
            check_pypi = true
            """,
        )
        assert config.base == "origin/develop"
        assert config.src_paths == ["src", "lib"]
        assert config.sandbox == "docker"
        assert config.sandbox_image == "python:3.11-slim"
        assert config.review_provider == "openai"
        assert config.review_model == "gpt-5.6-sol"
        assert config.diff_cover_min == 65.0
        assert config.fix_max_rounds == 5
        assert config.deep_blocking is True
        assert config.deep_max_survivors == 2
        assert config.runner_enabled("ruff") is False
        assert config.runner_args("ruff") == ["--select", "E"]
        assert config.runner_timeout("ruff") == 30.0
        assert config.runner_option("imports", "check_pypi", default=False) is True


class TestValidation:
    def test_invalid_sandbox_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="sandbox"):
            load(tmp_path, 'sandbox = "firecracker"')

    def test_invalid_test_paths_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="test_paths"):
            load(tmp_path, "test_paths = [1, 2]")

    def test_exclude_paths(self, tmp_path: Path) -> None:
        assert load(tmp_path).exclude_paths == []
        config = load(tmp_path, 'exclude_paths = ["fixture", "vendor"]')
        assert config.exclude_paths == ["fixture", "vendor"]
        with pytest.raises(ConfigError, match="exclude_paths"):
            load(tmp_path, "exclude_paths = [3]")

    def test_toml_syntax_error_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load(tmp_path, "base = [unclosed")

    def test_invalid_runner_args_rejected(self, tmp_path: Path) -> None:
        config = load(tmp_path, "[runners.ruff]\nargs = [1]")
        with pytest.raises(ConfigError, match=r"runners\.ruff\.args"):
            config.runner_args("ruff")

    def test_critical_paths(self, tmp_path: Path) -> None:
        config = load(tmp_path, 'critical_paths = ["src/auth", "**/migrations/**"]')
        assert config.critical_paths == ["src/auth", "**/migrations/**"]
        with pytest.raises(ConfigError, match="critical_paths"):
            load(tmp_path, 'critical_paths = "src/auth"')


class TestMatchesAny:
    def test_glob_patterns_cross_slashes(self) -> None:
        assert matches_any(Path("src/auth/login.py"), ["src/auth/**"])
        assert matches_any(Path("pkg/migrations/m1.py"), ["**/migrations/**"])
        assert matches_any(Path("a/deep/nested/secret.py"), ["*secret*"])
        assert not matches_any(Path("src/other.py"), ["src/auth/**"])

    def test_plain_patterns_match_as_prefixes(self) -> None:
        assert matches_any(Path("src/auth/login.py"), ["src/auth"])
        assert matches_any(Path("src/auth"), ["src/auth"])
        assert not matches_any(Path("src/authx/login.py"), ["src/auth"])
        assert not matches_any(Path("a.py"), [])
