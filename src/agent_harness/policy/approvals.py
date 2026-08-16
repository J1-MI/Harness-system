"""Binding a human Approval to a PolicyDecision — and refusing to let it
override what it structurally cannot.

Two invariants this module exists to enforce:

1. A ``DENY`` ``PolicyDecision`` (hard-coded invariant or ceiling denial)
   can never be turned into an allow by an ``Approval``, no matter who
   signs it — "사용자 승인으로도 hard deny를 우회할 수 없게 해야 한다"
   (architecture review section 3). Changing that requires a policy
   change, not an approval.
2. A stale ``Approval`` — expired, or bound to a different subject/policy
   digest than the one now in effect — is rejected outright, not silently
   treated as still valid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from agent_harness.domain.enums import PolicyOutcome
from agent_harness.domain.models import Approval, PolicyDecision
from agent_harness.domain.validation import validate_approval_binding

__all__ = ["PolicyApprovalError", "create_approval", "resolve_decision_with_approval"]


class PolicyApprovalError(RuntimeError):
    pass


def create_approval(
    decision: PolicyDecision,
    *,
    actor: str,
    decision_value: Literal["APPROVED", "REJECTED"],
    rationale: str,
    requested_at: datetime,
    decided_at: datetime,
    expires_at: datetime | None = None,
    single_use: bool = True,
    scope: list[str] | None = None,
) -> Approval:
    if decision.outcome is PolicyOutcome.DENY:
        raise PolicyApprovalError(
            f"PolicyDecision {decision.decision_id} is DENY; cannot create an "
            "Approval for it — hard/ceiling denials are not approval-"
            "overridable, change the policy ceiling instead"
        )
    return Approval(
        subject_type=decision.subject_type,
        subject_id=decision.subject_id,
        subject_digest=decision.subject_digest,
        policy_decision_id=decision.decision_id,
        policy_decision_digest=decision.integrity_digest,
        actor=actor,
        decision=decision_value,
        scope=scope if scope is not None else list(decision.approval_requirements),
        requested_at=requested_at,
        decided_at=decided_at,
        expires_at=expires_at,
        single_use=single_use,
        rationale=rationale,
    )


def resolve_decision_with_approval(
    decision: PolicyDecision, approval: Approval, *, now: datetime
) -> PolicyOutcome:
    """The effective outcome once ``approval`` is taken into account.

    Raises ``PolicyApprovalError`` if the approval cannot possibly apply:
    the decision is a hard/ceiling ``DENY`` (never approval-overridable),
    the approval is bound to a different subject/policy digest, or it has
    expired. A ``REJECTED`` approval or an unresolved ``DENY`` both surface
    as ``PolicyOutcome.DENY`` to the caller rather than raising, since
    those are valid (if negative) outcomes, not integrity failures.
    """

    if decision.outcome is PolicyOutcome.DENY:
        raise PolicyApprovalError(
            f"PolicyDecision {decision.decision_id} is DENY "
            f"({decision.reason_codes}); no approval can override a hard/"
            "ceiling denial"
        )

    validate_approval_binding(
        approval,
        expected_subject_digest=decision.subject_digest,
        expected_policy_decision_digest=decision.integrity_digest,
    )

    if approval.expires_at is not None and now > approval.expires_at:
        raise PolicyApprovalError(
            f"approval {approval.approval_id} expired at "
            f"{approval.expires_at.isoformat()} (now={now.isoformat()}) — "
            "request a fresh approval"
        )

    if approval.decision == "REJECTED":
        return PolicyOutcome.DENY

    # decision.outcome is ALLOW or REQUIRE_APPROVAL here; either way, a
    # valid, unexpired, correctly-bound APPROVED approval resolves to ALLOW.
    return PolicyOutcome.ALLOW
