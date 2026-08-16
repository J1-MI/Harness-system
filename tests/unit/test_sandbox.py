"""Tests for the sandbox backends (Phase 3.2 TrustedLocal, Phase 13 Container).

The Docker-availability tests run for real against whatever this host
actually has — no mocking of ``docker info`` itself — so
``test_probe_capabilities_is_fail_closed_when_docker_unavailable`` is a
genuine proof of fail-closed behavior in whatever environment CI/this
session happens to run in, not a simulated one. Actual container
*execution* tests are separately gated on Docker really being up
(``_docker_available()``) and will legitimately skip when it isn't.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_harness.domain.enums import IsolationBackend
from agent_harness.execution.process import ProcessLimits, ProcessResult
from agent_harness.execution.sandbox import (
    ContainerSandbox,
    ContainerSandboxConfig,
    TrustedLocalSandbox,
    UnavailableSandboxError,
    _docker_available,
    get_sandbox_backend,
    probe_capabilities,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_trusted_local_is_always_available():
    capabilities = probe_capabilities()
    assert IsolationBackend.TRUSTED_LOCAL in capabilities.available_backends


def test_get_sandbox_backend_returns_trusted_local():
    backend = get_sandbox_backend(IsolationBackend.TRUSTED_LOCAL)
    assert isinstance(backend, TrustedLocalSandbox)


def test_wsl2_is_never_reported_available():
    """WSL2 is deliberately unimplemented this phase (see module docstring)."""

    assert IsolationBackend.WSL2 not in probe_capabilities().available_backends
    with pytest.raises(UnavailableSandboxError):
        get_sandbox_backend(IsolationBackend.WSL2)


def test_container_backend_matches_real_docker_availability_on_this_host():
    """Whatever ``_docker_available()`` reports right now, ``probe_capabilities``/
    ``get_sandbox_backend`` must agree with it — proves the fail-closed
    wiring end to end without mocking the actual docker check."""

    really_available = _docker_available()
    capabilities = probe_capabilities()
    assert (IsolationBackend.CONTAINER in capabilities.available_backends) == really_available

    if really_available:
        backend = get_sandbox_backend(IsolationBackend.CONTAINER)
        assert isinstance(backend, ContainerSandbox)
    else:
        with pytest.raises(UnavailableSandboxError):
            get_sandbox_backend(IsolationBackend.CONTAINER)


def test_container_sandbox_raises_if_docker_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_harness.execution.sandbox.shutil.which", lambda name: None)
    sandbox = ContainerSandbox()
    with pytest.raises(UnavailableSandboxError):
        sandbox.run(["echo", "hi"], cwd=tmp_path, env={}, limits=ProcessLimits(timeout_seconds=5, max_output_bytes=1000))


def test_container_sandbox_builds_expected_docker_invocation(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run_process(argv, *, cwd, env, limits):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env
        return ProcessResult(
            argv=argv, exit_code=0, timed_out=False, output_cap_exceeded=False, duration_ms=1,
            stdout=b"", stderr=b"", stdout_truncated=False, stderr_truncated=False,
            started_at=_now(), completed_at=_now(),
        )

    monkeypatch.setattr("agent_harness.execution.sandbox.shutil.which", lambda name: r"C:\docker.exe")
    monkeypatch.setattr("agent_harness.execution.sandbox.run_process", fake_run_process)
    monkeypatch.setenv("PATH", r"C:\some\path")

    sandbox = ContainerSandbox(ContainerSandboxConfig(image="demo-image:latest"))
    sandbox.run(
        ["python", "-c", "print(1)"], cwd=tmp_path,
        env={"MY_SECRET": "value"}, limits=ProcessLimits(timeout_seconds=5, max_output_bytes=1000),
    )

    argv = captured["argv"]
    assert argv[0] == r"C:\docker.exe"
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "-v" in argv
    assert f"{tmp_path}:/workspace" in argv
    assert "-e" in argv and "MY_SECRET=value" in argv
    assert argv[-3:] == ["demo-image:latest", "python", "-c"] or "demo-image:latest" in argv
    assert "python" in argv and "-c" in argv and "print(1)" in argv
    # The task's env (MY_SECRET) must never leak into the outer wrapper env.
    assert "MY_SECRET" not in captured["env"]


def test_container_sandbox_never_reuses_task_env_for_the_docker_wrapper_process(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_harness.execution.sandbox.shutil.which", lambda name: r"C:\docker.exe")

    def fake_run_process(argv, *, cwd, env, limits):
        assert "TASK_SECRET" not in env
        return ProcessResult(
            argv=argv, exit_code=0, timed_out=False, output_cap_exceeded=False, duration_ms=1,
            stdout=b"", stderr=b"", stdout_truncated=False, stderr_truncated=False,
            started_at=_now(), completed_at=_now(),
        )

    monkeypatch.setattr("agent_harness.execution.sandbox.run_process", fake_run_process)
    sandbox = ContainerSandbox()
    sandbox.run(
        ["echo"], cwd=tmp_path, env={"TASK_SECRET": "leak-me-not"},
        limits=ProcessLimits(timeout_seconds=5, max_output_bytes=1000),
    )
