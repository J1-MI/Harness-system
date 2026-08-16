"""SandboxBackend abstraction.

Phase 3.2 shipped ``trusted_local`` only, deliberately deferring real
OS/container isolation to roadmap Phase 13 (Hardening and Recovery) — see
architecture review section 13's open question ("Windows에서 비신뢰
저장소를 어떤 격리 backend로 실행할 것인가?"). Phase 13 adds
``ContainerSandbox`` (Docker-based).

``TrustedLocalSandbox`` provides *no* isolation beyond what
``execution/process.py`` already gives (timeout, output cap, process-tree
kill, an explicitly-scrubbed environment) — it does **not** sandbox the
filesystem or network. Code must not claim otherwise: any request for a
stronger ``IsolationBackend`` fails closed with ``UnavailableSandboxError``
rather than silently running on ``trusted_local``.

``probe_capabilities()`` actually shells out to ``docker info`` (with a
short timeout) to decide whether ``CONTAINER`` is genuinely usable right
now — it does not just check that the ``docker`` binary exists on PATH.
A present-but-not-running Docker Desktop (the common case right after a
fresh boot, or in most CI containers) must report unavailable, not
"probably fine" — claiming a capability that isn't actually wired up and
working is exactly the kind of silent-downgrade risk section 7 warns
against. ``WSL2`` remains unimplemented and unavailable in this phase
(Docker Desktop's own internal WSL2 integration is not a general-purpose
WSL2 distro this module can exec arbitrary commands into) — still named
in ``IsolationBackend`` so the capability gap stays a structural,
detectable ``UnavailableSandboxError`` rather than a silent one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent_harness.domain.enums import IsolationBackend
from agent_harness.execution.process import ProcessLimits, ProcessResult, run_process

__all__ = [
    "SandboxCapabilities",
    "UnavailableSandboxError",
    "SandboxBackend",
    "TrustedLocalSandbox",
    "ContainerSandboxConfig",
    "ContainerSandbox",
    "probe_capabilities",
    "get_sandbox_backend",
]

# The handful of host env vars a Windows (or POSIX) child process needs to
# even start and talk to the Docker daemon over its local socket/named
# pipe — never task/command-specific values. The task's own ``env`` is
# passed *into the container* via ``-e``, never merged into this.
_DOCKER_WRAPPER_ENV_ALLOWLIST = (
    "PATH", "SystemRoot", "windir", "TEMP", "TMP", "USERPROFILE", "COMSPEC",
    "DOCKER_HOST", "DOCKER_CONTEXT", "HOME",
)


class UnavailableSandboxError(RuntimeError):
    """Raised when the requested IsolationBackend is not available.

    Fail-closed: callers must not downgrade to a weaker backend on their
    own when this is raised.
    """


@dataclass(frozen=True)
class SandboxCapabilities:
    available_backends: tuple[IsolationBackend, ...]
    probed_at: datetime


def _docker_wrapper_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _DOCKER_WRAPPER_ENV_ALLOWLIST if key in os.environ}


def _docker_available(*, probe_timeout_seconds: float = 5.0) -> bool:
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    try:
        result = subprocess.run(
            [docker_path, "info"],
            env=_docker_wrapper_env(),
            capture_output=True,
            timeout=probe_timeout_seconds,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def probe_capabilities() -> SandboxCapabilities:
    """Report what this process can actually back right now.

    ``CONTAINER`` is only reported available if ``docker info`` actually
    succeeds against a live daemon at probe time — not merely if a
    ``docker`` binary is on PATH. ``WSL2`` is unimplemented and never
    reported available (see module docstring). Claiming a capability that
    isn't actually wired up and working is exactly the kind of
    silent-downgrade risk section 7 warns against.
    """

    backends = [IsolationBackend.TRUSTED_LOCAL]
    if _docker_available():
        backends.append(IsolationBackend.CONTAINER)

    return SandboxCapabilities(
        available_backends=tuple(backends),
        probed_at=datetime.now(timezone.utc),
    )


class SandboxBackend(Protocol):
    backend_id: IsolationBackend

    def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], limits: ProcessLimits
    ) -> ProcessResult: ...


class TrustedLocalSandbox:
    """Runs commands as a plain native subprocess. No isolation guarantees.

    Suitable only for repositories/commands the deployment already trusts
    — see architecture review section 13: "native Windows execution
    backend: trusted_local 등급만 지원한다고 명시".
    """

    backend_id = IsolationBackend.TRUSTED_LOCAL

    def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], limits: ProcessLimits
    ) -> ProcessResult:
        return run_process(argv, cwd=cwd, env=env, limits=limits)


@dataclass(frozen=True)
class ContainerSandboxConfig:
    image: str = "python:3.12-slim"
    network: str = "none"
    read_only_root: bool = False
    extra_docker_args: tuple[str, ...] = field(default_factory=tuple)


class ContainerSandbox:
    """Runs commands inside a throwaway Docker container.

    The *outer* process this module spawns is ``docker run`` itself,
    executed through the same ``execution.process.run_process`` every
    other backend uses (timeout, output cap, Windows Job Object /
    POSIX process-group kill all apply to the ``docker`` CLI process
    exactly as they would to any other native process). Real isolation
    (filesystem, network, process namespace) is Docker's job, not this
    class's — this class's job is only to build the right ``docker run``
    invocation and never let the task's env leak into the wrapper.

    ``cwd`` is bind-mounted read-write at ``/workspace`` inside the
    container and used as the container's working directory. ``env`` is
    the task's own (already env-allowlist-scrubbed, per
    ``command_broker.execute_command``) environment, passed in via ``-e``
    — it is **not** reused for the outer ``docker`` process, which gets
    only ``_DOCKER_WRAPPER_ENV_ALLOWLIST`` from the real host environment
    (the minimum needed to find and talk to the Docker daemon).
    """

    backend_id = IsolationBackend.CONTAINER

    def __init__(self, config: ContainerSandboxConfig | None = None) -> None:
        self._config = config or ContainerSandboxConfig()

    def run(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], limits: ProcessLimits
    ) -> ProcessResult:
        docker_path = shutil.which("docker")
        if docker_path is None:
            raise UnavailableSandboxError("docker executable not found on PATH")

        container_workdir = "/workspace"
        docker_argv = [
            docker_path, "run", "--rm",
            "--network", self._config.network,
            "-v", f"{cwd}:{container_workdir}",
            "-w", container_workdir,
        ]
        for key, value in env.items():
            docker_argv += ["-e", f"{key}={value}"]
        if self._config.read_only_root:
            docker_argv.append("--read-only")
        docker_argv.extend(self._config.extra_docker_args)
        docker_argv.append(self._config.image)
        docker_argv.extend(argv)

        return run_process(docker_argv, cwd=cwd, env=_docker_wrapper_env(), limits=limits)


def get_sandbox_backend(requested: IsolationBackend, *, config: ContainerSandboxConfig | None = None) -> SandboxBackend:
    capabilities = probe_capabilities()
    if requested not in capabilities.available_backends:
        raise UnavailableSandboxError(
            f"IsolationBackend {requested!r} is not available "
            f"(available: {capabilities.available_backends!r}); refusing to "
            "silently downgrade"
        )
    if requested is IsolationBackend.TRUSTED_LOCAL:
        return TrustedLocalSandbox()
    if requested is IsolationBackend.CONTAINER:
        return ContainerSandbox(config)
    raise AssertionError(f"unreachable: {requested!r} was reported available but has no backend")
