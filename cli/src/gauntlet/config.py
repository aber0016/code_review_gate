""".gauntlet.toml loader (§3.6 of the plan).

All keys are optional; defaults are the documented contract. Unknown keys are
preserved in ``raw`` but ignored, so configs stay forward-compatible.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = ".gauntlet.toml"

DEFAULT_TEST_PATHS = ["tests", "test", "conftest.py"]
DEFAULT_SRC_PATHS = ["src"]
DEFAULT_CRITICAL_PATHS: list[str] = []
DEFAULT_SANDBOX = "none"
DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"
DEFAULT_DIFF_COVER_MIN = 80.0
DEFAULT_FIX_MAX_ROUNDS = 3
DEFAULT_DEEP_BLOCKING = False
DEFAULT_DEEP_MAX_SURVIVORS = 0

_VALID_SANDBOXES = ("none", "docker")


class ConfigError(ValueError):
    """Raised when .gauntlet.toml is malformed or carries invalid values."""


_GLOB_META_RE = re.compile(r"[*?\[]")


def matches_any(path: Path, patterns: list[str]) -> bool:
    """Whether a repo-relative path matches any of the configured patterns.

    Glob patterns go through :func:`fnmatch.fnmatch`, where ``*`` crosses
    ``/`` — intentional, so ``src/auth/**`` and ``**/migrations/**`` behave
    as users expect. A pattern with no glob metacharacters matches as an
    exact path or directory prefix (same idiom as ``cli.excluded``).
    """
    for pat in patterns:
        if _GLOB_META_RE.search(pat):
            if fnmatch.fnmatch(str(path), pat):
                return True
        elif path == Path(pat) or path.is_relative_to(pat):
            return True
    return False


@dataclass
class Config:
    """Typed accessors over the raw .gauntlet.toml mapping."""

    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Config:
        """Load ``<root>/.gauntlet.toml``; a missing file yields all defaults.

        Raises:
            ConfigError: on TOML syntax errors or invalid values.
        """
        path = root / CONFIG_FILENAME
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = tomllib.loads(path.read_text())
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path}: {exc}") from exc
        config = cls(raw=raw)
        config._validate()
        return config

    def _validate(self) -> None:
        if self.sandbox not in _VALID_SANDBOXES:
            raise ConfigError(
                f"sandbox must be one of {_VALID_SANDBOXES}, got {self.sandbox!r}"
            )
        for key in ("test_paths", "src_paths", "exclude_paths", "critical_paths"):
            value = self.raw.get(key)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                raise ConfigError(f"{key} must be a list of strings")

    def get(self, *keys: str, default: Any = None) -> Any:
        """Fetch a nested key path, e.g. ``get("review", "provider")``."""
        node: Any = self.raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ---- top-level keys -------------------------------------------------

    @property
    def base(self) -> str | None:
        """Configured base ref, if any."""
        value = self.raw.get("base")
        return str(value) if value is not None else None

    @property
    def test_paths(self) -> list[str]:
        """Repo-relative paths that count as test code (the test-lock scope)."""
        value = self.raw.get("test_paths")
        return list(value) if value is not None else list(DEFAULT_TEST_PATHS)

    @property
    def exclude_paths(self) -> list[str]:
        """Repo-relative path prefixes removed from the diff scope entirely.

        For vendored code and intentionally-broken test fixtures (like this
        repo's own ``fixture/``) that must never redden the gate.
        """
        value = self.raw.get("exclude_paths")
        return list(value) if value is not None else []

    @property
    def src_paths(self) -> list[str]:
        """Repo-relative paths that count as first-party source."""
        value = self.raw.get("src_paths")
        return list(value) if value is not None else list(DEFAULT_SRC_PATHS)

    @property
    def critical_paths(self) -> list[str]:
        """Red-list patterns (globs or prefixes) whose diffs always need a human.

        Blast-radius axis: any change touching these paths parks a mandatory
        ask-user finding, green tools or not. Empty (the default) disables it.
        """
        value = self.raw.get("critical_paths")
        return list(value) if value is not None else list(DEFAULT_CRITICAL_PATHS)

    @property
    def sandbox(self) -> str:
        """Execution backend for the exec tier: ``none`` or ``docker``."""
        return str(self.raw.get("sandbox", DEFAULT_SANDBOX))

    @property
    def sandbox_image(self) -> str:
        """Docker image used when ``sandbox = "docker"``."""
        return str(self.raw.get("sandbox_image", DEFAULT_SANDBOX_IMAGE))

    # ---- sections --------------------------------------------------------

    @property
    def review_provider(self) -> str | None:
        """Cross-model review provider (Layer 6)."""
        value = self.get("review", "provider")
        return str(value) if value is not None else None

    @property
    def review_model(self) -> str | None:
        """Cross-model review model id (Layer 6)."""
        value = self.get("review", "model")
        return str(value) if value is not None else None

    @property
    def diff_cover_min(self) -> float:
        """Minimum diff coverage percentage for the exec tier."""
        return float(self.get("exec", "diff_cover_min", default=DEFAULT_DIFF_COVER_MIN))

    @property
    def fix_max_rounds(self) -> int:
        """Maximum automatic fix rounds before parking remainders."""
        return int(self.get("fix", "max_rounds", default=DEFAULT_FIX_MAX_ROUNDS))

    @property
    def deep_blocking(self) -> bool:
        """Whether surviving mutants above the budget block the gate."""
        return bool(self.get("deep", "blocking", default=DEFAULT_DEEP_BLOCKING))

    @property
    def deep_max_survivors(self) -> int:
        """Survivor budget when ``deep.blocking`` is true."""
        return int(
            self.get("deep", "max_survivors", default=DEFAULT_DEEP_MAX_SURVIVORS)
        )

    # ---- per-runner ------------------------------------------------------

    def runner_enabled(self, name: str) -> bool:
        """Whether a runner is enabled (default: yes)."""
        return bool(self.get("runners", name, "enabled", default=True))

    def runner_args(self, name: str) -> list[str]:
        """Extra CLI args appended to a runner's tool invocation."""
        value = self.get("runners", name, "args", default=[])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ConfigError(f"runners.{name}.args must be a list of strings")
        return list(value)

    def runner_timeout(self, name: str) -> float | None:
        """Per-runner timeout override in seconds, if configured."""
        value = self.get("runners", name, "timeout")
        return float(value) if value is not None else None

    def runner_option(self, name: str, key: str, default: Any = None) -> Any:
        """Arbitrary per-runner option, e.g. ``runners.imports.check_pypi``."""
        return self.get("runners", name, key, default=default)
