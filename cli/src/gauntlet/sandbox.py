"""Execution backends for the exec tier: ``none`` (host) or ``docker`` (§5.2).

Strategy interface so a micro-VM backend can be added later without touching
runners. ``--network=none`` is the point of the docker backend: no egress.
Degradation is always visible: running on the host is a no-op warning; a
*requested-but-unavailable* docker sandbox downgrades with an ask-user
warning so the downgrade blocks instead of passing silently.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from gauntlet.findings import Action, Finding, Layer, Severity, make_id

if TYPE_CHECKING:
    from gauntlet.runners import RunContext

DOCKER_PROBE_TIMEOUT_S = 10.0


class Sandbox(Protocol):
    """Strategy for wrapping the exec-tier pytest invocation."""

    name: str

    def build_pytest_argv(
        self,
        ctx: RunContext,
        pytest_args: list[str],
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Full argv that runs pytest with the given args inside the backend.

        ``env`` vars must reach the *test process*: the host backend relies on
        the caller's subprocess env, the docker backend forwards them with
        ``-e`` (a container never inherits the client's environment).
        """
        ...


class HostSandbox:
    """Run directly on the host, in the target repo's venv."""

    name = "none"

    def build_pytest_argv(
        self,
        ctx: RunContext,
        pytest_args: list[str],
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """``<repo python> -m pytest …`` on the host (env via subprocess)."""
        del env  # applied by the caller's run_cmd
        return [str(ctx.repo_python()), "-m", "pytest", *pytest_args]


class DockerSandbox:
    """Run inside a network-less docker container.

    The repo is mounted read-write at /work (editable install keeps coverage
    paths pointing at the source tree); the entry script installs the project
    with its test extra, then runs pytest.

    ``--network=none`` means the container has NO PyPI access, so the
    configured image must pre-contain the project's build backend and test
    dependencies (e.g. hatchling + pytest + pytest-cov); the install runs
    with ``--no-build-isolation`` for exactly that reason. The bare default
    image fails visibly otherwise — degradation is never silent.
    """

    name = "docker"

    def __init__(self, image: str, root: Path, docker: str = "docker") -> None:
        self.image = image
        self.root = root
        self.docker = docker

    def build_pytest_argv(
        self,
        ctx: RunContext,
        pytest_args: list[str],
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """``docker run --rm --network=none …`` wrapping install + pytest."""
        inner = (
            "pip install --quiet --disable-pip-version-check "
            "--no-build-isolation -e '.[test]' && "
            f"python -m pytest {shlex.join(pytest_args)}"
        )
        env_flags: list[str] = []
        for key, value in (env or {}).items():
            env_flags += ["-e", f"{key}={value}"]
        return [
            self.docker,
            "run",
            "--rm",
            "--network=none",
            "--cpus=2",
            "--memory=2g",
            *env_flags,
            "-v",
            f"{self.root}:/work",
            "-w",
            "/work",
            self.image,
            "sh",
            "-c",
            inner,
        ]


def unsandboxed_finding(action: Action) -> Finding:
    """The visibility finding for host execution.

    ``no-op`` when the host was the configured backend; ``ask-user`` (which
    blocks) when a requested docker sandbox silently would have degraded.
    """
    downgraded = action is Action.ASK_USER
    return Finding(
        id=make_id("sandbox", "downgraded" if downgraded else "unsandboxed", ""),
        layer=Layer.EXEC,
        tool="sandbox",
        severity=Severity.WARNING,
        action=action,
        file="",
        line=0,
        message=(
            "exec tier ran unsandboxed on the host"
            + (" (docker sandbox requested but unavailable)" if downgraded else "")
        ),
        fix_hint=(
            'set sandbox = "docker" in .gauntlet.toml and install docker'
            if not downgraded
            else 'start the docker daemon, or set sandbox = "none" explicitly'
        ),
    )


def docker_usable(ctx: RunContext) -> bool:
    """Whether the docker CLI exists and the daemon answers."""
    docker = ctx.find_tool("docker")
    if docker is None:
        return False
    probe = ctx.run_cmd(
        [docker, "version", "--format", "{{.Server.Version}}"],
        timeout=DOCKER_PROBE_TIMEOUT_S,
    )
    return probe.ok() and bool(probe.stdout.strip())


def select_sandbox(ctx: RunContext) -> tuple[Sandbox, list[Finding]]:
    """Pick the backend from config, degrading visibly (§5.2)."""
    mode = ctx.config.sandbox
    if mode == "docker":
        if docker_usable(ctx):
            docker = ctx.find_tool("docker") or "docker"
            return (
                DockerSandbox(ctx.config.sandbox_image, ctx.root, docker=docker),
                [],
            )
        return HostSandbox(), [unsandboxed_finding(Action.ASK_USER)]
    return HostSandbox(), [unsandboxed_finding(Action.NO_OP)]
