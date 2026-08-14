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
pi install git:github.com/aber0016/code_review_gate@v1.3.0
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
| 0 | Style errors, obvious bugs, secrets, personal data (opt-in), changes on critical paths, large diffs | ruff, detect-secrets, critical-paths, diff-size | `gauntlet --tier fast` + pre-push hook |
| 1 | Type errors, calls to APIs that do not exist, unsafe patterns | mypy, bandit | `gauntlet --tier fast` |
| 2 | Imports that do not exist, unknown new packages, known CVEs | pip-audit, uv lockfile, import check | `gauntlet --tier fast` |
| 3 | Crashes, regressions, changed lines without tests | pytest + diff-cover, optional Docker sandbox | `gauntlet --tier exec` |
| 4 | Missing edge cases | Hypothesis scaffolds + validated agent tests | `gauntlet --tier gen` + pi extension |
| 5 | Weak tests that find no bugs; diffs that delete, skip, or weaken tests | mutmut on the changed files (deep); testdiff (fast) | `gauntlet --tier deep` + `--tier fast` |
| 6 | Code that does not do what the user wanted | Review by a different AI model | pi extension (`/gate`) |
| 7 | Decisions that need human judgment | Human approval of parked findings | pi extension dialogs |

## Deterministic checks and AI judgment

The gate has two types of checks. Do not mix them up:

- **Deterministic** — a program computes the result. The same input always gives
  the same result. No AI model makes the decision.
- **AI judgment** — a language model reads the code and gives an opinion. The
  result can change between runs. It can be wrong.

| Layer | Check | Type |
|---|---|---|
| 0 | ruff, detect-secrets, PII scan, critical-paths, diff-size | Deterministic |
| 1 | mypy, bandit | Deterministic |
| 2 | import check, pip-audit, lockfile check | Deterministic |
| 3 | pytest, diff-cover, Docker sandbox | Deterministic |
| 4 | Hypothesis scaffolds | Deterministic |
| 4 | Agent-written tests | Mixed: an AI writes the tests. A deterministic check keeps or rejects each test. |
| 5 | mutmut, testdiff | Deterministic |
| 6 | Cross-model review | AI judgment |
| 7 | Parked-finding approval | Human judgment (not AI) |

Three rules follow from this split:

- All deterministic checks are in the `gauntlet` CLI. All AI judgment is in the
  pi extension. CI and the pre-push hook run only the CLI. Because of this, a
  headless run contains **zero** AI judgment: the same commands give the same
  verdict every time.
- The mixed parts always end in a deterministic decision. In layer 4, the AI
  writes a test, but the gate keeps the test only when it fails on the old code
  and passes on the new code. In the fix loop, the AI changes the code, but the
  gate goes green only when the deterministic tiers pass again.
- AI judgment cannot approve anything. The review (layer 6) can only add
  findings. Only a human (layer 7) can dismiss a parked finding, and only a code
  repair can clear a tool finding. In a headless run there is no dialog, so
  every parked finding blocks.

## The human decision (layer 7)

### What the human sees

The gate does not show the human everything. It shows only the findings that
need a decision: the findings with the action `ask-user`. Examples: a possible
secret, a survived mutant, a change on a critical path, a reviewer finding.

The human sees three things in pi:

1. **A status widget.** One line, always visible. It shows the result of each
   tier, the number of parked findings, and the fix round, for example
   `fast ✓ | exec ✓ | 2 parked | round 1/3`.
2. **One dialog per parked finding.** The dialog shows the tool, the file and
   line, the message, and the context (for example "auto-fix budget
   exhausted"). The gate shows at most 10 dialogs per pass. More findings stay
   parked, and the widget shows the count. `Esc` stops the dialogs; the rest
   stays parked.
3. **A final report in the transcript.** It shows the verdict (green, red, or
   crashed), a one-line summary per tier, the counts of parked and dismissed
   findings, and each open finding with its severity, file, and line. The
   reviewer findings from layer 6 are in the same list.

In a headless run (CI, pre-push hook) there are no dialogs. The CLI prints the
same findings as text, and every parked finding blocks.

### The three choices

Each dialog has three options. Each option has one exact effect:

- **Approve (dismiss)** — the human accepts the risk. The gate records the
  finding ID as dismissed and does not count it again in this session. The
  transcript keeps a `dismissed by user` entry, so the decision stays visible.
  A dismissal does not change the code.
- **Send to fix round** — the human wants a repair. The gate marks the finding
  ID and puts the finding into the next fix round (see below).
- **Abort gate** — the gate stops with a red verdict. Nothing is dismissed.

### How the gate uses the feedback

A finding that the human sends to a fix round becomes work for the agent. The
gate builds one fix prompt and sends it to the agent as a message. The prompt
contains:

- the full findings as JSON (ID, tool, severity, file, line, message, and the
  evidence, cut at 500 characters)
- the diff base and the round number, for example `round 1/3`
- fixed instructions: repair **only** these findings, make the smallest change
  that resolves each root cause, and confirm each fix with the one named check
  (for example `mypy <file>`), not with the full suite.

During the fix round the test-lock is armed: the agent cannot edit, skip, or
delete tests to make a finding go away. When the agent reports that it is done,
the gate runs the tiers again from the start. The loop stops when the gate is
green, or when `[fix] max_rounds` is used up. Then the open findings go back to
the human.

### Your own feedback

The dialog choices are fixed. For feedback in your own words, use one of these
paths:

- **Before the review:** give the reviewer your intent with
  `/gate --review-only --intent "…"`. The reviewer compares the diff against
  this text and flags a mismatch as a finding. That finding then goes through
  the same park-and-decide flow.
- **After a run:** write your feedback as a normal message to the agent
  ("the boundary must be `qty < 10`, not `<=`"). The agent changes the code.
  Then run `/gate` again. Your feedback becomes code, and the code goes
  through the full gate again — the gate never trusts a change because a
  human asked for it.

There is no path where free-text feedback changes the verdict directly. Text
can only lead to a code change or to a dismiss decision, and both are checked
or recorded.

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
pi install git:github.com/aber0016/code_review_gate@v1.3.0
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
- `critical_paths` — the red-list. A change on these paths always needs a human
  decision, also when all tools are green. Example:
  `critical_paths = ["src/auth/**", "**/migrations/**", "**/payments/**"]`.
  The `/gate` command also starts the AI review for these changes, even when a
  check tier is red.
- `sandbox` — `"none"` or `"docker"`
- `sandbox_image` — the Docker image for the sandbox
- `[exec] diff_cover_min` — the minimum coverage of the changed lines, in percent
- `[fix] max_rounds` — the maximum number of automatic fix rounds
- `[deep] blocking`, `max_survivors` — the limits for mutation testing. When
  `blocking` is false (the default), surviving mutants stay visible in the
  report but do not block the gate.
- `[review] provider`, `model` — the AI model for the review
- `[runners.<name>] enabled`, `args`, `timeout` — options for one runner
- `[runners.imports] check_pypi` — optional age check of new packages on PyPI
- `[runners.diff-size] max_lines` — the advisory limit for the diff size
  (default 500; 0 turns the check off). The finding never blocks.
- `[runners.secrets] pii = true` — optional scan for personal data (emails,
  IBANs, German insurance numbers). Checksums filter the matches, so synthetic
  test data (for example `user@example.com`) stays green. Off by default.

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
