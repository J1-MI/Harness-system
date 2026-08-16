"""Tests for the rework service (Phase 10): ReworkContract construction,
scope-subset enforcement, attempt budget, no-progress breaker."""

from __future__ import annotations

import pytest

from agent_harness.application.rework import (
    ReworkExhaustedError,
    build_rework_contract,
    check_rework_budget,
    detect_no_progress,
    failed_criteria_ids_of,
)
from agent_harness.application.verification import VerifiedVerification
from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import CriterionResult, FindingSeverity, VerificationDecision, VerificationMethod
from agent_harness.domain.models import BudgetRequest, Finding, ScopeRules
from agent_harness.domain.validation import InvariantViolation
from tests.factories import (
    make_acceptance_criterion,
    make_criterion_verification,
    make_task_contract,
    make_verification_result,
    make_verification_spec,
)

VALID_DIGEST = "sha256:" + "0" * 64


def make_verified(**overrides) -> VerifiedVerification:
    data = dict(
        verification_result=make_verification_result(
            decision=VerificationDecision.REWORK,
            criteria=[make_criterion_verification(result=CriterionResult.FAIL)],
            required_fixes=["fix the failing assertion"],
        ),
        pass_invariant_violations=[],
        accepted_decision=VerificationDecision.REWORK,
    )
    data.update(overrides)
    return VerifiedVerification(**data)


# ---------------------------------------------------------------------------
# failed_criteria_ids_of / detect_no_progress
# ---------------------------------------------------------------------------


def test_failed_criteria_ids_excludes_pass_and_waived():
    result = make_verification_result(
        decision=VerificationDecision.REWORK,
        criteria=[
            make_criterion_verification("crit-pass", result=CriterionResult.PASS),
            make_criterion_verification(
                "crit-waived", result=CriterionResult.WAIVED, waiver_approval_ref="approval-1"
            ),
            make_criterion_verification("crit-fail", result=CriterionResult.FAIL),
            make_criterion_verification("crit-nv", result=CriterionResult.NOT_VERIFIED),
        ],
        required_fixes=["fix it"],
    )
    assert failed_criteria_ids_of(result) == frozenset({"crit-fail", "crit-nv"})


def test_detect_no_progress_true_only_when_identical_and_nonempty():
    assert detect_no_progress(None, frozenset({"crit-1"})) is False
    assert detect_no_progress(frozenset(), frozenset()) is False
    assert detect_no_progress(frozenset({"crit-1"}), frozenset({"crit-1"})) is True
    assert detect_no_progress(frozenset({"crit-1"}), frozenset({"crit-2"})) is False


# ---------------------------------------------------------------------------
# check_rework_budget
# ---------------------------------------------------------------------------


def test_check_rework_budget_allows_up_to_ceiling():
    ceiling = BudgetRequest(timeout_seconds=60, max_turns=5, max_rework_iterations=2)
    check_rework_budget(1, ceiling)
    check_rework_budget(2, ceiling)


def test_check_rework_budget_rejects_beyond_ceiling():
    ceiling = BudgetRequest(timeout_seconds=60, max_turns=5, max_rework_iterations=2)
    with pytest.raises(ReworkExhaustedError):
        check_rework_budget(3, ceiling)


# ---------------------------------------------------------------------------
# build_rework_contract
# ---------------------------------------------------------------------------


def test_build_rework_contract_binds_parent_and_fixes():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    verified = make_verified(
        verification_result=make_verification_result(
            decision=VerificationDecision.REWORK,
            contract_digest=contract.integrity.canonical_digest,
            criteria=[make_criterion_verification("crit-1", result=CriterionResult.FAIL)],
            required_fixes=["fix the assertion", "add a missing test"],
            prohibited_changes=["do not touch migrations/"],
        )
    )

    rework = build_rework_contract(contract, verified, attempt_number=1)

    assert rework.task_id == contract.task_id
    assert rework.parent_contract_digest == contract.integrity.canonical_digest
    assert rework.attempt_number == 1
    assert rework.failed_criteria_ids == ["crit-1"]
    assert [fix.description for fix in rework.required_fixes] == [
        "fix the assertion",
        "add a missing test",
    ]
    assert rework.prohibited_changes == ["do not touch migrations/"]
    assert rework.effective_scope == contract.scope


def test_build_rework_contract_inherits_verification_method_of_failed_criterion():
    manual_criterion = make_acceptance_criterion(
        "crit-manual",
        mandatory=True,
        verification=make_verification_spec(
            method=VerificationMethod.MANUAL, command_id=None, manual_instruction="check it by hand"
        ),
    )
    contract = make_task_contract(acceptance_criteria=[manual_criterion])
    verified = make_verified(
        verification_result=make_verification_result(
            decision=VerificationDecision.REWORK,
            contract_digest=contract.integrity.canonical_digest,
            criteria=[make_criterion_verification("crit-manual", result=CriterionResult.FAIL)],
            required_fixes=["fix it"],
        )
    )
    rework = build_rework_contract(contract, verified, attempt_number=1)
    assert rework.required_fixes[0].verification_method is VerificationMethod.MANUAL


def test_build_rework_contract_rejects_scope_widening():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1")])
    verified = make_verified(
        verification_result=make_verification_result(
            decision=VerificationDecision.REWORK,
            contract_digest=contract.integrity.canonical_digest,
            criteria=[make_criterion_verification("crit-1", result=CriterionResult.FAIL)],
            required_fixes=["fix it"],
        )
    )
    widened_scope = ScopeRules(
        allowed_path_rules=["src/**", "tests/**", "infra/**"],  # wider than contract.scope
        forbidden_path_rules=[],  # narrower forbid set than contract.scope's [".git/**"]
        allow_new_files=True,
        max_changed_files=None,
        max_changed_bytes=None,
        declared_generated_paths=[],
    )

    with pytest.raises(InvariantViolation):
        build_rework_contract(contract, verified, attempt_number=1, effective_scope=widened_scope)


def test_build_rework_contract_accepts_a_narrower_explicit_scope():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1")])
    verified = make_verified(
        verification_result=make_verification_result(
            decision=VerificationDecision.REWORK,
            contract_digest=contract.integrity.canonical_digest,
            criteria=[make_criterion_verification("crit-1", result=CriterionResult.FAIL)],
            required_fixes=["fix it"],
        )
    )
    narrower_scope = ScopeRules(
        allowed_path_rules=["src/**"],  # narrower than contract.scope's ["src/**", "tests/**"]
        forbidden_path_rules=[".git/**", "infra/**"],
        allow_new_files=False,
        max_changed_files=5,
        max_changed_bytes=50_000,
        declared_generated_paths=[],
    )
    rework = build_rework_contract(contract, verified, attempt_number=1, effective_scope=narrower_scope)
    assert rework.effective_scope == narrower_scope
