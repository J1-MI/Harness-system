"""Threat-model attack fixtures (Phase 13): "위협 모델 공격 fixture 통과".

Each test below is named after a row in the architecture review's "공격
표면별 통제" table and exercises the *actual* prevention/detection
control already built across earlier phases end to end — not a mocked or
re-derived version of it. Where a lower-level unit test already proves
the same mechanism in isolation (noted per test), this suite composes it
into a more realistic attacker narrative instead of duplicating it.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import ArtifactMediaKind, PolicyOutcome, RedactionStatus, SubjectType
from agent_harness.domain.models import CommandSpec, ScopeRules
from agent_harness.execution.command_broker import CommandCatalog, execute_command
from agent_harness.execution.sandbox import TrustedLocalSandbox
from agent_harness.execution.scope_guard import find_scope_violations
from agent_harness.execution.validation import build_file_manifest, diff_manifests
from agent_harness.persistence.artifacts import read_blob
from agent_harness.policy.evaluator import evaluate_policy
from tests.factories import make_policy_ceiling, make_requested_capabilities, make_scope


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Attack #3: path traversal — a malicious contract tries to declare a
# scope rule that escapes the worktree root.
# ---------------------------------------------------------------------------


def test_path_traversal_pattern_is_rejected_at_the_model_boundary():
    with pytest.raises(ValidationError):
        ScopeRules(allowed_path_rules=["../../etc/passwd"], forbidden_path_rules=[])

    with pytest.raises(ValidationError):
        ScopeRules(allowed_path_rules=["/etc/passwd"], forbidden_path_rules=[])


# ---------------------------------------------------------------------------
# Attack #4: shell command injection via a typed CommandSpec parameter —
# proves the injected metacharacters never spawn a second process, using a
# real marker file rather than just inspecting the resolved argv (the
# lower-level argv-shape proof already lives in
# tests/unit/test_command_broker.py::test_shell_metacharacters_in_parameters_are_never_interpreted).
# ---------------------------------------------------------------------------


def test_shell_injection_attempt_never_spawns_a_second_process(tmp_path):
    marker = tmp_path / "pwned.txt"
    catalog = CommandCatalog()
    catalog.register(
        CommandSpec(
            command_id="echo-message", executable_identity=sys.executable,
            argv_template=[sys.executable, "-c", "import sys; print(sys.argv[1])", "{message}"],
            cwd_policy="WORKSPACE_ROOT", env_allowlist=[], timeout_seconds=10, policy_version="v1",
        )
    )
    malicious_payload = f"hi; touch {marker}; $({marker.as_posix()})"

    execution = execute_command(
        catalog, "echo-message", {"message": malicious_payload},
        sandbox=TrustedLocalSandbox(), cwd=tmp_path, available_env={}, now=_utc_now(),
    )

    assert execution.process_result.exit_code == 0
    assert malicious_payload.encode() in execution.process_result.stdout
    assert not marker.exists()  # the shell metacharacters were never interpreted


# ---------------------------------------------------------------------------
# Attack #6/#7: secret leakage into stored logs/artifacts.
# ---------------------------------------------------------------------------


def test_secret_printed_by_a_command_is_redacted_before_storage(tmp_path):
    catalog = CommandCatalog()
    fake_token = "ghp_" + "a" * 36
    catalog.register(
        CommandSpec(
            command_id="leak-secret", executable_identity=sys.executable,
            argv_template=[sys.executable, "-c", f"print('token={fake_token}')"],
            cwd_policy="WORKSPACE_ROOT", env_allowlist=[], timeout_seconds=10, policy_version="v1",
        )
    )
    data_root = tmp_path / "data-root"

    execution = execute_command(
        catalog, "leak-secret", {}, sandbox=TrustedLocalSandbox(), cwd=tmp_path,
        available_env={}, now=_utc_now(), artifact_data_root=data_root,
    )

    assert fake_token.encode() in execution.process_result.stdout  # the raw process output does contain it
    assert execution.stdout_artifact is not None
    assert execution.stdout_artifact.redaction_status is RedactionStatus.REDACTED
    stored = read_blob(data_root, execution.stdout_artifact)
    assert fake_token.encode() not in stored  # but the persisted artifact never does
    assert b"[REDACTED]" in stored


# ---------------------------------------------------------------------------
# Attack: cloud metadata SSRF (hard-coded invariant — section "Policy 및
# Approval": "사용자 승인으로도 hard deny를 우회할 수 없다").
# ---------------------------------------------------------------------------


def test_cloud_metadata_endpoint_denied_even_with_a_maximally_permissive_ceiling():
    malicious_request = make_requested_capabilities(network_domains=["169.254.169.254"])
    permissive_ceiling = make_policy_ceiling(
        allowed_network_domains=frozenset({"169.254.169.254", "pypi.org"}),
        raw_shell_allowed=True, package_install_allowed=True,
    )

    decision = evaluate_policy(
        SubjectType.TASK_CONTRACT, new_id(), "sha256:" + "0" * 64,
        malicious_request, make_scope(), ceiling=permissive_ceiling, evaluated_at=_utc_now(),
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert any("METADATA" in code for code in decision.reason_codes)


# ---------------------------------------------------------------------------
# Attack #8: excessive Task Contract capability request — a compromised
# or malicious Planner asks for raw_shell the deployment never granted.
# ---------------------------------------------------------------------------


def test_raw_shell_request_denied_without_ceiling_grant():
    request = make_requested_capabilities(raw_shell=True)
    ceiling = make_policy_ceiling(raw_shell_allowed=False)

    decision = evaluate_policy(
        SubjectType.TASK_CONTRACT, new_id(), "sha256:" + "0" * 64,
        request, make_scope(), ceiling=ceiling, evaluated_at=_utc_now(),
    )

    assert decision.outcome is PolicyOutcome.DENY
    assert "CEILING_FORBIDS_RAW_SHELL" in decision.reason_codes


# ---------------------------------------------------------------------------
# Attack #2/#14: malicious script tries to flood stdout past the cap.
# ---------------------------------------------------------------------------


def test_output_flood_is_capped_not_silently_buffered_in_full(tmp_path):
    catalog = CommandCatalog()
    catalog.register(
        CommandSpec(
            command_id="flood", executable_identity=sys.executable,
            argv_template=[sys.executable, "-c", "import sys; sys.stdout.write('A' * 5_000_000)"],
            cwd_policy="WORKSPACE_ROOT", env_allowlist=[], timeout_seconds=10, policy_version="v1",
        )
    )

    execution = execute_command(
        catalog, "flood", {}, sandbox=TrustedLocalSandbox(), cwd=tmp_path,
        available_env={}, now=_utc_now(), max_output_bytes=10_000,
    )

    # ``output_cap_exceeded`` only means "the process was still running
    # when the harness observed the cap and force-killed it" — a fast
    # process that finishes writing before the next poll tick exits on
    # its own instead. ``stdout_truncated`` is the field that actually
    # guarantees the harness never buffered the full 5,000,000 bytes.
    assert execution.process_result.stdout_truncated is True
    assert len(execution.process_result.stdout) <= 10_000


# ---------------------------------------------------------------------------
# Attack #9: Claude/Worker writes outside the contract's declared scope —
# exercised end to end through the real filesystem manifest pipeline.
# ---------------------------------------------------------------------------


def test_out_of_scope_write_is_flagged_by_the_deterministic_scope_guard(tmp_path):
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "main.py").write_bytes(b"print('hi')\n")
    baseline = build_file_manifest(worktree)

    # The "attack": a change lands under secrets/, never declared in scope.
    (worktree / "secrets").mkdir()
    (worktree / "secrets" / "prod.key").write_bytes(b"super-secret-key-material\n")
    (worktree / "src" / "main.py").write_bytes(b"print('changed')\n")
    result_manifest = build_file_manifest(worktree)

    diff = diff_manifests(baseline, result_manifest)
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    violations = find_scope_violations(diff, scope)
    assert any("secrets/prod.key" in v for v in violations)
    # The in-scope change is not flagged.
    assert not any("src/main.py" in v for v in violations)
