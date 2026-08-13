"""gen tier: hypothesis scaffolder + fail-before-fix worktree guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import commit_all, git
from gauntlet.config import Config
from gauntlet.findings import Action, Severity
from gauntlet.runners import RunContext
from gauntlet.runners.hypothesis_gen import (
    HypothesisGenRunner,
    collect_public_functions,
    module_import_path,
    render_scaffold,
    strategy_for,
)
from gauntlet.runners.hypothesis_gen import (
    tested_names as scan_tested_names,  # aliased: pytest would collect the name
)
from gauntlet.runners.pytest_cov import PytestCovRunner, coverage_info_finding


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def make_ctx(
    root: Path,
    files: list[Path],
    tier: str = "gen",
    verify_fails_on: str | None = None,
) -> RunContext:
    return RunContext(
        root=root,
        base_ref="main",
        merge_base="a" * 40,
        head="b" * 7,
        changed_files=files,
        changed_lines={f: {1} for f in files},
        config=Config(),
        tier=tier,
        timeout=60.0,
        verify_fails_on=verify_fails_on,
    )


class TestScaffolder:
    def test_strategy_inference(self) -> None:
        assert strategy_for("int") == ("st.integers()", True)
        assert strategy_for("float") == ("st.floats(allow_nan=False)", True)
        assert strategy_for("str") == ("st.text()", True)
        assert strategy_for("MyThing") == ("st.nothing()", False)
        assert strategy_for(None) == ("st.nothing()", False)

    def test_module_import_path(self) -> None:
        assert (
            module_import_path(Path("src/fixture_pkg/pricing.py"), ["src"])
            == "fixture_pkg.pricing"
        )
        assert module_import_path(Path("pkg/mod.py"), ["src"]) == "pkg.mod"

    def test_collect_public_functions(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(
            "def visible(price: float, qty: int) -> float: ...\n"
            "def _private() -> None: ...\n"
            "class Calc:\n"
            "    def method(self, x: int) -> int: ...\n"
            "    def _hidden(self) -> None: ...\n"
        )
        functions = collect_public_functions(tmp_path, [Path("m.py")])
        assert [(f.qualname, f.params) for f in functions] == [
            ("visible", [("price", "float"), ("qty", "int")]),
            ("Calc.method", [("x", "int")]),
        ]

    def test_tested_names(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("from m import visible\nvisible(1, 2)\n")
        names = scan_tested_names(tmp_path, ["tests"])
        assert "visible" in names
        assert "method" not in names

    def test_scaffold_content(self) -> None:
        funcs = collect_public_functions_from_source(
            "def discounted(price: float, qty: int) -> float: ...\n"
        )
        content = render_scaffold("fixture_pkg.pricing", funcs)
        assert "from hypothesis import given" in content
        assert "from fixture_pkg.pricing import discounted" in content
        assert "@given(price=st.floats(allow_nan=False), qty=st.integers())" in content
        assert "def test_discounted_properties(price, qty) -> None:" in content

    def test_runner_end_to_end(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "__init__.py").touch()
        (src / "mod.py").write_text("def fresh(x: int) -> int:\n    return x\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_other.py").write_text("def test_nothing() -> None: ...\n")

        ctx = make_ctx(tmp_path, [Path("src/pkg/mod.py")])
        findings = HypothesisGenRunner(ctx).run(ctx)

        untested = [f for f in findings if ":untested:" in f.id]
        assert len(untested) == 1
        assert untested[0].severity is Severity.INFO
        assert untested[0].action is Action.NO_OP
        assert "'fresh'" in untested[0].message

        scaffold = tmp_path / ".gauntlet" / "scaffolds" / "test_mod_props.py"
        assert scaffold.is_file()
        assert "from pkg.mod import fresh" in scaffold.read_text()

    def test_tested_function_not_scaffolded(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text("def covered(x: int) -> int:\n    return x\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_mod.py").write_text("covered(1)\n")
        ctx = make_ctx(tmp_path, [Path("src/mod.py")])
        assert HypothesisGenRunner(ctx).run(ctx) == []


def collect_public_functions_from_source(source: str) -> list:  # type: ignore[type-arg]
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pricing.py"
        path.write_text(source)
        return collect_public_functions(Path(tmp), [Path("pricing.py")])


class TestCoverageInfoFinding:
    def test_machine_readable(self) -> None:
        finding = coverage_info_finding(42.5)
        assert finding.severity is Severity.INFO
        assert finding.action is Action.NO_OP
        assert not finding.blocking()
        assert float(finding.evidence) == 42.5
        assert ":coverage:" in finding.id


class TestVerifyFailsOn:
    def test_worktree_pytest_argv(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, [Path("tests/test_new.py")], tier="exec")
        runner = PytestCovRunner(ctx)
        argv = runner.build_worktree_pytest_argv(
            Path("/wt"), [Path("tests/test_new.py")]
        )
        assert argv[1:] == [
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "/wt/tests/test_new.py",
        ]

    def test_no_new_tests_guard(self, tmp_path: Path) -> None:
        ctx = make_ctx(
            tmp_path, [Path("src/mod.py")], tier="exec", verify_fails_on="HEAD~1"
        )
        runner = PytestCovRunner(ctx)
        findings = runner._verify_fails_on(ctx, "HEAD~1", 30.0)
        assert len(findings) == 1
        assert findings[0].severity is Severity.ERROR
        assert findings[0].action is Action.ASK_USER
        assert "not demonstrated" in findings[0].message

    def test_guard_fires_when_tests_pass_on_base(self, scratch_repo: Path) -> None:
        """A test asserting CURRENT behavior passes on base too → guard error."""
        self._make_repo(scratch_repo, test_body="assert add(2, 2) == 5\n")
        ctx = self._ctx(scratch_repo)
        findings = PytestCovRunner(ctx)._verify_fails_on(ctx, "HEAD~1", 60.0)
        assert any(":fails-on-base:" in f.id for f in findings), findings
        guard = next(f for f in findings if ":fails-on-base:" in f.id)
        assert guard.severity is Severity.ERROR
        assert "misguidance-effect guard" in guard.message

    def test_guard_passes_for_fail_before_fix_test(self, scratch_repo: Path) -> None:
        """A test that fails on the buggy base and passes now → info finding."""
        self._make_repo(scratch_repo, test_body="assert add(2, 2) == 4\n")
        ctx = self._ctx(scratch_repo)
        findings = PytestCovRunner(ctx)._verify_fails_on(ctx, "HEAD~1", 60.0)
        assert any(":fails-on-base-ok:" in f.id for f in findings), findings

    @staticmethod
    def _make_repo(root: Path, test_body: str) -> None:
        """base: buggy add (returns sum+1); HEAD: fixed add + new test."""
        (root / "mod.py").write_text("def add(a, b):\n    return a + b + 1\n")
        commit_all(root, "base (buggy)")
        (root / "mod.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_add.py").write_text(
            f"from mod import add\n\n\ndef test_add() -> None:\n    {test_body}"
        )
        git(root, "add", "-A")
        commit_all(root, "fix + test")

    @staticmethod
    def _ctx(root: Path) -> RunContext:
        return RunContext(
            root=root,
            base_ref="HEAD~1",
            merge_base="x" * 40,
            head="y" * 7,
            changed_files=[Path("mod.py"), Path("tests/test_add.py")],
            changed_lines={},
            config=Config(),
            tier="exec",
            timeout=60.0,
            verify_fails_on="HEAD~1",
        )
