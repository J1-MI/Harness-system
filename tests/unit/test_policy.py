"""Tests for the Policy and Approval Engine (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import PolicyOutcome, SubjectType
from agent_harness.domain.validation import InvariantViolation
from agent_harness.policy.approvals import (
    PolicyApprovalError,
    create_approval,
    resolve_decision_with_approval,
)
from agent_harness.policy.commands import intersect_command_ids, rejected_command_ids
from agent_harness.policy.evaluator import evaluate_policy
from agent_harness.policy.paths import intersect_scope
from tests.factories import make_policy_ceiling, make_requested_capabilities, make_scope


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _evaluate(requested_capabilities=None, requested_scope=None, ceiling=None):
    return evaluate_policy(
        SubjectType.TASK_CONTRACT,
        new_id(),
        "sha256:" + "0" * 64,
        requested_capabilities or make_requested_capabilities(),
        requested_scope or make_scope(),
        ceiling=ceiling or make_policy_ceiling(),
        evaluated_at=_utc_now(),
    )


# ---------------------------------------------------------------------------
# intersect_scope / intersect_command_ids
# ---------------------------------------------------------------------------


def test_intersect_scope_narrows_to_common_allowed_paths():
    requested = make_scope(allowed_path_rules=["src/**", "tests/**", "docs/**"])
    ceiling = make_scope(allowed_path_rules=["src/**", "tests/**"])
    result = intersect_scope(requested, ceiling)
    assert set(result.allowed_path_rules) == {"src/**", "tests/**"}


def test_intersect_scope_unions_forbidden_paths():
    requested = make_scope(forbidden_path_rules=[".git/**"])
    ceiling = make_scope(forbidden_path_rules=["secrets/**"])
    result = intersect_scope(requested, ceiling)
    assert set(result.forbidden_path_rules) == {".git/**", "secrets/**"}


def test_intersect_scope_takes_stricter_numeric_ceiling():
    requested = make_scope(max_changed_files=100)
    ceiling = make_scope(max_changed_files=10)
    result = intersect_scope(requested, ceiling)
    assert result.max_changed_files == 10


def test_intersect_command_ids_is_intersection():
    assert intersect_command_ids(["pytest", "make"], frozenset({"pytest", "npm"})) == ["pytest"]


def test_rejected_command_ids_is_difference():
    assert rejected_command_ids(["pytest", "make"], frozenset({"pytest"})) == ["make"]


# ---------------------------------------------------------------------------
# evaluate_policy: ALLOW / REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_plain_workspace_request_within_ceiling_is_allowed():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(command_ids=["pytest"]),
    )
    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.grants.command_ids == ["pytest"]


def test_network_access_requires_approval_even_within_ceiling():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert "network_access" in decision.approval_requirements
    # The domain is still recorded in grants — approval is a gate on
    # *proceeding*, not on what the Policy Engine computed as grantable.
    assert decision.grants.network_rules == ["pypi.org"]


def test_raw_shell_requires_approval_when_ceiling_allows_it():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(raw_shell=True),
        ceiling=make_policy_ceiling(raw_shell_allowed=True),
    )
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert "raw_shell" in decision.approval_requirements


# ---------------------------------------------------------------------------
# evaluate_policy: DENY (ceiling-level)
# ---------------------------------------------------------------------------


def test_command_outside_ceiling_allowlist_is_denied():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(command_ids=["curl"]),
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert any("CEILING_FORBIDS_COMMANDS" in r for r in decision.reason_codes)


def test_raw_shell_denied_when_ceiling_forbids_it():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(raw_shell=True),
        ceiling=make_policy_ceiling(raw_shell_allowed=False),
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert "CEILING_FORBIDS_RAW_SHELL" in decision.reason_codes


def test_package_install_denied_when_ceiling_forbids_it():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(package_install=True),
        ceiling=make_policy_ceiling(package_install_allowed=False),
    )
    assert decision.outcome is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# evaluate_policy: hard-coded invariant cannot be overridden by ceiling
# ---------------------------------------------------------------------------


def test_metadata_endpoint_is_denied_even_when_ceiling_explicitly_allows_it():
    ceiling = make_policy_ceiling(
        allowed_network_domains=frozenset({"169.254.169.254", "pypi.org"})
    )
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(
            network_domains=["169.254.169.254"]
        ),
        ceiling=ceiling,
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert any("HARD_DENY_NETWORK_METADATA_ENDPOINT" in r for r in decision.reason_codes)
    assert "169.254.169.254" not in decision.grants.network_rules


# ---------------------------------------------------------------------------
# Approval: hard deny cannot be overridden
# ---------------------------------------------------------------------------


def test_create_approval_refuses_a_deny_decision():
    decision = _evaluate(requested_capabilities=make_requested_capabilities(command_ids=["curl"]))
    assert decision.outcome is PolicyOutcome.DENY
    with pytest.raises(PolicyApprovalError):
        create_approval(
            decision,
            actor="admin@example.com",
            decision_value="APPROVED",
            rationale="I really want this",
            requested_at=_utc_now(),
            decided_at=_utc_now(),
        )


def test_resolve_decision_with_approval_rejects_hard_deny_even_with_forged_approval():
    """Even a hand-built, correctly-bound 'APPROVED' Approval must not
    flip a DENY decision — proving hard-deny cannot be approval-bypassed."""

    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(
            network_domains=["169.254.169.254"]
        ),
        ceiling=make_policy_ceiling(
            allowed_network_domains=frozenset({"169.254.169.254"})
        ),
    )
    assert decision.outcome is PolicyOutcome.DENY

    from agent_harness.domain.models import Approval

    forged_approval = Approval(
        subject_type=decision.subject_type,
        subject_id=decision.subject_id,
        subject_digest=decision.subject_digest,
        policy_decision_id=decision.decision_id,
        policy_decision_digest=decision.integrity_digest,
        actor="admin@example.com",
        decision="APPROVED",
        requested_at=_utc_now(),
        decided_at=_utc_now(),
        rationale="approving anyway",
    )
    with pytest.raises(PolicyApprovalError):
        resolve_decision_with_approval(decision, forged_approval, now=_utc_now())


# ---------------------------------------------------------------------------
# Approval: stale (expired) approval rejected
# ---------------------------------------------------------------------------


def test_resolve_decision_with_approval_rejects_expired_approval():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL

    approval = create_approval(
        decision,
        actor="admin@example.com",
        decision_value="APPROVED",
        rationale="fine for now",
        requested_at=_utc_now() - timedelta(hours=2),
        decided_at=_utc_now() - timedelta(hours=2),
        expires_at=_utc_now() - timedelta(hours=1),  # already expired
    )
    with pytest.raises(PolicyApprovalError):
        resolve_decision_with_approval(decision, approval, now=_utc_now())


def test_resolve_decision_with_approval_accepts_unexpired_approval():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    approval = create_approval(
        decision,
        actor="admin@example.com",
        decision_value="APPROVED",
        rationale="fine",
        requested_at=_utc_now(),
        decided_at=_utc_now(),
        expires_at=_utc_now() + timedelta(hours=1),
    )
    outcome = resolve_decision_with_approval(decision, approval, now=_utc_now())
    assert outcome is PolicyOutcome.ALLOW


def test_resolve_decision_with_approval_rejects_mismatched_subject_digest():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    approval = create_approval(
        decision,
        actor="admin@example.com",
        decision_value="APPROVED",
        rationale="fine",
        requested_at=_utc_now(),
        decided_at=_utc_now(),
    )
    tampered = approval.model_copy(update={"subject_digest": "sha256:" + "9" * 64})
    with pytest.raises(InvariantViolation):
        resolve_decision_with_approval(decision, tampered, now=_utc_now())


def test_resolve_decision_with_approval_honors_rejection():
    decision = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    approval = create_approval(
        decision,
        actor="admin@example.com",
        decision_value="REJECTED",
        rationale="too risky",
        requested_at=_utc_now(),
        decided_at=_utc_now(),
    )
    outcome = resolve_decision_with_approval(decision, approval, now=_utc_now())
    assert outcome is PolicyOutcome.DENY


def test_approval_does_not_apply_to_a_re_evaluated_decision():
    """Re-evaluating policy (e.g. after a contract revision) produces a new
    ``PolicyDecision`` with a new ``integrity_digest`` — an approval bound
    to the old decision must not silently carry over to the new one."""

    decision_v1 = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    approval = create_approval(
        decision_v1,
        actor="admin@example.com",
        decision_value="APPROVED",
        rationale="fine",
        requested_at=_utc_now(),
        decided_at=_utc_now(),
    )

    decision_v2 = _evaluate(
        requested_capabilities=make_requested_capabilities(network_domains=["pypi.org"])
    )
    assert decision_v1.integrity_digest != decision_v2.integrity_digest
    with pytest.raises(InvariantViolation):
        resolve_decision_with_approval(decision_v2, approval, now=_utc_now())
