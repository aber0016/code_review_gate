"""gauntlet CLI entry point: tier dispatch, orchestration, report emission.

Exit codes (CI contract, §2): 0 = no blocking findings, 1 = blocking findings
exist, 2 = a runner or the gate itself crashed (fail closed).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time
import traceback
from pathlib import Path
from typing import TextIO

from gauntlet import __version__, gitdiff
from gauntlet.config import Config, ConfigError
from gauntlet.findings import (
    EXIT_BLOCKING,
    EXIT_CRASH,
    EXIT_OK,
    Action,
    Finding,
    Report,
    Severity,
    make_id,
)
from gauntlet.runners import (
    TIER_TIMEOUTS,
    TIERS,
    RunContext,
    Runner,
    runners_for_tier,
    skipped_finding,
)

MAX_PARALLEL_RUNNERS = 8


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse CLI."""
    parser = argparse.ArgumentParser(
        prog="gauntlet",
        description="Layered review-and-test gate for AI-generated Python code.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help=(
            "Run gate tiers: --tier {fast,exec,gen,deep} --base REF "
            "--json --fix [--runner NAME]"
        ),
        description="Run the review gate for one tier against a base ref.",
    )
    run_parser.add_argument(
        "--tier",
        choices=TIERS,
        default="fast",
        help="Gate tier to run (default: fast)",
    )
    run_parser.add_argument(
        "--base",
        help="Base ref to diff against (default: .gauntlet.toml, origin/main, main)",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as a single JSON document on stdout",
    )
    run_parser.add_argument(
        "--fix",
        action="store_true",
        help="Allow runners to apply their mechanical fixes (ruff --fix, uv lock)",
    )
    run_parser.add_argument(
        "--runner",
        help="Run a single runner by name (debugging / CI matrix jobs)",
    )
    run_parser.add_argument(
        "--verify-fails-on",
        metavar="REF",
        help=(
            "exec tier: additionally require the new/changed tests to FAIL "
            "when run against REF (fail-before-fix discipline, §9.2.4)"
        ),
    )

    hook_parser = subparsers.add_parser(
        "install-hook",
        help="Install the pre-push hook (fast tier gates every push)",
        description=(
            "Writes .git/hooks/pre-push running the fast tier against @{push} "
            "(fallback: origin/main). `git push --no-verify` is the escape hatch."
        ),
    )
    hook_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing pre-push hook",
    )
    return parser


def excluded(path: Path, exclude_paths: list[str]) -> bool:
    """Whether a repo-relative path falls under an excluded prefix."""
    return any(path == Path(p) or path.is_relative_to(p) for p in exclude_paths)


def build_context(
    root: Path,
    config: Config,
    base: str,
    tier: str,
    fix: bool,
    verify_fails_on: str | None = None,
) -> RunContext:
    """Assemble the RunContext: diff scope (minus exclusions) + config."""
    exclude = config.exclude_paths
    changed_files = [
        p for p in gitdiff.changed_files(root, base) if not excluded(p, exclude)
    ]
    changed_lines = {
        p: lines
        for p, lines in gitdiff.changed_lines(root, base).items()
        if not excluded(p, exclude)
    }
    return RunContext(
        root=root,
        base_ref=base,
        merge_base=gitdiff.merge_base(root, base),
        head=gitdiff.head_rev(root),
        changed_files=changed_files,
        changed_lines=changed_lines,
        config=config,
        tier=tier,
        timeout=TIER_TIMEOUTS[tier],
        fix=fix,
        verify_fails_on=verify_fails_on,
    )


def _run_one(runner: Runner, ctx: RunContext) -> tuple[str, list[Finding]]:
    """Execute one runner, converting crashes into findings (fail closed)."""
    try:
        return "ok", runner.run(ctx)
    except Exception as exc:  # noqa: BLE001 - a crashed runner must not kill the gate
        detail = "".join(traceback.format_exception_only(exc)).strip()
        crash = Finding(
            id=make_id(runner.name, "crashed", ""),
            layer=runner.layer,
            tool=runner.name,
            severity=Severity.ERROR,
            action=Action.ASK_USER,
            file="",
            line=0,
            message=f"runner {runner.name} crashed: {detail}",
            evidence=traceback.format_exc(limit=8)[-2000:],
        )
        return f"crashed: {detail}"[:200], [crash]


def execute_tier(ctx: RunContext, only: str | None = None) -> Report:
    """Run all (enabled, available) runners of a tier in parallel and report."""
    start = time.monotonic()
    report = Report(
        tier=ctx.tier,
        base=ctx.merge_base[:7],
        head=ctx.head,
        changed_files=[str(p) for p in ctx.changed_files],
    )
    runners = runners_for_tier(ctx, ctx.tier, only=only)
    if only is not None and not runners:
        raise ConfigError(f"no runner named {only!r} (or it is disabled)")

    ready: list[Runner] = []
    for runner in runners:
        if runner.available():
            ready.append(runner)
        else:
            report.runners[runner.name] = "skipped"
            report.findings.append(skipped_finding(runner.name, runner.layer))

    if ready:
        workers = min(MAX_PARALLEL_RUNNERS, len(ready))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, r, ctx): r for r in ready}
            for future in concurrent.futures.as_completed(futures):
                runner = futures[future]
                status, findings = future.result()
                report.runners[runner.name] = status
                report.findings.extend(findings)

    report.duration_s = time.monotonic() - start
    report.sort_findings()
    return report


def emit_human(report: Report, stream: TextIO | None = None) -> None:
    """Compact human-readable report for terminals and the pre-push hook."""
    out = stream if stream is not None else sys.stdout
    for finding in report.findings:
        location = f"{finding.file}:{finding.line}" if finding.file else "(repo)"
        print(
            f"[{finding.severity}] {finding.tool} {location} — {finding.message}"
            f" ({finding.action})",
            file=out,
        )
    blocking = report.blocking_findings()
    verdict = "RED" if (blocking or report.crashed()) else "green"
    counts = {
        sev: sum(1 for f in report.findings if f.severity is sev) for sev in Severity
    }
    statuses = ", ".join(f"{k}={v}" for k, v in sorted(report.runners.items()))
    print(
        f"gate {verdict} — tier={report.tier} base={report.base} head={report.head} "
        f"| {counts[Severity.ERROR]} error(s), {counts[Severity.WARNING]} warning(s), "
        f"{counts[Severity.INFO]} info | blocking={len(blocking)} "
        f"| {report.duration_s:.1f}s | {statuses or 'no runners'}",
        file=out,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Handle `gauntlet run`."""
    try:
        root = gitdiff.repo_root()
        config = Config.load(root)
        base = gitdiff.resolve_base(root, args.base, config.base)
        ctx = build_context(
            root, config, base, args.tier, args.fix, args.verify_fails_on
        )
        report = execute_tier(ctx, only=args.runner)
    except (gitdiff.GitError, ConfigError, OSError) as exc:
        print(f"gauntlet: {exc}", file=sys.stderr)
        return EXIT_CRASH
    if args.json:
        print(report.to_json())
    else:
        emit_human(report)
    return report.exit_code()


HOOK_SCRIPT = """#!/bin/sh
# Installed by `gauntlet install-hook`. Bypass with: git push --no-verify
GAUNTLET="$(git rev-parse --show-toplevel)/.venv/bin/gauntlet"
[ -x "$GAUNTLET" ] || GAUNTLET=gauntlet
"$GAUNTLET" run --tier fast --base @{push} 2>/dev/null || \\
    "$GAUNTLET" run --tier fast --base origin/main
"""


def cmd_install_hook(args: argparse.Namespace) -> int:
    """Handle `gauntlet install-hook` (§11): write .git/hooks/pre-push."""
    try:
        root = gitdiff.repo_root()
        git_dir = gitdiff.git_dir(root)
    except gitdiff.GitError as exc:
        print(f"gauntlet: {exc}", file=sys.stderr)
        return EXIT_CRASH
    hook_path = git_dir / "hooks" / "pre-push"
    if hook_path.exists() and not args.force:
        existing = hook_path.read_text(errors="replace")
        if existing == HOOK_SCRIPT:
            print(f"pre-push hook already installed at {hook_path}")
            return EXIT_OK
        print(
            f"gauntlet: {hook_path} already exists and differs; "
            "re-run with --force to overwrite",
            file=sys.stderr,
        )
        return EXIT_BLOCKING
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(HOOK_SCRIPT)
    hook_path.chmod(0o755)
    print(
        f"pre-push hook installed at {hook_path} (bypass with `git push --no-verify`)"
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script)."""
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "install-hook":
        return cmd_install_hook(args)
    raise AssertionError(f"unhandled command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
