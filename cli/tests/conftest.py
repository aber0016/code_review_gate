"""Shared test helpers: scratch git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(root: Path, *args: str) -> str:
    """Run git in a scratch repo with deterministic identity/signing config."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=gauntlet-test",
            "-c",
            "user.email=gauntlet-test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """An initialized empty git repo on branch `main`."""
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def commit_all(root: Path, message: str) -> str:
    """Stage everything and commit; returns the commit SHA."""
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()
