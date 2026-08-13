"""import-existence / slopsquatting detector — Layer 2 (§4.7 of the plan).

For every changed .py file: parse imports with ``ast``, discard stdlib and
first-party modules, then map the rest to installed distributions via
``importlib.metadata.packages_distributions()`` *executed in the target
repo's interpreter*. Imports that resolve to no installed package are
errors (possible hallucinated dependency); installed-but-undeclared
packages are warnings. Findings are scoped to changed files (a
hallucinated dependency is a file/environment property, not a line
property). Never installs anything.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gauntlet.findings import Action, Finding, Layer, Severity, make_id
from gauntlet.runners import RunContext, timeout_finding

_PROBE_SCRIPT = (
    "import json, sys\n"
    "from importlib.metadata import packages_distributions\n"
    "print(json.dumps({\n"
    "    'stdlib': sorted(sys.stdlib_module_names),\n"
    "    'dist_map': {k: sorted(set(v))\n"
    "                 for k, v in packages_distributions().items()},\n"
    "}))\n"
)

_RECENT_DAYS = 90
_PYPI_TIMEOUT_S = 5.0


def normalize(name: str) -> str:
    """PEP 503 package-name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class EnvInfo:
    """What the target interpreter knows: stdlib names and module→dist map."""

    stdlib: set[str]
    dist_map: dict[str, list[str]]


@dataclass
class ImportSite:
    """One top-level imported name and where it first appears per file."""

    module: str
    file: Path
    line: int


def collect_imports(root: Path, files: list[Path]) -> list[ImportSite]:
    """Top-level names from ``import X`` / ``from X import …`` per file.

    Relative imports are skipped (always first-party). Files that fail to
    parse are skipped — the syntax error belongs to ruff/pytest, not here.
    """
    sites: list[ImportSite] = []
    for file in files:
        try:
            tree = ast.parse((root / file).read_text(errors="replace"))
        except SyntaxError:
            continue
        seen: set[str] = set()
        for node in ast.walk(tree):
            names: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                names = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [(node.module, node.lineno)]
            for dotted, line in names:
                top = dotted.split(".")[0]
                if top not in seen:
                    seen.add(top)
                    sites.append(ImportSite(module=top, file=file, line=line))
    return sites


def first_party_names(root: Path, src_paths: list[str]) -> set[str]:
    """Module names resolvable under src_paths or the repo root."""
    names: set[str] = set()
    for base in [root, *(root / sp for sp in src_paths)]:
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if entry.is_file() and entry.suffix == ".py":
                names.add(entry.stem)
            elif entry.is_dir() and (
                (entry / "__init__.py").is_file() or any(entry.glob("*.py"))
            ):
                names.add(entry.name)
    return names


def _sibling_module(root: Path, site: ImportSite) -> bool:
    """Whether the imported name resolves in the importing file's directory."""
    base = (root / site.file).parent
    return (base / f"{site.module}.py").is_file() or (
        base / site.module / "__init__.py"
    ).is_file()


def declared_distributions(root: Path) -> set[str] | None:
    """Normalized dist names declared in pyproject.toml / uv.lock.

    Returns ``None`` when neither manifest exists — with nothing declared,
    the undeclared-dependency check would only produce noise.
    """
    declared: set[str] = set()
    found = False
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        found = True
        try:
            doc = tomllib.loads(pyproject.read_text(errors="replace"))
        except tomllib.TOMLDecodeError:
            doc = {}
        specs: list[str] = list(doc.get("project", {}).get("dependencies", []))
        for group in doc.get("project", {}).get("optional-dependencies", {}).values():
            specs.extend(group)
        for group in doc.get("dependency-groups", {}).values():
            specs.extend(spec for spec in group if isinstance(spec, str))
        project_name = doc.get("project", {}).get("name")
        if isinstance(project_name, str):
            declared.add(normalize(project_name))
        for spec in specs:
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
            if match:
                declared.add(normalize(match.group(1)))
    lock = root / "uv.lock"
    if lock.is_file():
        found = True
        try:
            doc = tomllib.loads(lock.read_text(errors="replace"))
        except tomllib.TOMLDecodeError:
            doc = {}
        for package in doc.get("package", []):
            name = package.get("name")
            if isinstance(name, str):
                declared.add(normalize(name))
    return declared if found else None


def pypi_age_note(module: str) -> str:
    """Optional slopsquatting check against the live PyPI index.

    Network failures downgrade to a note, never an error; nothing is ever
    installed.
    """
    url = f"https://pypi.org/pypi/{module}/json"
    try:
        # nosec B310 - scheme is fixed https, host is pypi.org, read-only GET
        with urllib.request.urlopen(url, timeout=_PYPI_TIMEOUT_S) as response:  # nosec
            doc = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return " Not registered on PyPI at all."
        return f" (PyPI check failed: HTTP {exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return f" (PyPI check failed: {exc})"
    uploads = [
        datetime.fromisoformat(item["upload_time_iso_8601"].replace("Z", "+00:00"))
        for release in doc.get("releases", {}).values()
        for item in release
        if "upload_time_iso_8601" in item
    ]
    if not uploads:
        return " Registered on PyPI but has no uploads."
    first = min(uploads)
    if datetime.now(UTC) - first < timedelta(days=_RECENT_DAYS):
        return (
            f" Package exists on PyPI but was first released {first:%Y-%m-%d} "
            f"(< {_RECENT_DAYS} days ago) — classic slopsquatting profile; "
            "verify before installing."
        )
    return ""


class ImportsRunner:
    """The custom hallucinated-import / undeclared-dependency detector."""

    name = "imports"
    layer = Layer.SUPPLY_CHAIN
    tier = "fast"

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def available(self) -> bool:
        """Always available: needs only ast + the repo interpreter."""
        return True

    def build_probe_argv(self) -> list[str]:
        """Argv of the env probe executed in the *target repo's* venv."""
        return [str(self.ctx.repo_python()), "-c", _PROBE_SCRIPT]

    def _probe_env(self, ctx: RunContext, timeout: float) -> EnvInfo | Finding:
        result = ctx.run_cmd(self.build_probe_argv(), timeout=min(60.0, timeout))
        if result.timed_out:
            return timeout_finding(self.name, self.layer, min(60.0, timeout))
        if result.code != 0:
            return Finding(
                id=make_id(self.name, "probe-failed", ""),
                layer=self.layer,
                tool=self.name,
                severity=Severity.WARNING,
                action=Action.ASK_USER,
                file="",
                line=0,
                message=(
                    "imports check could not query the repo interpreter "
                    f"({ctx.repo_python()})"
                ),
                evidence=result.stderr.strip()[:500],
            )
        doc = json.loads(result.stdout)
        return EnvInfo(
            stdlib=set(doc["stdlib"]),
            dist_map={str(k): list(v) for k, v in doc["dist_map"].items()},
        )

    def run(self, ctx: RunContext) -> list[Finding]:
        """Classify every imported top-level name in changed files."""
        files = ctx.changed_py_files
        if not files:
            return []
        sites = collect_imports(ctx.root, files)
        if not sites:
            return []
        timeout = ctx.config.runner_timeout(self.name) or ctx.timeout
        env = self._probe_env(ctx, timeout)
        if isinstance(env, Finding):
            return [env]
        first_party = first_party_names(ctx.root, ctx.config.src_paths)
        declared = declared_distributions(ctx.root)
        check_pypi = bool(ctx.config.runner_option("imports", "check_pypi", False))

        findings: list[Finding] = []
        pypi_notes: dict[str, str] = {}
        for site in sites:
            module = site.module
            if module in env.stdlib or module in first_party:
                continue
            if _sibling_module(ctx.root, site):
                # Resolvable next to the importing file (pytest-style
                # conftest/test helpers): first-party by construction.
                continue
            file = str(site.file)
            dists = env.dist_map.get(module)
            if dists is None:
                message = (
                    f"import '{module}' resolves to no installed package — "
                    "possible hallucinated dependency"
                )
                if check_pypi:
                    if module not in pypi_notes:
                        pypi_notes[module] = pypi_age_note(module)
                    message += pypi_notes[module]
                findings.append(
                    Finding(
                        id=make_id(self.name, "missing-dist", file, module),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.ERROR,
                        action=Action.ASK_USER,
                        file=file,
                        line=site.line,
                        message=message,
                        evidence=f"module: {module}",
                        fix_hint=(
                            "Replace with a real, declared dependency or remove "
                            "the import. Never install unverified packages."
                        ),
                    )
                )
            elif declared is not None and declared.isdisjoint(
                normalize(d) for d in dists
            ):
                # None of the distributions providing this module is declared.
                undeclared = sorted(dists)
                findings.append(
                    Finding(
                        id=make_id(self.name, "undeclared", file, module),
                        layer=self.layer,
                        tool=self.name,
                        severity=Severity.WARNING,
                        action=Action.ASK_USER,
                        file=file,
                        line=site.line,
                        message=(
                            f"import '{module}' is installed via "
                            f"{', '.join(undeclared)} but undeclared — "
                            "pin it or remove it"
                        ),
                        evidence=f"distributions: {', '.join(dists)}",
                        fix_hint="declare it in pyproject.toml and lock (uv add)",
                    )
                )
        return findings
