"""The custom imports runner: collection, classification, degradation."""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.config import Config
from gauntlet.findings import Action, Severity
from gauntlet.runners import RunContext
from gauntlet.runners.imports import (
    EnvInfo,
    ImportsRunner,
    collect_imports,
    declared_distributions,
    first_party_names,
)


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def make_ctx(root: Path, files: list[Path]) -> RunContext:
    return RunContext(
        root=root,
        base_ref="main",
        merge_base="a" * 40,
        head="b" * 7,
        changed_files=files,
        changed_lines={f: {1} for f in files},
        config=Config(),
        tier="fast",
        timeout=30.0,
    )


class TestCollectImports:
    def test_import_forms(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(
            "import requestz\n"
            "import os.path\n"
            "from collections import OrderedDict\n"
            "from . import sibling\n"
            "from .relative import thing\n"
            "import requestz.submodule\n"  # dedup with line 1
        )
        sites = collect_imports(tmp_path, [Path("m.py")])
        assert [(s.module, s.line) for s in sites] == [
            ("requestz", 1),
            ("os", 2),
            ("collections", 3),
        ]

    def test_syntax_error_files_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.py").write_text("def broken(:\n")
        assert collect_imports(tmp_path, [Path("bad.py")]) == []


class TestFirstParty:
    def test_src_layout_and_root_modules(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "mypkg").mkdir(parents=True)
        (tmp_path / "src" / "mypkg" / "__init__.py").touch()
        (tmp_path / "standalone.py").touch()
        (tmp_path / "namespace_pkg").mkdir()
        (tmp_path / "namespace_pkg" / "mod.py").touch()

        names = first_party_names(tmp_path, ["src"])
        assert {"mypkg", "standalone", "namespace_pkg"}.issubset(names)


class TestDeclared:
    def test_pyproject_and_lock(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "my-proj"\n'
            'dependencies = ["requests>=2", "Flask_Login"]\n'
            "[project.optional-dependencies]\n"
            'test = ["pytest"]\n'
            "[dependency-groups]\n"
            'dev = ["mypy"]\n'
        )
        (tmp_path / "uv.lock").write_text(
            'version = 1\n[[package]]\nname = "urllib3"\nversion = "2.0"\n'
        )
        declared = declared_distributions(tmp_path)
        assert declared is not None
        assert {
            "my-proj",
            "requests",
            "flask-login",
            "pytest",
            "mypy",
            "urllib3",
        }.issubset(declared)

    def test_no_manifests_returns_none(self, tmp_path: Path) -> None:
        assert declared_distributions(tmp_path) is None


class TestClassification:
    def make_runner(self, tmp_path: Path, env: EnvInfo) -> ImportsRunner:
        runner = ImportsRunner(make_ctx(tmp_path, [Path("mod.py")]))

        def fake_probe(ctx: RunContext, timeout: float) -> EnvInfo:
            return env

        runner._probe_env = fake_probe  # type: ignore[method-assign]
        return runner

    def test_hallucinated_and_undeclared(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text(
            "import os\nimport requestz\nimport requests\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = []\n'
        )
        env = EnvInfo(stdlib={"os"}, dist_map={"requests": ["requests"]})
        runner = self.make_runner(tmp_path, env)
        findings = runner.run(runner.ctx)

        by_id = {f.id: f for f in findings}
        hallucinated = by_id["imports:missing-dist:mod.py:requestz"]
        assert hallucinated.severity is Severity.ERROR
        assert hallucinated.action is Action.ASK_USER
        assert "requestz" in hallucinated.message
        assert "hallucinated" in hallucinated.message

        undeclared = by_id["imports:undeclared:mod.py:requests"]
        assert undeclared.severity is Severity.WARNING
        assert "undeclared" in undeclared.message

    def test_declared_dependency_is_clean(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("import requests\n")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["requests"]\n'
        )
        env = EnvInfo(stdlib=set(), dist_map={"requests": ["requests"]})
        runner = self.make_runner(tmp_path, env)
        assert runner.run(runner.ctx) == []

    def test_no_manifest_skips_undeclared_check(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("import requests\n")
        env = EnvInfo(stdlib=set(), dist_map={"requests": ["requests"]})
        runner = self.make_runner(tmp_path, env)
        assert runner.run(runner.ctx) == []

    def test_first_party_src_module_is_clean(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "mypkg").mkdir(parents=True)
        (tmp_path / "src" / "mypkg" / "__init__.py").touch()
        (tmp_path / "mod.py").write_text("import mypkg\n")
        env = EnvInfo(stdlib=set(), dist_map={})
        runner = self.make_runner(tmp_path, env)
        assert runner.run(runner.ctx) == []

    def test_sibling_module_is_first_party(self, tmp_path: Path) -> None:
        """pytest-style conftest/test-helper imports must not be flagged."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "conftest.py").touch()
        (tmp_path / "tests" / "test_x.py").write_text("from conftest import helper\n")
        ctx = make_ctx(tmp_path, [Path("tests/test_x.py")])
        runner = ImportsRunner(ctx)
        env = EnvInfo(stdlib=set(), dist_map={})

        def fake_probe(ctx: RunContext, timeout: float) -> EnvInfo:
            return env

        runner._probe_env = fake_probe  # type: ignore[method-assign]
        assert runner.run(ctx) == []

    def test_broken_interpreter_degrades_visibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "mod.py").write_text("import requestz\n")
        ctx = make_ctx(tmp_path, [Path("mod.py")])
        monkeypatch.setattr(ctx, "repo_python", lambda: Path("/nonexistent/python"))
        findings = ImportsRunner(ctx).run(ctx)
        assert len(findings) == 1
        assert findings[0].severity is Severity.WARNING
        assert findings[0].action is Action.ASK_USER
        assert "could not query the repo interpreter" in findings[0].message
