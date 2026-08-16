"""Tests for the freeze + host-validation pipeline (Phase 3.3)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import EvidenceTrustTier
from agent_harness.domain.models import CommandSpec
from agent_harness.execution.command_broker import CommandCatalog
from agent_harness.execution.evidence import (
    build_command_evidence,
    freeze_and_validate,
    freeze_manifest,
    run_approved_checks,
)
from agent_harness.execution.sandbox import TrustedLocalSandbox
from agent_harness.persistence.artifacts import read_blob


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_echo_spec(command_id: str = "run-tests", exit_code: int = 0) -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        executable_identity=sys.executable,
        argv_template=[
            sys.executable, "-c",
            f"import sys; print('check output'); sys.exit({exit_code})",
        ],
        cwd_policy="WORKSPACE_ROOT",
        env_allowlist=["PATH"],
        timeout_seconds=10,
        policy_version="v1",
    )


# ---------------------------------------------------------------------------
# freeze_manifest
# ---------------------------------------------------------------------------


def test_freeze_manifest_round_trips_through_artifact_store(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.txt").write_bytes(b"hello")

    data_root = tmp_path / "data-root"
    entries, artifact = freeze_manifest(worktree, data_root=data_root, now=_utc_now())

    assert len(entries) == 1
    assert entries[0].relative_path == "a.txt"

    stored = json.loads(read_blob(data_root, artifact))
    assert stored == [
        {
            "relative_path": "a.txt",
            "file_type": "file",
            "mode": entries[0].mode,
            "size": 5,
            "sha256": entries[0].sha256,
            "symlink_target": None,
        }
    ]


# ---------------------------------------------------------------------------
# run_approved_checks / build_command_evidence
# ---------------------------------------------------------------------------


def test_run_approved_checks_executes_registered_commands(tmp_path):
    catalog = CommandCatalog()
    catalog.register(make_echo_spec())
    data_root = tmp_path / "data-root"

    executions = run_approved_checks(
        catalog,
        ["run-tests"],
        sandbox=TrustedLocalSandbox(),
        cwd=tmp_path,
        available_env=dict(os.environ),
        now=_utc_now(),
        data_root=data_root,
    )
    assert len(executions) == 1
    assert executions[0].command_run.exit_code == 0
    assert b"check output" in executions[0].process_result.stdout


def test_build_command_evidence_is_host_observed_and_self_consistent(tmp_path):
    catalog = CommandCatalog()
    catalog.register(make_echo_spec())
    data_root = tmp_path / "data-root"

    executions = run_approved_checks(
        catalog,
        ["run-tests"],
        sandbox=TrustedLocalSandbox(),
        cwd=tmp_path,
        available_env=dict(os.environ),
        now=_utc_now(),
        data_root=data_root,
    )
    run_id = new_id()
    task_id = new_id()
    evidence = build_command_evidence(run_id, task_id, executions[0], now=_utc_now())

    assert len(evidence) == 2  # stdout + stderr
    for record in evidence:
        assert record.provenance.trust_tier is EvidenceTrustTier.HOST_OBSERVED
        assert record.run_id == run_id
        assert record.task_id == task_id


# ---------------------------------------------------------------------------
# freeze_and_validate: end-to-end pipeline
# ---------------------------------------------------------------------------


def test_freeze_and_validate_detects_changes_and_test_mutation(tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert False\n")
    (worktree / "src.py").write_bytes(b"def f(): return 1\n")

    data_root = tmp_path / "data-root"
    baseline_manifest, baseline_artifact = freeze_manifest(
        worktree, data_root=data_root, now=_utc_now()
    )

    # Simulate the worker "fixing" the test instead of the source.
    (worktree / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert True\n")
    (worktree / "new_file.py").write_bytes(b"# added by worker\n")

    catalog = CommandCatalog()
    catalog.register(make_echo_spec("run-tests"))

    result = freeze_and_validate(
        new_id(),
        new_id(),
        worktree,
        baseline_manifest,
        baseline_artifact,
        catalog=catalog,
        check_command_ids=["run-tests"],
        test_path_patterns=["tests/**"],
        sandbox=TrustedLocalSandbox(),
        available_env=dict(os.environ),
        now=_utc_now(),
        data_root=data_root,
    )

    assert "tests/test_thing.py" in result.manifest_diff.modified
    assert "new_file.py" in result.manifest_diff.added
    assert result.test_mutations == ["tests/test_thing.py"]
    assert len(result.check_executions) == 1
    assert len(result.evidence) == 2
    assert result.baseline_artifact.content_digest != result.result_artifact.content_digest


def test_freeze_and_validate_computes_scope_violations_when_scope_given(tmp_path):
    """Codex review M-01: find_scope_violations must actually run inside
    freeze_and_validate, not just exist as a standalone function."""

    from agent_harness.domain.models import ScopeRules

    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "main.py").write_bytes(b"print('hi')\n")

    data_root = tmp_path / "data-root"
    baseline_manifest, baseline_artifact = freeze_manifest(worktree, data_root=data_root, now=_utc_now())

    (worktree / "secrets").mkdir()
    (worktree / "secrets" / "prod.key").write_bytes(b"leaked\n")

    catalog = CommandCatalog()
    result = freeze_and_validate(
        new_id(), new_id(), worktree, baseline_manifest, baseline_artifact,
        catalog=catalog, check_command_ids=[], test_path_patterns=["tests/**"],
        sandbox=TrustedLocalSandbox(), available_env=dict(os.environ), now=_utc_now(),
        data_root=data_root,
        scope=ScopeRules(allowed_path_rules=["src/**"], forbidden_path_rules=[]),
    )

    assert any("secrets/prod.key" in v for v in result.scope_violations)


def test_freeze_and_validate_skips_scope_violations_when_scope_not_given(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "outside.py").write_bytes(b"x\n")
    data_root = tmp_path / "data-root"
    baseline_manifest, baseline_artifact = freeze_manifest(worktree, data_root=data_root, now=_utc_now())

    catalog = CommandCatalog()
    result = freeze_and_validate(
        new_id(), new_id(), worktree, baseline_manifest, baseline_artifact,
        catalog=catalog, check_command_ids=[], test_path_patterns=["tests/**"],
        sandbox=TrustedLocalSandbox(), available_env=dict(os.environ), now=_utc_now(),
        data_root=data_root,
    )
    assert result.scope_violations == []


def test_freeze_and_validate_detects_test_side_effects(tmp_path):
    """Codex review B-02: a check command that mutates the worktree as a
    side effect of running (e.g. a malicious/buggy test script writing a
    file) must not be invisible just because the pre-test freeze already
    happened — a second, post-test freeze is needed to catch it."""

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "src.py").write_bytes(b"def f(): return 1\n")

    data_root = tmp_path / "data-root"
    baseline_manifest, baseline_artifact = freeze_manifest(worktree, data_root=data_root, now=_utc_now())

    catalog = CommandCatalog()
    catalog.register(
        CommandSpec(
            command_id="mutating-check", executable_identity=sys.executable,
            argv_template=[
                sys.executable, "-c",
                "open('planted_by_test.txt', 'w').write('sneaky')",
            ],
            cwd_policy="WORKSPACE_ROOT", env_allowlist=["PATH"], timeout_seconds=10, policy_version="v1",
        )
    )

    result = freeze_and_validate(
        new_id(), new_id(), worktree, baseline_manifest, baseline_artifact,
        catalog=catalog, check_command_ids=["mutating-check"], test_path_patterns=["tests/**"],
        sandbox=TrustedLocalSandbox(), available_env=dict(os.environ), now=_utc_now(),
        data_root=data_root,
    )

    assert "planted_by_test.txt" in result.test_side_effects
    # The pre-test manifest_diff/result_manifest never saw it — this is
    # exactly why a second freeze is needed.
    assert "planted_by_test.txt" not in result.manifest_diff.added
    assert result.post_test_artifact is not None
    assert result.post_test_artifact.content_digest != result.result_artifact.content_digest


def test_freeze_and_validate_no_test_mutation_when_only_source_changes(tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert True\n")
    (worktree / "src.py").write_bytes(b"def f(): return 1\n")

    data_root = tmp_path / "data-root"
    baseline_manifest, baseline_artifact = freeze_manifest(
        worktree, data_root=data_root, now=_utc_now()
    )

    (worktree / "src.py").write_bytes(b"def f(): return 2\n")

    catalog = CommandCatalog()
    catalog.register(make_echo_spec("run-tests"))

    result = freeze_and_validate(
        new_id(),
        new_id(),
        worktree,
        baseline_manifest,
        baseline_artifact,
        catalog=catalog,
        check_command_ids=["run-tests"],
        test_path_patterns=["tests/**"],
        sandbox=TrustedLocalSandbox(),
        available_env=dict(os.environ),
        now=_utc_now(),
        data_root=data_root,
    )

    assert result.manifest_diff.modified == ["src.py"]
    assert result.test_mutations == []
