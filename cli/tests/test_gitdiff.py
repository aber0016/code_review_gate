"""gitdiff behavior against real scratch repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import commit_all, git
from gauntlet import gitdiff


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestChangedLines:
    def test_known_diff_line_sets(self, scratch_repo: Path) -> None:
        """Modify line 3 of 5; append two lines. Expect {3, 6, 7}."""
        write(scratch_repo, "a.py", "l1\nl2\nl3\nl4\nl5\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "a.py", "l1\nl2\nl3-changed\nl4\nl5\nl6\nl7\n")
        commit_all(scratch_repo, "change")

        lines = gitdiff.changed_lines(scratch_repo, "HEAD~1")
        assert lines == {Path("a.py"): {3, 6, 7}}

    def test_uncommitted_changes_are_visible(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "l1\nl2\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "a.py", "l1\nl2-edited\n")  # not committed

        lines = gitdiff.changed_lines(scratch_repo, "HEAD")
        assert lines == {Path("a.py"): {2}}

    def test_untracked_file_counts_fully(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "l1\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "new.py", "x = 1\ny = 2\nz = 3")  # no trailing newline

        lines = gitdiff.changed_lines(scratch_repo, "HEAD")
        assert lines[Path("new.py")] == {1, 2, 3}

    def test_deleted_file_excluded(self, scratch_repo: Path) -> None:
        write(scratch_repo, "gone.py", "x\n")
        write(scratch_repo, "kept.py", "y\n")
        commit_all(scratch_repo, "base")
        (scratch_repo / "gone.py").unlink()
        write(scratch_repo, "kept.py", "y2\n")
        commit_all(scratch_repo, "change")

        assert gitdiff.changed_files(scratch_repo, "HEAD~1") == [Path("kept.py")]
        assert Path("gone.py") not in gitdiff.changed_lines(scratch_repo, "HEAD~1")

    def test_pure_deletion_hunk_yields_empty_set(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "l1\nl2\nl3\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "a.py", "l1\nl3\n")  # delete line 2 only
        commit_all(scratch_repo, "delete-only")

        assert gitdiff.changed_lines(scratch_repo, "HEAD~1") == {Path("a.py"): set()}
        assert gitdiff.changed_files(scratch_repo, "HEAD~1") == [Path("a.py")]


class TestResolveBase:
    def test_explicit_missing_base_errors(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "x\n")
        commit_all(scratch_repo, "base")
        with pytest.raises(gitdiff.GitError, match="does not exist"):
            gitdiff.resolve_base(scratch_repo, cli_base="nope")

    def test_falls_back_to_main_without_origin(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "x\n")
        commit_all(scratch_repo, "base")
        assert gitdiff.resolve_base(scratch_repo) == "main"

    def test_cli_beats_config(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "x\n")
        commit_all(scratch_repo, "base")
        git(scratch_repo, "branch", "release")
        assert (
            gitdiff.resolve_base(scratch_repo, cli_base="release", config_base="main")
            == "release"
        )


class TestNameStatus:
    def test_modify_add_delete(self, scratch_repo: Path) -> None:
        write(scratch_repo, "mod.py", "a\n")
        write(scratch_repo, "gone.py", "b\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "mod.py", "a2\n")
        write(scratch_repo, "new.py", "c\n")
        (scratch_repo / "gone.py").unlink()
        commit_all(scratch_repo, "change")

        entries = gitdiff.name_status(scratch_repo, "HEAD~1")
        assert sorted(entries) == [
            ("A", Path("new.py"), None),
            ("D", Path("gone.py"), None),
            ("M", Path("mod.py"), None),
        ]

    def test_rename_carries_both_paths(self, scratch_repo: Path) -> None:
        write(scratch_repo, "old.py", "line\n" * 20)
        commit_all(scratch_repo, "base")
        git(scratch_repo, "mv", "old.py", "renamed.py")
        commit_all(scratch_repo, "rename")

        entries = gitdiff.name_status(scratch_repo, "HEAD~1")
        assert entries == [("R", Path("old.py"), Path("renamed.py"))]

    def test_uncommitted_deletion_visible(self, scratch_repo: Path) -> None:
        write(scratch_repo, "gone.py", "b\n")
        commit_all(scratch_repo, "base")
        (scratch_repo / "gone.py").unlink()

        assert gitdiff.name_status(scratch_repo, "HEAD") == [
            ("D", Path("gone.py"), None)
        ]


class TestRemovedLines:
    def test_old_side_numbering(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "l1\nl2\nl3\nl4\nl5\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "a.py", "l1\nl3\nl5\nl6\n")

        removed = gitdiff.removed_lines(scratch_repo, "HEAD")
        assert removed == {Path("a.py"): [(2, "l2"), (4, "l4")]}

    def test_pure_deletion_and_multi_hunk(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "keep\ndrop1\nkeep\ndrop2\ndrop3\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "a.py", "keep\nkeep\n")

        removed = gitdiff.removed_lines(scratch_repo, "HEAD")
        assert removed == {Path("a.py"): [(2, "drop1"), (4, "drop2"), (5, "drop3")]}

    def test_added_only_files_absent(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "x\n")
        commit_all(scratch_repo, "base")
        write(scratch_repo, "b.py", "new\n")
        commit_all(scratch_repo, "add")

        assert gitdiff.removed_lines(scratch_repo, "HEAD~1") == {}


class TestMergeBase:
    def test_merge_base_of_branch(self, scratch_repo: Path) -> None:
        write(scratch_repo, "a.py", "x\n")
        base_sha = commit_all(scratch_repo, "base")
        git(scratch_repo, "checkout", "-q", "-b", "feature")
        write(scratch_repo, "a.py", "x2\n")
        commit_all(scratch_repo, "feature-work")

        assert gitdiff.merge_base(scratch_repo, "main") == base_sha
        assert gitdiff.head_rev(scratch_repo)
