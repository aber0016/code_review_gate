# code_review_gate

## TL;DR

An AI agent writes code fast. But the code can have hidden problems.

This repository is a quality gate for that code. The gate:

- finds bugs, bad imports, weak tests, and security problems in Python code
- runs the cheap checks first and the slow checks last
- lets a second, different AI model review the code
- asks a human to decide the risky findings
- blocks the code until the checks pass.

The same tool runs on your computer, in the [pi](https://pi.dev) agent, in a git
pre-push hook, and in CI. The result is always the same.

Install the pi package:

```bash
pi install git:github.com/aber0016/code_review_gate@v1.2.0
```

## What is in this repository

This repository has two parts:

- **`gauntlet`** — a Python CLI in `cli/`. It runs the check tiers (`fast`, `exec`,
  `gen`, `deep`). It writes each finding in one stable JSON format. The core uses
  only the Python standard library.
- **A pi package** — an extension, a skill, and a prompt template for the pi agent.
  The internal package name is `pi-gauntlet`. The package runs the CLI, controls
  the fix loop, applies the test-lock, and starts the AI review.

## The layers

| Layer | What it finds | Tool | Where it runs |
|---|---|---|---|
| 0 | Style errors, obvious bugs, secrets | ruff, detect-secrets | `gauntlet --tier fast` + pre-push hook |
| 1 | Type errors, calls to APIs that do not exist, unsafe patterns | mypy, bandit | `gauntlet --tier fast` |
| 2 | Imports that do not exist, unknown new packages, known CVEs | pip-audit, uv lockfile, import check | `gauntlet --tier fast` |
| 3 | Crashes, regressions, changed lines without tests | pytest + diff-cover, optional Docker sandbox | `gauntlet --tier exec` |
| 4 | Missing edge cases | Hypothesis scaffolds + validated agent tests | `gauntlet --tier gen` + pi extension |
| 5 | Weak tests that find no bugs | mutmut on the changed files | `gauntlet --tier deep` |
| 6 | Code that does not do what the user wanted | Review by a different AI model | pi extension (`/gate`) |
| 7 | Decisions that need human judgment | Human approval of parked findings | pi extension dialogs |

## Two rules control the design

1. **One binary decides "green".** All checks that give a fixed result are in the
   `gauntlet` CLI. The same command runs in pi, in the pre-push hook, and in
   GitHub Actions. The pi extension does not do its own checks.
2. **The harness enforces the rules. Prompts only ask.** In a fix round, the
   system blocks the write access of the agent to the test files. An instruction
   alone is not enough. Note: the block patterns stop a careless agent. They
   are not a security barrier against a hostile process.

## Quick start

### The CLI

Install the CLI into the venv of the target repository:

```bash
uv pip install -e ./cli
```

Run the fast tier:

```bash
gauntlet run --tier fast --base origin/main --json
```

Install the pre-push hook:

```bash
gauntlet install-hook
```

The hook blocks a `git push` when the gate is red. To skip the hook one time,
use `git push --no-verify`.

The exit codes are: `0` = no blocking findings. `1` = blocking findings.
`2` = a runner failed. A failed runner also makes the gate red.

### The pi package

Install the package with a pinned tag:

```bash
pi install git:github.com/aber0016/code_review_gate@v1.2.0
```

Then run the gate in pi:

```
/gate [base] [--gen --bugfix --deep --review-only --intent "…"]
```

**Caution:** a pi package runs with your full system permissions. Install only a
tag that you examined before.

### Configuration

Put a `.gauntlet.toml` file in the root of the target repository. All keys are
optional:

- `base` — the git ref to compare against
- `src_paths` — the folders with the source code (default `["src"]`)
- `test_paths` — the folders and files with the test code
- `exclude_paths` — paths that the gate ignores
- `sandbox` — `"none"` or `"docker"`
- `sandbox_image` — the Docker image for the sandbox
- `[exec] diff_cover_min` — the minimum coverage of the changed lines, in percent
- `[fix] max_rounds` — the maximum number of automatic fix rounds
- `[deep] blocking`, `max_survivors` — the limits for mutation testing
- `[review] provider`, `model` — the AI model for the review
- `[runners.<name>] enabled`, `args`, `timeout` — options for one runner
- `[runners.imports] check_pypi` — optional age check of new packages on PyPI.

### The Docker sandbox

The `exec` tier can run the tests in a Docker container. The container has no
network access (`--network=none`). Because of this, pip cannot download packages
in the container. The image must contain the build backend and the test
dependencies:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir hatchling editables pytest pytest-cov
```

If the image does not have these packages, the run fails with a clear message.
The gate does not pass silently.

## Demo

We ran the full pipeline on a copy of `fixture/`. The fixture has these
planted bugs:

- an import that does not exist (`requestz`)
- a bare `except`
- MD5 as the hash function
- a wrong boundary (`qty <= 10`)
- a weak test.

One pi session ran the gate. The model gpt-5.6-sol was the author and the
fixer. The model deepseek-v4-pro was the reviewer. A human made the dialog
choices.

1. `/gate base0` → the gate is RED. Five tools report findings: `imports`,
   `mypy`, `ruff`, `bandit`, and `diff-cover`. The dialogs park the risky
   findings for the human.
2. The fix loop starts. The agent repairs the two mypy errors in round 1. The
   agent replaces `requestz` with `requests`. The gate then reports a new
   problem: `requests` is not a declared dependency. The coverage error stays,
   because the test-lock does not let the fixer change the tests. The gate
   stays RED.
3. The human repairs the code: `json.loads` replaces the import that does not
   exist, `sha256` replaces `md5`, and `qty < 10` replaces `qty <= 10`.
4. `/gate base0 --gen --bugfix` → the test-author round writes two test files.
   The validation keeps both files. The coverage of the changed lines goes from
   14% to 100%. The new tests fail on the old code and pass on the new code.
   The kept tests go into one commit.
5. `/gate base0 --deep` → mutation testing first reported one mutant that the
   tests did not detect (`qty < 10` → `qty <= 10`). After a boundary test, the
   same command reports 0 survivors.
6. `/gate base0 --review-only --intent "…"` → the reviewer parks two correct
   findings: `fetch_json` mixes a JSON `null` with a parse error, and a test
   docstring does not agree with the given intent. Before the fix, the same
   reviewer found the wrong boundary. No fixed-result tool can find that type
   of bug.
7. The final `/gate base0` is green. The widget shows `fast ✓ | exec ✓ |
   0 parked`. Only the test-author commits changed the `tests/` folder.

We tested the test-lock separately. A scripted model tried to change
`tests/test_pricing.py` and tried to run `pytest -k` in a fix round. The system
blocked the two calls and showed the reasons. A decision matrix with 47 cases
covers the lock rules.

## License

MIT. See the `LICENSE` file.
