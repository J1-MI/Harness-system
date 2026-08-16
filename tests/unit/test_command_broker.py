"""Tests for the Command Broker (Phase 3.2): catalog + argv resolution + execution."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

from agent_harness.domain.models import CommandSpec
from agent_harness.execution.command_broker import (
    CommandCatalog,
    InvalidCommandParametersError,
    UnknownCommandError,
    execute_command,
    resolve_argv,
)
from agent_harness.execution.sandbox import TrustedLocalSandbox


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_spec(**overrides) -> CommandSpec:
    data = dict(
        command_id="print-args",
        executable_identity=sys.executable,
        argv_template=[sys.executable, "-c", "import sys; print(repr(sys.argv[1:]))", "{message}"],
        cwd_policy="WORKSPACE_ROOT",
        env_allowlist=["PATH"],
        timeout_seconds=10,
        policy_version="v1",
    )
    data.update(overrides)
    return CommandSpec(**data)


@pytest.fixture()
def catalog() -> CommandCatalog:
    cat = CommandCatalog()
    cat.register(make_spec())
    return cat


@pytest.fixture()
def sandbox() -> TrustedLocalSandbox:
    return TrustedLocalSandbox()


# ---------------------------------------------------------------------------
# Catalog: only registered command_ids run
# ---------------------------------------------------------------------------


def test_unknown_command_id_is_refused(catalog):
    with pytest.raises(UnknownCommandError):
        catalog.get("does-not-exist")


def test_execute_command_refuses_unregistered_id(catalog, sandbox, tmp_path):
    with pytest.raises(UnknownCommandError):
        execute_command(
            catalog,
            "not-in-catalog",
            {"message": "hi"},
            sandbox=sandbox,
            cwd=tmp_path,
            available_env=dict(os.environ),
            now=_utc_now(),
        )


# ---------------------------------------------------------------------------
# resolve_argv: parameter substitution
# ---------------------------------------------------------------------------


def test_resolve_argv_substitutes_parameters():
    spec = make_spec(argv_template=["git", "checkout", "{ref}"])
    argv = resolve_argv(spec, {"ref": "main"})
    assert argv == ["git", "checkout", "main"]


def test_resolve_argv_raises_for_missing_parameter():
    spec = make_spec(argv_template=["git", "checkout", "{ref}"])
    with pytest.raises(InvalidCommandParametersError):
        resolve_argv(spec, {})


def test_resolve_argv_ignores_unused_parameters():
    spec = make_spec(argv_template=["git", "status"])
    argv = resolve_argv(spec, {"unused": "value"})
    assert argv == ["git", "status"]


# ---------------------------------------------------------------------------
# Shell injection: parameter values are literal argv tokens, never shell syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malicious_value",
    [
        "; rm -rf / #",
        "$(whoami)",
        "`whoami`",
        "hello && echo injected",
        "a\nb",
        "--evil-flag",
    ],
)
def test_shell_metacharacters_in_parameters_are_never_interpreted(
    catalog, sandbox, tmp_path, malicious_value
):
    execution = execute_command(
        catalog,
        "print-args",
        {"message": malicious_value},
        sandbox=sandbox,
        cwd=tmp_path,
        available_env=dict(os.environ),
        now=_utc_now(),
    )
    assert execution.command_run.exit_code == 0
    # The child received the malicious string as exactly one argv element —
    # sys.argv[1:] is a one-item list containing it verbatim.
    stdout = execution.process_result.stdout.decode("utf-8", errors="replace")
    assert repr([malicious_value]) in stdout
    assert execution.command_run.resolved_argv[-1] == malicious_value


def test_resolve_argv_does_not_reinterpret_braces_in_parameter_values():
    spec = make_spec(argv_template=["echo", "{message}"])
    argv = resolve_argv(spec, {"message": "{not_a_real_placeholder}"})
    assert argv == ["echo", "{not_a_real_placeholder}"]


# ---------------------------------------------------------------------------
# Env scrub
# ---------------------------------------------------------------------------


def test_execute_command_only_passes_allowlisted_env_vars(sandbox, tmp_path):
    catalog = CommandCatalog()
    catalog.register(
        make_spec(
            command_id="read-env",
            argv_template=[
                sys.executable,
                "-c",
                "import os; print(os.environ.get('SECRET_VALUE', 'ABSENT'))",
            ],
            env_allowlist=["PATH"],
        )
    )
    env = dict(os.environ)
    env["SECRET_VALUE"] = "should-not-leak"

    execution = execute_command(
        catalog,
        "read-env",
        {},
        sandbox=sandbox,
        cwd=tmp_path,
        available_env=env,
        now=_utc_now(),
    )
    assert b"ABSENT" in execution.process_result.stdout
    assert b"should-not-leak" not in execution.process_result.stdout


# ---------------------------------------------------------------------------
# CommandRun construction + optional artifact persistence
# ---------------------------------------------------------------------------


def test_execute_command_builds_command_run(catalog, sandbox, tmp_path):
    execution = execute_command(
        catalog,
        "print-args",
        {"message": "hello"},
        sandbox=sandbox,
        cwd=tmp_path,
        available_env=dict(os.environ),
        now=_utc_now(),
    )
    assert execution.command_run.command_spec_id == "print-args"
    assert execution.command_run.exit_code == 0
    assert execution.command_run.stdout_artifact_ref is None
    assert execution.command_run.stderr_artifact_ref is None
    assert execution.stdout_artifact is None
    assert execution.stderr_artifact is None


def test_execute_command_writes_artifacts_when_data_root_given(catalog, sandbox, tmp_path):
    data_root = tmp_path / "data-root"
    execution = execute_command(
        catalog,
        "print-args",
        {"message": "hello"},
        sandbox=sandbox,
        cwd=tmp_path,
        available_env=dict(os.environ),
        now=_utc_now(),
        artifact_data_root=data_root,
    )
    assert execution.command_run.stdout_artifact_ref is not None
    assert execution.command_run.stderr_artifact_ref is not None
    assert execution.stdout_artifact is not None
    assert execution.stderr_artifact is not None
    assert execution.command_run.stdout_artifact_ref == execution.stdout_artifact.artifact_id
