#!/usr/bin/env bash
# Stand up a scratch copy of fixture/ for Verify blocks and the acceptance run.
#
# The fixture's planted bugs stay tracked in the outer repo; the plan's
# `git init` dance happens here, in a disposable copy, so no nested .git
# ever breaks tracking. The "AI diff" perturbation touches the planted
# buggy lines (trailing comments) so strict changed-line filtering still
# surfaces every planted finding, and leaves `return price` uncovered so
# the exec tier's diff-coverage gate trips.
#
# Usage: scripts/make-scratch-fixture.sh [target-dir] [--no-venv]
# Prints the scratch path on the last line of stdout.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="${1:-$(mktemp -d "${TMPDIR:-/tmp}/gauntlet-fixture.XXXXXX")}"
NO_VENV="${2:-}"

mkdir -p "$SCRATCH"
cp -R "$REPO_ROOT/fixture/." "$SCRATCH/"

GIT=(git -C "$SCRATCH" -c user.name=fixture -c user.email=fixture@example.com \
     -c commit.gpgsign=false)

cd "$SCRATCH"
printf '%s\n' '.venv/' '.gauntlet/' 'mutants/' '.pytest_cache/' '__pycache__/' \
  '*.egg-info/' '.coverage*' 'coverage.xml' 'junit.xml' > .gitignore

git init -q -b main
"${GIT[@]}" add -A
"${GIT[@]}" commit -q -m base

# --- the "AI diff": touch the planted buggy lines -------------------------
python3 - <<'EOF'
from pathlib import Path

TOUCH = {
    "src/fixture_pkg/fetcher.py": ("import requestz", "    except:"),
    "src/fixture_pkg/hasher.py": ("    return hashlib.md5(data).hexdigest()",),
    "src/fixture_pkg/pricing.py": ("    if qty <= 10:", "        return price"),
}
for rel, needles in TOUCH.items():
    path = Path(rel)
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line in needles:
            lines[i] = line + "  # ai-diff"
    path.write_text("\n".join(lines) + "\n")
EOF

"${GIT[@]}" add -A
"${GIT[@]}" commit -q -m "AI diff"

# --- venv with the checked tools ------------------------------------------
if [ "$NO_VENV" != "--no-venv" ]; then
  uv venv .venv --python 3.12 --quiet
  uv pip install --python .venv/bin/python --quiet \
    -e ".[test]" -e "$REPO_ROOT/cli" ruff mypy bandit pip-audit detect-secrets \
    diff-cover hypothesis mutmut
fi

echo "$SCRATCH"
