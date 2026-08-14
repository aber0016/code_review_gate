# Changelog

## v1.3.0 — 2026-08-14

- New `critical-paths` runner (fast tier). The `critical_paths` key in
  `.gauntlet.toml` is a red-list: a change or a deletion on these paths always
  parks a finding for a human, also when all tools are green. The `/gate`
  command starts the AI review for these diffs even when a tier is red.
- New `testdiff` runner (fast tier). It finds diffs that weaken the tests:
  deleted test files or functions, tests moved out of the test folders, new
  skip or xfail marks, and `assert True`. On a critical path the finding is an
  error. The test-lock only guards fix rounds; this runner guards CI and the
  pre-push hook.
- New `diff-size` runner (fast tier). A diff with more changed lines than
  `[runners.diff-size] max_lines` (default 500) gets one advisory finding. The
  finding never blocks.
- The secrets runner has an opt-in scan for personal data
  (`[runners.secrets] pii = true`): emails, IBANs, and German insurance
  numbers, each filtered by a checksum so synthetic test data stays green.
- The README has a new section "Deterministic checks and AI judgment". It marks
  each layer as deterministic, AI judgment, or mixed.
- The README has a new section "The human decision (layer 7)". It shows what
  the human sees, the three dialog choices, the path from a choice to a fix
  round, and the two paths for free-text feedback.
- Fix: `[deep] blocking = false` now works. Surviving mutants were
  warning/ask-user, which still made the gate red. They are now warning/no-op:
  visible in the report, not blocking.

## v1.2.0 — 2026-08-14

- The README is new. It uses ASD-STE100 Simplified Technical English. It starts
  with a short TL;DR in simple words.
- The README shows the pinned install command for the pi package.
- No code changes.

## v0.1.0 — 2026-08-13

- First public release.
- The `gauntlet` CLI with four tiers: `fast`, `exec`, `gen`, and `deep`.
- The pi package: the `/gate` command, the fix loop, the test-lock, the
  test-author round, and the cross-model review.
- The pre-push hook and the GitHub Actions workflow. All three use the same
  binary and give the same result.
