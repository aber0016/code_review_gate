"""Changed files and changed line ranges vs a base ref (§3.5 of the plan).

All diff scoping starts from ``git merge-base <base> HEAD`` and compares the
*working tree* against it, so uncommitted fixes made during a fix round are
visible to the gate. Untracked (but not ignored) files count as fully changed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 60.0

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    """Raised when a required git operation fails."""


def _git(root: Path, *args: str) -> str:
    """Run a git command rooted at ``root`` and return stdout.

    Raises:
        GitError: on non-zero exit or timeout.
    """
    argv = ["git", "-C", str(root), *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise GitError(f"git {' '.join(args)} timed out") from exc
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        raise GitError("git executable not found") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout


def repo_root(start: Path | None = None) -> Path:
    """Resolve the repository toplevel for ``start`` (default: cwd)."""
    base = start if start is not None else Path.cwd()
    out = _git(base, "rev-parse", "--show-toplevel")
    return Path(out.strip())


def git_dir(root: Path) -> Path:
    """Resolve the .git directory (worktree-aware)."""
    out = _git(root, "rev-parse", "--git-dir").strip()
    path = Path(out)
    return path if path.is_absolute() else root / path


def ref_exists(root: Path, ref: str) -> bool:
    """Whether ``ref`` resolves to a commit in this repository."""
    try:
        _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError:
        return False
    return True


def resolve_base(
    root: Path, cli_base: str | None = None, config_base: str | None = None
) -> str:
    """Resolve the base ref: CLI flag → config → ``origin/main`` → ``main``.

    An explicitly provided base (CLI or config) that does not resolve is an
    error rather than a silent fallback — gating against the wrong base is
    worse than failing closed.

    Raises:
        GitError: when the chosen candidate does not resolve to a commit.
    """
    for source, cand in (("--base", cli_base), (".gauntlet.toml base", config_base)):
        if cand:
            if ref_exists(root, cand):
                return cand
            raise GitError(f"base ref {cand!r} (from {source}) does not exist")
    for cand in ("origin/main", "main"):
        if ref_exists(root, cand):
            return cand
    raise GitError("no usable base ref (tried origin/main, main)")


def merge_base(root: Path, base: str) -> str:
    """SHA of ``git merge-base <base> HEAD``."""
    return _git(root, "merge-base", base, "HEAD").strip()


def head_rev(root: Path) -> str:
    """Short SHA of HEAD."""
    return _git(root, "rev-parse", "--short", "HEAD").strip()


def _unquote_git_path(raw: str) -> str:
    """Undo git's C-style quoting of unusual paths in diff headers."""
    if raw.startswith('"') and raw.endswith('"'):
        return (
            raw[1:-1]
            .encode("latin-1", "backslashreplace")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8", "replace")
        )
    return raw


def _untracked_files(root: Path) -> list[Path]:
    """Untracked, non-ignored files (repo-relative)."""
    out = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return [Path(p) for p in out.split("\0") if p]


def _count_lines(path: Path) -> int:
    """Line count of a text file; a missing trailing newline still counts."""
    content = path.read_text(errors="replace")
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def changed_files(root: Path, base: str) -> list[Path]:
    """Repo-relative files changed between merge-base and the working tree.

    Includes untracked files; excludes deleted files (a finding cannot be
    attached to a file that no longer exists).
    """
    mb = merge_base(root, base)
    out = _git(root, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=d", mb)
    files = {Path(p) for p in out.split("\0") if p}
    files.update(_untracked_files(root))
    return sorted(p for p in files if (root / p).is_file())


def changed_lines(root: Path, base: str) -> dict[Path, set[int]]:
    """New-side changed line numbers per file, from ``git diff -U0``.

    Untracked files contribute all of their lines. Files whose diff contains
    only deletions map to an empty set (they are still *changed files*, but
    have no new lines to anchor findings to).
    """
    mb = merge_base(root, base)
    out = _git(
        root,
        "diff",
        "--unified=0",
        "--no-color",
        "--no-renames",
        "--diff-filter=d",
        mb,
    )
    lines: dict[Path, set[int]] = {}
    current: Path | None = None
    for raw in out.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].split("\t", 1)[0]
            if target == "/dev/null":  # pragma: no cover - excluded by filter
                current = None
                continue
            target = _unquote_git_path(target)
            current = Path(target[2:]) if target.startswith("b/") else Path(target)
            lines.setdefault(current, set())
            continue
        if current is None:
            continue
        match = _HUNK_RE.match(raw)
        if match:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            lines[current].update(range(start, start + count))
    for path in _untracked_files(root):
        full = root / path
        if full.is_file():
            lines[path] = set(range(1, _count_lines(full) + 1))
    return {p: s for p, s in lines.items() if (root / p).is_file()}
