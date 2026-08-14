"""testdiff runner — Layer 4/5: diff-level test-weakening detection.

The test-lock (§7.1) only guards live fix rounds inside pi; in CI, pre-push,
and plain sessions nothing stops a diff that deletes a test, adds a skip mark,
or neuters an assert — the suite stays green while the oracle gets weaker.
This runner makes that a finding. Heuristics stay conservative: legitimate
patterns (``pytest.importorskip``, ``xfail(strict=True)``, renames within the
test tree, assert consolidation) are exempt or advisory-only.

Findings escalate from warning to error when the weakened file also matches a
``critical_paths`` pattern — weakening a red-list oracle is the one change
class the article-era deny rules hard-block.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from gauntlet import gitdiff
from gauntlet.config import Config, matches_any
from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext

_DEF_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(")
_SKIP_RE = re.compile(
    r"@\s*pytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail)\s*\("
    r"|@\s*unittest\.skip"
)
_STRICT_RE = re.compile(r"strict\s*=\s*True")
_TRIVIAL_ASSERT_RE = re.compile(r"^\s*assert\s+(?:True|1)\s*(?:,|#|$)")
_ASSERT_RE = re.compile(r"^\s*assert\b")

_FIX_HINT = (
    "Weakening the test oracle needs human sign-off: restore the test, or "
    "have a human approve why this check may go."
)


def is_test_file(path: Path, config: Config) -> bool:
    """Whether a repo-relative path is in-scope test code (and not excluded)."""
    return (
        path.suffix == ".py"
        and matches_any(path, config.test_paths)
        and not matches_any(path, config.exclude_paths)
    )


def removed_test_names(removed: list[tuple[int, str]]) -> list[str]:
    """Test-function names whose ``def`` line was removed."""
    names: list[str] = []
    for _line, text in removed:
        match = _DEF_TEST_RE.match(text)
        if match:
            names.append(match.group(1))
    return names


def defined_function_names(source: str) -> set[str] | None:
    """All function/method names in a module, or None if it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def weakening_code(text: str) -> str | None:
    """Classify one added line: ``skip-added``, ``trivial-assert``, or None."""
    match = _SKIP_RE.search(text)
    if match and not ("xfail" in match.group(0) and _STRICT_RE.search(text)):
        return "skip-added"
    if _TRIVIAL_ASSERT_RE.match(text):
        return "trivial-assert"
    return None


class WeakenedTestsRunner:
    """Flags diffs that weaken the test suite instead of the product code."""

    name = "testdiff"
    layer = Layer.TEST_GEN
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: pure git-diff + AST analysis, no external tool."""
        return True

    def _finding(
        self, code: str, file: str, location: int | str, message: str, evidence: str
    ) -> Finding:
        critical = matches_any(Path(file), self.ctx.config.critical_paths)
        return Finding(
            id=make_id(self.name, code, file, location),
            layer=self.layer,
            tool=self.name,
            severity=Severity.ERROR if critical else Severity.WARNING,
            action=Action.ASK_USER,
            file=file,
            line=location if isinstance(location, int) else 0,
            message=message + (" [critical path]" if critical else ""),
            evidence=evidence,
            fix_hint=_FIX_HINT,
        )

    def run(self, ctx: RunContext) -> list[Finding]:
        """Scan the diff for deleted, moved, skipped, or neutered tests."""
        config = ctx.config
        findings: list[Finding] = []

        for status, old_path, new_path in gitdiff.name_status(ctx.root, ctx.base_ref):
            if not is_test_file(old_path, config):
                continue
            if status == "D":
                findings.append(
                    self._finding(
                        "deleted-file", str(old_path), 0, "test file deleted", ""
                    )
                )
            elif status == "R" and new_path is not None:
                if not is_test_file(new_path, config):
                    findings.append(
                        self._finding(
                            "moved-out",
                            str(old_path),
                            str(new_path),
                            f"test file moved out of test paths (now {new_path})",
                            "",
                        )
                    )

        removed = gitdiff.removed_lines(ctx.root, ctx.base_ref)
        for old_path, removed_entries in sorted(removed.items()):
            full = ctx.root / old_path
            if not is_test_file(old_path, config) or not full.is_file():
                continue
            try:
                content = full.read_text(errors="replace")
            except OSError:
                continue
            names = removed_test_names(removed_entries)
            if names:
                current = defined_function_names(content)
                if current is not None:
                    for name in names:
                        if name not in current:
                            findings.append(
                                self._finding(
                                    "removed-test",
                                    str(old_path),
                                    name,
                                    f"test function {name} removed",
                                    f"def {name} no longer exists in the file",
                                )
                            )
            # Assert-count advisory lives here, not in the added-lines loop:
            # a pure-deletion diff has an empty new-side line set and would
            # never be seen there.
            lines = ctx.changed_lines.get(old_path, set())
            added_asserts = sum(
                1
                for lineno, text in enumerate(content.splitlines(), start=1)
                if lineno in lines and _ASSERT_RE.match(text)
            )
            removed_asserts = sum(
                1 for _line, text in removed_entries if _ASSERT_RE.match(text)
            )
            if removed_asserts > added_asserts:
                findings.append(
                    Finding(
                        id=make_id(self.name, "assert-count", str(old_path)),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.INFO,
                        action=Action.NO_OP,
                        file=str(old_path),
                        line=0,
                        message=(
                            f"net assertion count dropped "
                            f"(-{removed_asserts}/+{added_asserts}) — advisory; "
                            "refactors legitimately consolidate asserts"
                        ),
                        evidence=f"-{removed_asserts}/+{added_asserts}",
                    )
                )

        for file, lines in sorted(ctx.changed_lines.items()):
            if not lines or not is_test_file(file, config):
                continue
            try:
                content = (ctx.root / file).read_text(errors="replace")
            except OSError:
                continue
            for lineno, text in enumerate(content.splitlines(), start=1):
                if lineno not in lines:
                    continue
                code = weakening_code(text)
                if code is not None:
                    findings.append(
                        self._finding(
                            code,
                            str(file),
                            lineno,
                            "skip/xfail added to test"
                            if code == "skip-added"
                            else "trivial assertion added (always passes)",
                            text.strip()[:200],
                        )
                    )
        return findings
