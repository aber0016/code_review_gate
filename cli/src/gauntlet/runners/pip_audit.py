"""pip-audit runner — Layer 2: known CVEs in installed packages (§4.5).

Audits the *target repo's* environment (whole environment — CVEs are not
diff-scoped). Vulnerable packages touched by the diff's manifest changes are
errors; pre-existing vulnerabilities are warnings. Everything is ask-user.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

_MANIFEST_NAMES = ("pyproject.toml", "uv.lock", "requirements.txt")
_PYPROJECT_NAME_RE = re.compile(r'"([A-Za-z0-9][A-Za-z0-9._-]*)')
_LOCK_NAME_RE = re.compile(r'^\s*name\s*=\s*"([^"]+)"')


def normalize(name: str) -> str:
    """PEP 503 package-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def touched_packages(root: Path, changed_lines: dict[Path, set[int]]) -> set[str]:
    """Package names appearing on changed lines of dependency manifests."""
    touched: set[str] = set()
    for file, lines in changed_lines.items():
        if file.name not in _MANIFEST_NAMES or not lines:
            continue
        try:
            content = (root / file).read_text(errors="replace")
        except OSError:
            continue
        for lineno, text in enumerate(content.splitlines(), start=1):
            if lineno not in lines:
                continue
            if file.name == "uv.lock":
                match = _LOCK_NAME_RE.match(text)
                if match:
                    touched.add(normalize(match.group(1)))
            else:
                for match in _PYPROJECT_NAME_RE.finditer(text):
                    touched.add(normalize(match.group(1)))
    return touched


def parse_output(
    doc: dict[str, Any], touched: set[str], manifest: str
) -> list[Finding]:
    """Map the pip-audit JSON document to findings."""
    findings: list[Finding] = []
    for dep in doc.get("dependencies", []):
        package = str(dep.get("name", ""))
        version = str(dep.get("version", ""))
        for vuln in dep.get("vulns", []):
            vuln_id = str(vuln.get("id", "UNKNOWN"))
            in_diff = normalize(package) in touched
            fix_versions = ", ".join(vuln.get("fix_versions", [])) or "none published"
            description = str(vuln.get("description", "")).strip()
            findings.append(
                Finding(
                    id=make_id("pip-audit", vuln_id, package),
                    layer=Layer.SUPPLY_CHAIN,
                    tool="pip-audit",
                    severity=Severity.ERROR if in_diff else Severity.WARNING,
                    action=Action.ASK_USER,
                    file=manifest,
                    line=0,
                    message=(
                        f"{package} {version} has known vulnerability {vuln_id}"
                        + (" (introduced/touched by this diff)" if in_diff else "")
                    ),
                    evidence=description[:500],
                    fix_hint=f"upgrade {package}; fixed in: {fix_versions}",
                )
            )
    return findings


class PipAuditRunner:
    """Runs `pip-audit -f json` against the target repo's environment."""

    name = "pip-audit"
    layer = Layer.SUPPLY_CHAIN
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """pip_audit importable by the *target repo's* interpreter.

        Running the module via the repo interpreter guarantees the audited
        environment is the target venv, not gauntlet's own.
        """
        probe = self.ctx.run_cmd(
            [str(self.ctx.repo_python()), "-c", "import pip_audit"],
            timeout=20,
        )
        return probe.code == 0

    def build_argv(self) -> list[str]:
        """Argv for the audit (module invocation in the target interpreter)."""
        argv = [
            str(self.ctx.repo_python()),
            "-m",
            "pip_audit",
            "-f",
            "json",
            "--progress-spinner",
            "off",
        ]
        argv += self.ctx.config.runner_args(self.name)
        return argv

    def run(self, ctx: RunContext) -> list[Finding]:
        """Audit the environment; classify vulns by diff-touched packages."""
        import json as _json

        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        result = ctx.run_cmd(self.build_argv(), timeout=timeout)
        if result.timed_out:
            return [timeout_finding(self.name, self.layer, timeout)]
        if result.code not in (0, 1):
            raise RuntimeError(
                f"pip-audit exited {result.code}: {result.stderr.strip()[:500]}"
            )
        doc = _json.loads(result.stdout or "{}")
        touched = touched_packages(ctx.root, ctx.changed_lines)
        manifest = next(
            (str(p) for p in ctx.changed_files if p.name in _MANIFEST_NAMES),
            "pyproject.toml" if (ctx.root / "pyproject.toml").is_file() else "",
        )
        return parse_output(doc, touched, manifest)
