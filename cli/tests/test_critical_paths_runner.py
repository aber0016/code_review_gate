"""critical-paths runner: the blast-radius red-list."""

from __future__ import annotations

from pathlib import Path

from conftest import commit_all
from gauntlet.cli import build_context
from gauntlet.config import Config
from gauntlet.findings import Action, Finding, Severity
from gauntlet.runners.critical_paths import CriticalPathsRunner


def run_gate(root: Path, base: str, config: Config) -> list[Finding]:
    ctx = build_context(root, config, base, "fast", fix=False)
    return CriticalPathsRunner(ctx).run(ctx)


class TestCriticalPaths:
    def test_empty_config_is_silent(self, scratch_repo: Path) -> None:
        (scratch_repo / "src").mkdir()
        (scratch_repo / "src" / "auth.py").write_text("x = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "src" / "auth.py").write_text("x = 2\n")
        assert run_gate(scratch_repo, base, Config()) == []

    def test_touched_prefix_and_glob(self, scratch_repo: Path) -> None:
        (scratch_repo / "src" / "auth").mkdir(parents=True)
        (scratch_repo / "pkg" / "migrations").mkdir(parents=True)
        (scratch_repo / "src" / "auth" / "login.py").write_text("a = 1\n")
        (scratch_repo / "pkg" / "migrations" / "m1.py").write_text("b = 1\n")
        (scratch_repo / "other.py").write_text("c = 1\n")
        base = commit_all(scratch_repo, "base")
        for name in ("src/auth/login.py", "pkg/migrations/m1.py", "other.py"):
            path = scratch_repo / name
            path.write_text(path.read_text() + "changed = True\n")
        config = Config(raw={"critical_paths": ["src/auth", "**/migrations/**"]})
        findings = run_gate(scratch_repo, base, config)
        assert sorted(f.file for f in findings) == [
            "pkg/migrations/m1.py",
            "src/auth/login.py",
        ]
        for finding in findings:
            assert finding.severity is Severity.WARNING
            assert finding.action is Action.ASK_USER
            assert finding.blocking()
            assert "human review required" in finding.message
            assert finding.id.startswith("critical-paths:touched:")

    def test_one_finding_per_file(self, scratch_repo: Path) -> None:
        (scratch_repo / "src" / "auth").mkdir(parents=True)
        (scratch_repo / "src" / "auth" / "login.py").write_text("a = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "src" / "auth" / "login.py").write_text("a = 2\nb = 3\n")
        config = Config(raw={"critical_paths": ["src/auth", "src/auth/*.py"]})
        findings = run_gate(scratch_repo, base, config)
        assert len(findings) == 1  # first matching pattern wins, no duplicates

    def test_deleted_critical_file(self, scratch_repo: Path) -> None:
        (scratch_repo / "src" / "auth").mkdir(parents=True)
        (scratch_repo / "src" / "auth" / "gone.py").write_text("a = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "src" / "auth" / "gone.py").unlink()
        config = Config(raw={"critical_paths": ["src/auth"]})
        findings = run_gate(scratch_repo, base, config)
        assert [f.id for f in findings] == [
            "critical-paths:deleted:src/auth/gone.py:src/auth"
        ]
        assert findings[0].blocking()

    def test_excluded_deletion_is_silent(self, scratch_repo: Path) -> None:
        (scratch_repo / "fixture" / "auth").mkdir(parents=True)
        (scratch_repo / "fixture" / "auth" / "gone.py").write_text("a = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "fixture" / "auth" / "gone.py").unlink()
        config = Config(
            raw={"critical_paths": ["**/auth/**"], "exclude_paths": ["fixture"]}
        )
        assert run_gate(scratch_repo, base, config) == []
