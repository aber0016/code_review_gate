"""install-hook subcommand: hook content, permissions, overwrite safety."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gauntlet.cli import HOOK_SCRIPT, main
from gauntlet.findings import EXIT_BLOCKING, EXIT_OK


@pytest.fixture
def in_scratch_repo(scratch_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(scratch_repo)
    return scratch_repo


class TestInstallHook:
    def test_installs_executable_hook(self, in_scratch_repo: Path) -> None:
        assert main(["install-hook"]) == EXIT_OK
        hook = in_scratch_repo / ".git" / "hooks" / "pre-push"
        assert hook.is_file()
        assert os.access(hook, os.X_OK)
        content = hook.read_text()
        assert content == HOOK_SCRIPT
        assert "--base @{push}" in content
        assert "--base origin/main" in content
        assert "--no-verify" in content  # documented escape hatch

    def test_idempotent_reinstall(self, in_scratch_repo: Path) -> None:
        assert main(["install-hook"]) == EXIT_OK
        assert main(["install-hook"]) == EXIT_OK

    def test_refuses_to_clobber_foreign_hook(self, in_scratch_repo: Path) -> None:
        hook = in_scratch_repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho custom hook\n")
        assert main(["install-hook"]) == EXIT_BLOCKING
        assert "custom hook" in hook.read_text()
        assert main(["install-hook", "--force"]) == EXIT_OK
        assert hook.read_text() == HOOK_SCRIPT
