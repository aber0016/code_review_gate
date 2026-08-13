---
name: gauntlet
description: Run the gauntlet layered review-and-test gate on Python code before pushing, after completing a coding task, or when asked to "check", "gate", or "review before push". Interprets the finding contract (severity × action) and fixes what is mechanically fixable without deciding judgment calls.
---

# gauntlet — layered review gate for Python diffs

When the user asks to "check", "gate", "review before push" — or after you complete a
coding task in a Python repo — run the deterministic gate and act on its findings:

```bash
gauntlet run --tier fast --base origin/main --json
```

(If `gauntlet` is not on PATH, use `.venv/bin/gauntlet`. If the repo's default branch
is not `origin/main`, pass the right `--base`, or omit `--base` to let
`.gauntlet.toml` / `origin/main` / `main` resolution decide.)

Read the JSON from stdout. Exit codes: `0` green, `1` blocking findings, `2` a runner
crashed (treat as red — fail closed; do not push).

For deeper checks: `--tier exec` runs tests + diff coverage; `--tier gen` scaffolds
property tests for untested changed functions; `--tier deep` runs mutation testing
(slow; only when explicitly requested). Inside pi, prefer the `/gate` command, which
orchestrates tiers, the fix loop, and human approval.

## The finding contract

Every finding has this shape:

```json
{
  "id": "ruff:E722:src/fetcher.py:14",
  "layer": "static",
  "tool": "ruff",
  "severity": "error",
  "action": "auto-fix",
  "file": "src/fetcher.py",
  "line": 14,
  "message": "Do not use bare `except`",
  "evidence": "E722",
  "fix_hint": "Catch the specific exception; never swallow silently."
}
```

| Field | Values | Meaning |
|---|---|---|
| `severity` | `error` \| `warning` \| `info` | Errors and warnings block; info never does. |
| `action` | `auto-fix` | Mechanical; you may fix it yourself without asking. |
| | `ask-user` | Touches intent or risk; **surface it to the user verbatim — do not decide it yourself**. |
| | `no-op` | Informational; shown, never blocks, no action needed. |
| `layer` | `precommit`, `static`, `supply-chain`, `exec`, `test-gen`, `mutation`, `review` | Which pipeline layer produced it. |

## How to act on findings

1. Fix every `auto-fix` finding with `severity: error` (and `warning` where the fix is
   obvious). Make the smallest change that resolves the root cause, then re-run the
   same `gauntlet run` command to confirm.
2. For `ask-user` findings: quote the finding's `message` (and `fix_hint`) to the user
   verbatim and ask how to proceed. These are security/intent judgment calls
   (secrets, hallucinated dependencies, insecure hashing, CVEs) — never resolve them
   silently, and **never install a package to make a hallucinated import resolve**.
3. `no-op` findings need no action.
4. A crashed runner (exit 2) means the gate is red, not "skip it": report it.
5. Never edit or weaken tests to make the gate pass; fix `src/` instead. During pi fix
   rounds, test edits are mechanically blocked anyway.
