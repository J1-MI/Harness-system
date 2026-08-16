"""Unit tests for domain contract models and cross-model invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_harness.domain.digests import compute_model_digest
from agent_harness.domain.enums import (
    ActorType,
    CriterionResult,
    EvidenceTrustTier,
    FindingSeverity,
    PolicyOutcome,
    VerificationDecision,
    WorkerResultStatus,
)
from agent_harness.domain.models import (
    EvidenceProvenance,
    Finding,
    PolicyDecision,
    TaskContract,
)
from agent_harness.domain.validation import (
    InvariantViolation,
    assert_rework_scope_is_subset,
    assert_valid_pass,
    is_scope_subset,
    validate_approval_binding,
)
from tests.factories import (
    VALID_DIGEST,
    make_acceptance_criterion,
    make_approval,
    make_criterion_verification,
    make_evidence_record,
    make_policy_decision,
    make_requested_capabilities,
    make_rework_contract,
    make_scope,
    make_task_contract,
    make_verification_result,
    make_worker_result,
)


# ---------------------------------------------------------------------------
# 1. valid TaskContract + deterministic digest
# ---------------------------------------------------------------------------


def test_valid_task_contract_has_deterministic_digest():
    contract = make_task_contract()

    digest_a = compute_model_digest(contract, exclude_fields={"integrity"})
    digest_b = compute_model_digest(contract, exclude_fields={"integrity"})

    assert digest_a == digest_b
    assert digest_a.startswith("sha256:")
    assert len(digest_a) == len("sha256:") + 64


def test_task_contract_digest_changes_with_content():
    shared_ids = {"run_id": "run-fixed", "task_id": "task-fixed"}
    contract_a = make_task_contract(objective="Do the first thing", **shared_ids)
    contract_b = make_task_contract(objective="Do a different thing", **shared_ids)

    exclude = {"integrity", "contract_id"}
    digest_a = compute_model_digest(contract_a, exclude_fields=exclude)
    digest_b = compute_model_digest(contract_b, exclude_fields=exclude)

    assert digest_a != digest_b


# ---------------------------------------------------------------------------
# 2. extra field rejection
# ---------------------------------------------------------------------------


def test_task_contract_rejects_extra_fields():
    payload = make_task_contract().model_dump(mode="json")
    payload["unexpected_field"] = "not allowed"

    with pytest.raises(ValidationError):
        TaskContract.model_validate(payload)


def test_policy_decision_rejects_extra_fields():
    payload = make_policy_decision().model_dump(mode="json")
    payload["granted_by_mistake"] = True

    with pytest.raises(ValidationError):
        PolicyDecision.model_validate(payload)


# ---------------------------------------------------------------------------
# 3. path rejection lives in tests/unit/test_digests.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. TaskContract has no effective-grant field
# ---------------------------------------------------------------------------


def test_task_contract_has_no_effective_capability_field():
    forbidden_field_names = {
        "approval_required",
        "granted_capabilities",
        "effective_capabilities",
        "grants",
    }
    assert forbidden_field_names.isdisjoint(TaskContract.model_fields.keys())
    assert "requested_capabilities" in TaskContract.model_fields


def test_task_contract_requested_capabilities_do_not_imply_grant():
    contract = make_task_contract(
        requested_capabilities=make_requested_capabilities(raw_shell=True)
    )
    # Requesting raw_shell does not make the contract carry any grant;
    # only a separately-modeled PolicyDecision.grants could do that.
    assert contract.requested_capabilities.raw_shell is True
    assert not hasattr(contract, "grants")


# ---------------------------------------------------------------------------
# 5. PolicyDecision.grants model validation
# ---------------------------------------------------------------------------


def test_policy_decision_deny_requires_reason_codes():
    with pytest.raises(ValidationError):
        make_policy_decision(outcome=PolicyOutcome.DENY, reason_codes=[])


def test_policy_decision_require_approval_requires_requirements():
    with pytest.raises(ValidationError):
        make_policy_decision(
            outcome=PolicyOutcome.REQUIRE_APPROVAL, approval_requirements=[]
        )


def test_policy_decision_allow_is_valid_without_reason_codes():
    decision = make_policy_decision(outcome=PolicyOutcome.ALLOW)
    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.grants.sandbox_profile == "trusted_local"


# ---------------------------------------------------------------------------
# 6. Approval subject digest mismatch
# ---------------------------------------------------------------------------


def test_approval_binding_matches_exactly():
    approval = make_approval(
        subject_digest=VALID_DIGEST, policy_decision_digest=VALID_DIGEST
    )
    validate_approval_binding(
        approval,
        expected_subject_digest=VALID_DIGEST,
        expected_policy_decision_digest=VALID_DIGEST,
    )


def test_approval_binding_rejects_subject_digest_mismatch():
    other_digest = "sha256:" + "9" * 64
    approval = make_approval(
        subject_digest=other_digest, policy_decision_digest=VALID_DIGEST
    )
    with pytest.raises(InvariantViolation):
        validate_approval_binding(
            approval,
            expected_subject_digest=VALID_DIGEST,
            expected_policy_decision_digest=VALID_DIGEST,
        )


# ---------------------------------------------------------------------------
# 7. WorkerResult provider-reported semantics
# ---------------------------------------------------------------------------


def test_worker_result_uses_reported_prefixed_fields():
    field_names = set(type(make_worker_result()).model_fields.keys())
    claim_fields = {
        name
        for name in field_names
        if "changed_files" in name or "commands" in name or "tests" in name
    }
    assert claim_fields == {
        "reported_changed_files",
        "reported_commands",
        "reported_tests",
    }


def test_worker_result_blocked_requires_reason():
    with pytest.raises(ValidationError):
        make_worker_result(status=WorkerResultStatus.BLOCKED, blocked_reason=None)


def test_worker_result_failed_requires_reason():
    with pytest.raises(ValidationError):
        make_worker_result(status=WorkerResultStatus.FAILED, blocked_reason="")


def test_worker_result_completed_does_not_require_reason():
    result = make_worker_result(status=WorkerResultStatus.COMPLETED)
    assert result.blocked_reason is None


# ---------------------------------------------------------------------------
# 8. VerificationResult invalid PASS rejection
# ---------------------------------------------------------------------------


def test_pass_is_valid_when_all_mandatory_criteria_pass():
    contract = make_task_contract(
        acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)]
    )
    result = make_verification_result(
        decision=VerificationDecision.PASS,
        criteria=[make_criterion_verification("crit-1", CriterionResult.PASS)],
    )
    assert_valid_pass(contract, result)  # must not raise


def test_pass_rejected_when_mandatory_criterion_not_verified():
    contract = make_task_contract(
        acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)]
    )
    result = make_verification_result(
        decision=VerificationDecision.PASS,
        criteria=[
            make_criterion_verification("crit-1", CriterionResult.NOT_VERIFIED)
        ],
    )
    with pytest.raises(InvariantViolation):
        assert_valid_pass(contract, result)


def test_pass_rejected_with_unresolved_blocker_finding():
    contract = make_task_contract(
        acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)]
    )
    result = make_verification_result(
        decision=VerificationDecision.PASS,
        criteria=[make_criterion_verification("crit-1", CriterionResult.PASS)],
        security_findings=[
            Finding(
                id="finding-1",
                severity=FindingSeverity.BLOCKER,
                description="Command injection in test runner",
            )
        ],
    )
    with pytest.raises(InvariantViolation):
        assert_valid_pass(contract, result)


def test_pass_rejected_when_mandatory_criterion_waived_without_approval():
    with pytest.raises(ValidationError):
        # waiver without an approval ref is rejected at the model level
        make_criterion_verification(
            "crit-1", CriterionResult.WAIVED, waiver_approval_ref=None
        )


def test_pass_valid_when_mandatory_criterion_waived_with_approval():
    contract = make_task_contract(
        acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)]
    )
    result = make_verification_result(
        decision=VerificationDecision.PASS,
        criteria=[
            make_criterion_verification(
                "crit-1",
                CriterionResult.WAIVED,
                waiver_approval_ref="approval-1",
            )
        ],
    )
    assert_valid_pass(contract, result)  # must not raise


def test_non_pass_decision_is_not_checked_against_pass_invariants():
    contract = make_task_contract(
        acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)]
    )
    result = make_verification_result(
        decision=VerificationDecision.REWORK,
        criteria=[
            make_criterion_verification("crit-1", CriterionResult.NOT_VERIFIED)
        ],
        required_fixes=["fix the thing"],
    )
    assert_valid_pass(contract, result)  # no-op for non-PASS decisions


# ---------------------------------------------------------------------------
# 9. ReworkContract scope expansion rejection
# ---------------------------------------------------------------------------


def test_rework_scope_subset_of_parent_is_accepted():
    parent = make_task_contract(
        scope=make_scope(allowed_path_rules=["src/**", "tests/**"])
    )
    rework = make_rework_contract(
        effective_scope=make_scope(allowed_path_rules=["src/**"])
    )
    assert_rework_scope_is_subset(parent, rework.effective_scope)


def test_rework_scope_expansion_is_rejected():
    parent = make_task_contract(scope=make_scope(allowed_path_rules=["src/**"]))
    rework = make_rework_contract(
        effective_scope=make_scope(allowed_path_rules=["src/**", "infra/**"])
    )
    with pytest.raises(InvariantViolation):
        assert_rework_scope_is_subset(parent, rework.effective_scope)


def test_rework_scope_cannot_narrow_forbidden_rules():
    parent = make_task_contract(
        scope=make_scope(
            allowed_path_rules=["src/**"], forbidden_path_rules=[".git/**", "secrets/**"]
        )
    )
    narrower_forbid = make_scope(
        allowed_path_rules=["src/**"], forbidden_path_rules=[".git/**"]
    )
    assert is_scope_subset(narrower_forbid, parent.scope) is False


def test_rework_scope_cannot_raise_max_changed_files_ceiling():
    parent_scope = make_scope(allowed_path_rules=["src/**"], max_changed_files=10)
    child_scope = make_scope(allowed_path_rules=["src/**"], max_changed_files=50)
    assert is_scope_subset(child_scope, parent_scope) is False


# ---------------------------------------------------------------------------
# 10. Artifact/Evidence digest and provenance validation
# ---------------------------------------------------------------------------


def test_evidence_record_requires_valid_digest_format():
    with pytest.raises(ValidationError):
        make_evidence_record(content_digest="not-a-digest")


def test_evidence_record_host_observed_cannot_be_producer_worker():
    with pytest.raises(ValidationError):
        make_evidence_record(
            provenance=EvidenceProvenance(
                producer_type=ActorType.WORKER,
                producer_id="claude-worker",
                collection_method="self_report",
                trust_tier=EvidenceTrustTier.HOST_OBSERVED,
            )
        )


def test_evidence_record_provider_reported_may_be_produced_by_worker():
    evidence = make_evidence_record(
        provenance=EvidenceProvenance(
            producer_type=ActorType.WORKER,
            producer_id="claude-worker",
            collection_method="self_report",
            trust_tier=EvidenceTrustTier.PROVIDER_REPORTED,
        )
    )
    assert evidence.provenance.trust_tier is EvidenceTrustTier.PROVIDER_REPORTED


def test_evidence_record_requires_at_least_one_artifact_ref():
    with pytest.raises(ValidationError):
        make_evidence_record(artifact_refs=[])
