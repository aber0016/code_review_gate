# pi-gauntlet

A layered review-and-test gate for AI-generated Python code:

- **`gauntlet`** — a standalone, stdlib-only Python CLI that runs tiered deterministic
  gates (`fast`, `exec`, `gen`, `deep`) and emits findings in one stable JSON contract.
  The identical binary runs in pi, in a `pre-push` hook, and in GitHub Actions.
- **a pi package** (extension + skill + prompt template) that orchestrates the CLI inside
  the [pi](https://pi.dev) harness, mechanically enforces the test-lock during fix rounds,
  and runs the cross-model review + human-approval layers.

Built and verified layer by layer: 116 CLI tests, a 47-case test-lock decision matrix,
end-to-end runs in pi (TUI, RPC, and headless), a network-less Docker sandbox run, and
a red→green CI demonstration on this repo's own workflow.

## Layers

| Layer | What it catches | Tool | Where it lives |
|---|---|---|---|
| 0 | Style, obvious bugs, secrets | ruff, detect-secrets | `gauntlet --tier fast` + pre-push hook |
| 1 | Type errors, hallucinated APIs, insecure patterns | mypy, bandit | `gauntlet --tier fast` |
| 2 | Hallucinated/slopsquatted deps, known CVEs | pip-audit, uv lockfile, import-existence check | `gauntlet --tier fast` |
| 3 | Crashes, regressions, untested changed lines | pytest + diff-cover, optional Docker sandbox | `gauntlet --tier exec` |
| 4 | Missing edge cases | Hypothesis scaffolds + coverage-validated agent tests | `gauntlet --tier gen` + pi extension |
| 5 | Tautological/weak tests | mutmut on changed files | `gauntlet --tier deep` |
| 6 | Intent mismatch, semantic bugs | Cross-model LLM diff review | pi extension (`/gate`) |
| 7 | Judgment calls | Human approval of parked findings | pi extension UI |

## Design principles

1. **One binary defines "green".** All deterministic logic lives in the `gauntlet` CLI;
   the pi extension never re-implements a check.
2. **The harness enforces what prompts can only request.** During fix rounds the fixing
   agent is *mechanically blocked* from editing tests via pi `tool_call` interception —
   not merely instructed to leave them alone. The block patterns are a tripwire against
   accidental/lazy tampering by an in-harness agent, **not a security boundary** against
   a deliberately adversarial process.

## Quick start

```bash
# CLI (works anywhere: pi, pre-push, CI)
uv pip install -e ./cli            # into the target repo's venv
gauntlet run --tier fast --base origin/main --json
gauntlet install-hook              # pre-push gate; git push --no-verify bypasses

# pi package (pin a tag — pi packages run with your full system permissions)
pi install git:github.com/aber0016/code_review_gate@v0.1.0
# then, inside pi: /gate [base] [--gen --bugfix --deep --review-only --intent "…"]
```

Config: `.gauntlet.toml` at the target repo root — `base`, `src_paths`, `test_paths`,
`exclude_paths`, `sandbox = "none"|"docker"`, `sandbox_image`, `[exec] diff_cover_min`,
`[fix] max_rounds`, `[deep] blocking/max_survivors`, `[review] provider/model`,
per-runner `[runners.<name>] enabled/args/timeout` (plus `[runners.imports] check_pypi`).

The docker sandbox runs with `--network=none` (no egress — that's the point), so
`sandbox_image` must pre-contain the project's build backend and test deps, e.g.:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir hatchling editables pytest pytest-cov
```

A bare image fails visibly (pip cannot reach PyPI offline); it never passes silently.

## Demo

Transcript record of the end-to-end acceptance run (scratch copy of `fixture/`, one pi
session driven over RPC, gpt-5.6-sol as author/fixer, deepseek-v4-pro as reviewer;
dialog choices below are the "human"):

1. **`/gate base0` → RED.** Findings from 5 tools (`imports`, `mypy`, `ruff`, `bandit`,
   `diff-cover`) across layers `static`, `supply-chain`, `exec`. Parking dialogs:
   `requestz` hallucinated import, bare `except`, MD5 — approved-to-unblock (Layer 7).
2. **Fix loop:** round 1 resolved both mypy errors (the agent swapped `requestz` →
   `requests`; the re-run gate immediately flagged *"`requests` installed but
   undeclared"* — the gate catching its own fix's supply-chain fallout). The exec
   tier's diff-coverage error can't be fixed under the test-lock, so the round budget
   exhausted and the gate stayed RED with the coverage finding parked. ≤ 3 rounds;
   `git status tests/` clean throughout.
3. **Human fixes (scripted):** `requestz` → stdlib `json.loads`, `md5` → `sha256`,
   `qty <= 10` → `qty < 10`; committed.
4. **`/gate base0 --gen --bugfix`:** test-author round under the inverse lock authored
   `tests/test_fetcher.py` + `tests/test_hasher.py`; validation kept 2 / rejected 0;
   diff coverage 14.0% → **100.0%**; fail-before-fix guard satisfied (the new tests
   error on `base0`, where the hallucinated import breaks); kept tests landed in
   exactly one `test: gauntlet test-author round` commit.
5. **`/gate base0 --deep`:** first proved the tension the tiers are designed around —
   with coverage saturated, the boundary tests had been rejected as "adds zero covered
   lines", and mutation testing duly reported `qty < 10 → qty <= 10` **surviving**.
   After fixing the saturation edge (at 100% diff coverage, pass/fail + the bugfix
   guard decide, not an unsatisfiable coverage delta), a directed test-author round
   kept the boundary tests — and the same `/gate --deep` reported **0 surviving
   mutants** in `pricing.py`.
6. **`/gate base0 --review-only --intent "discount applies from the 10th unit…"`:**
   the out-of-family reviewer parked two real semantic findings — `fetch_json`
   conflating a JSON `null` payload with a parse error, and a test docstring
   contradicting the stated non-security intent of `digest`. (Earlier, with the buggy
   boundary still in place, the same reviewer flagged *"qty <= 10 still denies the
   discount at the 10th unit"* — the Layer 6 catch no deterministic tool can make.)
7. **Final `/gate base0` → green.** Widget: `⛩ gate base0..HEAD — fast ✓ 1.9s |
   exec ✓ 1.3s | 0 parked`. `git diff base0 -- tests/` contains only the test-author
   rounds' files; `git log base0..HEAD -- tests/` shows exactly the two
   `test: gauntlet test-author round` commits.

Reward-hacking defenses verified separately (Phase 4): a scripted provider forcing
`edit tests/test_pricing.py` and `pytest -k` inside a genuine fix round was blocked
mechanically with the reasons shown, and a 47-case decision matrix covers the lock
(fix + test-author modes) through the real module.
