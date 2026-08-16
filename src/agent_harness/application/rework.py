"""Rework Lifecycle (Phase 10): turn a REWORK verdict into a bound ReworkContract.

Architecture review finding H-09: "재작업은 단순히 IMPLEMENTING으로 되돌아간다.
이전 계약의 실패 항목, 허용 경로, 금지 변경이 유실된다" — a rework attempt must
never be "just retry the same prompt". This module is what the roadmap calls
the "rework service": it turns Codex Verifier's free-text
``VerificationResult.required_fixes``/``prohibited_changes`` into a
structured, scope-bound ``ReworkContract`` that the Harness itself
constructs and validates — the domain model table's "Harness가 Codex 제안을
검증해 생성" applies here exactly as it does for ``TaskContract`` in
``orchestrator.py``'s CONTRACT_VALIDATING step.

Three review-mandated guards live here, matching roadmap row 10's test
criteria exactly:

- **scope subset** — ``build_rework_contract`` always calls Phase 1.1's
  ``assert_rework_scope_is_subset`` before returning; a ReworkContract that
  would widen scope beyond the parent TaskContract's simply cannot be built.
- **attempt budget** — ``check_rework_budget`` enforces
  ``BudgetRequest.max_rework_iterations``, matching "max rework 초과 시 생성
  금지".
- **no-progress breaker** — ``detect_no_progress`` catches "동일 수정 반복
  금지": if two consecutive rework attempts fail the exact same set of
  acceptance criteria, the loop is not making progress and must stop
  rather than spin forever burning budget (security control 11 in the
  attack-surface table: "재작업 비용 폭주").
"""

from __future__ import annotations

from agent_harness.domain.digests import compute_model_digest
from agent_harness.domain.enums import CriterionResult, VerificationDecision, VerificationMethod
from agent_harness.domain.models import (
    BudgetRequest,
    RequestedCapabilities,
    RequiredFix,
    ReworkContract,
    ScopeRules,
    TaskContract,
    VerificationResult,
)
from agent_harness.domain.validation import assert_rework_scope_is_subset
from agent_harness.application.verification import VerifiedVerification

__all__ = [
    "ReworkExhaustedError",
    "failed_criteria_ids_of",
    "detect_no_progress",
    "check_rework_budget",
    "build_rework_contract",
]

_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


class ReworkExhaustedError(RuntimeError):
    """Raised when another rework attempt would exceed the budget, or when
    consecutive attempts show no progress (same failing criteria twice)."""


def failed_criteria_ids_of(result: VerificationResult) -> frozenset[str]:
    """Every criterion the model did not mark PASS or WAIVED.

    Used both to build a ``ReworkContract.failed_criteria_ids`` and, by the
    caller, to compare across attempts for the no-progress breaker.
    """

    return frozenset(
        c.id for c in result.criteria if c.result not in (CriterionResult.PASS, CriterionResult.WAIVED)
    )


def detect_no_progress(
    previous_failed_ids: frozenset[str] | None, current_failed_ids: frozenset[str]
) -> bool:
    """True if this attempt failed exactly the same criteria as the last one.

    ``previous_failed_ids is None`` means this is the first attempt — there
    is nothing to compare against yet, so it can never be "no progress".
    An empty ``current_failed_ids`` is never "no progress" either (that
    would mean nothing is failing, which is not a REWORK situation).
    """

    if previous_failed_ids is None:
        return False
    return bool(current_failed_ids) and current_failed_ids == previous_failed_ids


def check_rework_budget(attempt_number: int, ceiling: BudgetRequest) -> None:
    """Raise if creating a rework attempt numbered ``attempt_number`` would
    exceed ``ceiling.max_rework_iterations``."""

    if attempt_number > ceiling.max_rework_iterations:
        raise ReworkExhaustedError(
            f"attempt_number={attempt_number} exceeds "
            f"max_rework_iterations={ceiling.max_rework_iterations}"
        )


def build_rework_contract(
    contract: TaskContract,
    verified: VerifiedVerification,
    *,
    attempt_number: int,
    effective_scope: ScopeRules | None = None,
    requested_additional_capabilities: RequestedCapabilities | None = None,
) -> ReworkContract:
    """Deterministically construct the accepted ``ReworkContract`` for one
    REWORK verdict.

    ``effective_scope`` defaults to the parent contract's own scope
    (trivially a subset of itself, satisfying "기본 effective_scope는 원
    Task Contract scope의 부분집합") — a caller may pass a narrower scope
    explicitly (e.g. restricting rework to only the paths implicated by the
    failed criteria), but never a wider one: ``assert_rework_scope_is_subset``
    below rejects that unconditionally, regardless of caller intent.
    """

    result = verified.verification_result
    if result.decision is not VerificationDecision.REWORK:
        raise ValueError(f"build_rework_contract requires a REWORK decision, got {result.decision!r}")

    failed_ids = sorted(failed_criteria_ids_of(result))

    verification_method = VerificationMethod.COMMAND
    for criterion in contract.acceptance_criteria:
        if criterion.id in failed_ids:
            verification_method = criterion.verification.method
            break

    required_fixes = [
        RequiredFix(
            id=f"fix-{index + 1}",
            description=description,
            allowed_path_rules=list(contract.scope.allowed_path_rules),
            verification_method=verification_method,
        )
        for index, description in enumerate(result.required_fixes)
    ]

    finding_ids = [finding.id for finding in (*result.security_findings, *result.quality_findings)]

    draft = ReworkContract(
        task_id=contract.task_id,
        parent_contract_digest=contract.integrity.canonical_digest,
        parent_verification_id=result.verification_id,
        parent_result_snapshot_digest=result.result_snapshot_digest,
        attempt_number=attempt_number,
        failed_criteria_ids=failed_ids,
        finding_ids=finding_ids,
        required_fixes=required_fixes,
        prohibited_changes=result.prohibited_changes,
        effective_scope=effective_scope if effective_scope is not None else contract.scope,
        requested_additional_capabilities=requested_additional_capabilities,
        budget=contract.budget_request,
        integrity_digest=_PLACEHOLDER_DIGEST,
    )
    real_digest = compute_model_digest(draft, exclude_fields={"integrity_digest"})
    accepted = draft.model_copy(update={"integrity_digest": real_digest})

    # Never trust the default (or any future non-default) effective_scope
    # without re-checking it — this call is what H-09/the review's rework
    # guard actually depends on, not the field default above.
    assert_rework_scope_is_subset(contract, accepted.effective_scope)
    return accepted
