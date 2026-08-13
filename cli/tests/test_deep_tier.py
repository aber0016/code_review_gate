"""deep tier: mutmut runner parsing, config generation, tier gating."""

from __future__ import annotations

from pathlib import Path

import pytest

import gauntlet.runners
from gauntlet.config import Config
from gauntlet.findings import Action, Severity
from gauntlet.runners import RunContext, runners_for_tier
from gauntlet.runners.mutmut_diff import (
    MutmutDiffRunner,
    diff_first_changed_line,
    module_to_file,
    mutant_module,
    parse_results,
    render_setup_cfg,
)


@pytest.fixture(autouse=True)
def _no_ambient_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def make_ctx(root: Path, raw_config: dict[str, object] | None = None) -> RunContext:
    return RunContext(
        root=root,
        base_ref="main",
        merge_base="a" * 40,
        head="b" * 7,
        changed_files=[Path("src/fixture_pkg/pricing.py")],
        changed_lines={Path("src/fixture_pkg/pricing.py"): {3}},
        config=Config(raw=raw_config or {}),
        tier="deep",
        timeout=1800.0,
    )


class TestParsing:
    def test_parse_results(self) -> None:
        stdout = (
            "    fixture_pkg.pricing.x_discounted__mutmut_1: survived\n"
            "    fixture_pkg.pricing.x_discounted__mutmut_2: survived\n"
            "    fixture_pkg.other.x_f__mutmut_1: killed\n"
            "random noise\n"
        )
        assert parse_results(stdout) == [
            "fixture_pkg.pricing.x_discounted__mutmut_1",
            "fixture_pkg.pricing.x_discounted__mutmut_2",
        ]

    def test_parse_results_empty_when_all_killed(self) -> None:
        assert parse_results("") == []

    def test_mutant_module(self) -> None:
        assert (
            mutant_module("fixture_pkg.pricing.x_discounted__mutmut_1")
            == "fixture_pkg.pricing"
        )

    def test_module_to_file(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "fixture_pkg"
        target.mkdir(parents=True)
        (target / "pricing.py").touch()
        assert (
            module_to_file("fixture_pkg.pricing", ["src"], tmp_path)
            == "src/fixture_pkg/pricing.py"
        )
        assert module_to_file("nope.missing", ["src"], tmp_path) == ""

    def test_diff_first_changed_line(self) -> None:
        diff = (
            "# name: survived\n"
            "--- src/fixture_pkg/pricing.py\n"
            "+++ src/fixture_pkg/pricing.py\n"
            "@@ -1,5 +1,5 @@\n"
            " def discounted(price: float, qty: int) -> float:\n"
            "     # BUG comment\n"
            "-    if qty <= 10:\n"
            "+    if qty < 10:\n"
            "     return price\n"
        )
        assert diff_first_changed_line(diff) == 3


class TestSetupCfg:
    def test_scoped_config(self) -> None:
        content = render_setup_cfg(
            None, ["src"], ["src/fixture_pkg/pricing.py", "src/fixture_pkg/hasher.py"]
        )
        assert "[mutmut]" in content
        assert "source_paths = src" in content
        assert "src/fixture_pkg/pricing.py" in content
        assert "src/fixture_pkg/hasher.py" in content

    def test_existing_cfg_merged_and_mutmut_replaced(self) -> None:
        existing = "[flake8]\nmax-line-length = 100\n[mutmut]\nsource_paths = lib\n"
        content = render_setup_cfg(existing, ["src"], ["src/a.py"])
        assert "[flake8]" in content
        assert "max-line-length = 100" in content
        assert "lib" not in content
        assert "source_paths = src" in content


class TestRunner:
    def test_argv(self, tmp_path: Path) -> None:
        runner = MutmutDiffRunner(make_ctx(tmp_path))
        assert runner.build_argv("run")[-1] == "run"
        assert runner.build_argv("results")[-1] == "results"
        assert runner.build_argv("show", ["m1"])[-2:] == ["show", "m1"]

    def test_non_posix_degrades_visibly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.name", "nt")
        ctx = make_ctx(tmp_path)
        findings = MutmutDiffRunner(ctx).run(ctx)
        assert len(findings) == 1
        assert findings[0].severity is Severity.INFO
        assert findings[0].action is Action.NO_OP
        assert "requires Linux/macOS/WSL" in findings[0].message

    def test_no_changed_src_files_is_empty(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        ctx.changed_files = [Path("tests/test_x.py"), Path("README.md")]
        assert MutmutDiffRunner(ctx).run(ctx) == []


class TestTierGating:
    def test_deep_never_runs_in_other_tiers(self, tmp_path: Path) -> None:
        """§10 Done-when: deep never runs unless explicitly requested."""
        ctx = make_ctx(tmp_path)
        for tier in ("fast", "exec", "gen"):
            names = {r.name for r in runners_for_tier(ctx, tier)}
            assert "mutmut" not in names, tier
        deep_names = {r.name for r in runners_for_tier(ctx, "deep")}
        assert deep_names == {"mutmut"}

    def test_all_runners_have_unique_names(self, tmp_path: Path) -> None:
        runners = gauntlet.runners.all_runners(make_ctx(tmp_path))
        names = [r.name for r in runners]
        assert len(names) == len(set(names))
