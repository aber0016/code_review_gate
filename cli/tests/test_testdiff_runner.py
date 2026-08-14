"""testdiff runner: diff-level test-weakening detection."""

from __future__ import annotations

from pathlib import Path

from conftest import commit_all, git
from gauntlet.cli import build_context
from gauntlet.config import Config
from gauntlet.findings import Action, Finding, Severity
from gauntlet.runners.testdiff import (
    WeakenedTestsRunner,
    defined_function_names,
    removed_test_names,
    weakening_code,
)


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def run_testdiff(root: Path, base: str, config: Config | None = None) -> list[Finding]:
    cfg = config or Config()
    ctx = build_context(root, cfg, base, "fast", fix=False)
    return WeakenedTestsRunner(ctx).run(ctx)


TWO_TESTS = "def test_one():\n    assert 1 == 1\n\ndef test_two():\n    assert 2 == 2\n"


class TestClassifiers:
    def test_weakening_code(self) -> None:
        assert weakening_code("    @pytest.mark.skip(reason='x')") == "skip-added"
        assert weakening_code("    @pytest.mark.skipif(sys.platform == 'win32')") == (
            "skip-added"
        )
        assert weakening_code("    pytest.skip('slow')") == "skip-added"
        assert weakening_code("    @unittest.skip('x')") == "skip-added"
        assert weakening_code("    assert True") == "trivial-assert"
        assert weakening_code("    assert True, 'msg'") == "trivial-assert"
        assert weakening_code("    assert 1") == "trivial-assert"
        # Legitimate patterns stay silent:
        assert weakening_code("    pytest.importorskip('numpy')") is None
        assert weakening_code("    @pytest.mark.xfail(strict=True)") is None
        assert weakening_code("    assert True_ish") is None
        assert weakening_code("    assert result") is None

    def test_removed_test_names(self) -> None:
        removed = [
            (1, "def test_gone():"),
            (2, "    assert x"),
            (3, "async def test_async_gone():"),
            (4, "def helper():"),
        ]
        assert removed_test_names(removed) == ["test_gone", "test_async_gone"]

    def test_defined_function_names(self) -> None:
        names = defined_function_names("def test_a():\n    pass\n")
        assert names == {"test_a"}
        assert defined_function_names("def broken(:\n") is None


class TestRunner:
    def test_removed_test_function(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_a.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        write(scratch_repo, "tests/test_a.py", TWO_TESTS.split("\n\n")[0] + "\n")
        findings = run_testdiff(scratch_repo, base)
        removed = [f for f in findings if ":removed-test:" in f.id]
        assert [f.id for f in removed] == [
            "testdiff:removed-test:tests/test_a.py:test_two"
        ]
        assert removed[0].severity is Severity.WARNING
        assert removed[0].action is Action.ASK_USER

    def test_reordered_test_is_silent(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_a.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        parts = TWO_TESTS.split("\n\n")
        write(scratch_repo, "tests/test_a.py", parts[1] + "\n\n" + parts[0])
        findings = run_testdiff(scratch_repo, base)
        assert [f for f in findings if ":removed-test:" in f.id] == []

    def test_skip_and_trivial_assert_added(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_a.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        write(
            scratch_repo,
            "tests/test_a.py",
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='later')\n"
            "def test_one():\n"
            "    assert True\n"
            "\n"
            "def test_two():\n"
            "    assert 2 == 2\n",
        )
        findings = run_testdiff(scratch_repo, base)
        codes = sorted(f.id.split(":")[1] for f in findings if f.blocking())
        assert codes == ["skip-added", "trivial-assert"]

    def test_deleted_test_file(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_gone.py", TWO_TESTS)
        write(scratch_repo, "keep.py", "x = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "tests" / "test_gone.py").unlink()
        findings = run_testdiff(scratch_repo, base)
        assert [f.id for f in findings] == [
            "testdiff:deleted-file:tests/test_gone.py:0"
        ]

    def test_rename_within_test_paths_is_silent(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_a.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        git(scratch_repo, "mv", "tests/test_a.py", "tests/test_renamed.py")
        commit_all(scratch_repo, "rename")
        assert run_testdiff(scratch_repo, base) == []

    def test_moved_out_of_test_paths(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_a.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "helpers").mkdir()
        git(scratch_repo, "mv", "tests/test_a.py", "helpers/checks.py")
        commit_all(scratch_repo, "move out")
        findings = run_testdiff(scratch_repo, base)
        assert [f.id for f in findings] == [
            "testdiff:moved-out:tests/test_a.py:helpers/checks.py"
        ]

    def test_assert_count_drop_is_advisory(self, scratch_repo: Path) -> None:
        write(
            scratch_repo,
            "tests/test_a.py",
            "def test_one():\n    assert 1\n    assert 2 == 2\n    assert 3 == 3\n",
        )
        base = commit_all(scratch_repo, "base")
        write(scratch_repo, "tests/test_a.py", "def test_one():\n    assert 2 == 2\n")
        findings = run_testdiff(scratch_repo, base)
        advisory = [f for f in findings if ":assert-count:" in f.id]
        assert len(advisory) == 1
        assert advisory[0].severity is Severity.INFO
        assert advisory[0].action is Action.NO_OP
        assert not advisory[0].blocking()

    def test_critical_path_escalates_to_error(self, scratch_repo: Path) -> None:
        write(scratch_repo, "tests/test_auth.py", TWO_TESTS)
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "tests" / "test_auth.py").unlink()
        config = Config(raw={"critical_paths": ["tests/test_auth.py"]})
        findings = run_testdiff(scratch_repo, base, config)
        assert findings[0].severity is Severity.ERROR
        assert "[critical path]" in findings[0].message

    def test_non_test_files_ignored(self, scratch_repo: Path) -> None:
        write(scratch_repo, "src/mod.py", "def calc():\n    assert True\n")
        base = commit_all(scratch_repo, "base")
        write(scratch_repo, "src/mod.py", "def calc():\n    assert True\n    x = 1\n")
        assert run_testdiff(scratch_repo, base) == []

    def test_excluded_paths_are_silent(self, scratch_repo: Path) -> None:
        write(scratch_repo, "fixture/tests/test_planted.py", TWO_TESTS)
        write(scratch_repo, "keep.py", "x = 1\n")
        base = commit_all(scratch_repo, "base")
        (scratch_repo / "fixture" / "tests" / "test_planted.py").unlink()
        config = Config(
            raw={"test_paths": ["tests", "fixture/tests"], "exclude_paths": ["fixture"]}
        )
        assert run_testdiff(scratch_repo, base, config) == []
