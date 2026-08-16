"""Cross-model semantic validators.

Field-level rules (extra fields, digest format, path format, immutability)
live directly on the Pydantic models in ``domain/models.py``. This module
holds validators that need *two or more* models at once — the kind of
check a single model's ``model_validator`` cannot express.
"""

from __future__ import annotations

from agent_harness.domain.enums import CriterionResult, FindingSeverity, VerificationDecision
from agent_harness.domain.models import (
    Approval,
    Artifact,
    EvidenceRecord,
    ScopeRules,
    TaskContract,
    VerificationResult,
)


class InvariantViolation(ValueError):
    """Raised when a cross-model semantic invariant does not hold."""


# ---------------------------------------------------------------------------
# Approval binding
# ---------------------------------------------------------------------------


def validate_approval_binding(
    approval: Approval,
    *,
    expected_subject_digest: str,
    expected_policy_decision_digest: str,
) -> None:
    """Reject an approval that is not bound to the exact subject/policy pair.

    Per the architecture review, an approval is invalid the moment the
    contract, base revision, allowed paths, or network domains it was
    granted against change — enforced here by requiring an exact digest
    match rather than a fuzzy comparison.
    """

    violations: list[str] = []
    if approval.subject_digest != expected_subject_digest:
        violations.append(
            f"subject_digest mismatch: approval={approval.subject_digest!r} "
            f"expected={expected_subject_digest!r}"
        )
    if approval.policy_decision_digest != expected_policy_decision_digest:
        violations.append(
            "policy_decision_digest mismatch: "
            f"approval={approval.policy_decision_digest!r} "
            f"expected={expected_policy_decision_digest!r}"
        )
    if violations:
        raise InvariantViolation("; ".join(violations))


# ---------------------------------------------------------------------------
# VerificationResult PASS invariants
# ---------------------------------------------------------------------------


def find_pass_invariant_violations(
    contract: TaskContract, result: VerificationResult
) -> list[str]:
    """Return reasons ``result`` is not a valid PASS for ``contract``.

    An empty list means the PASS is valid. Callers should treat any
    non-empty list as a hard rejection regardless of what
    ``result.decision`` claims — the whole point is that the Codex
    verifier's PASS is a recommendation, not the official disposition.
    """

    violations: list[str] = []

    criteria_by_id = {c.id: c for c in result.criteria}
    mandatory_ids = {c.id for c in contract.acceptance_criteria if c.mandatory}

    for criterion_id in mandatory_ids:
        verification = criteria_by_id.get(criterion_id)
        if verification is None:
            violations.append(
                f"mandatory criterion {criterion_id!r} has no verification result"
            )
            continue
        if verification.result is CriterionResult.WAIVED:
            if not verification.waiver_approval_ref:
                violations.append(
                    f"mandatory criterion {criterion_id!r} is WAIVED without "
                    "a waiver_approval_ref"
                )
        elif verification.result is not CriterionResult.PASS:
            violations.append(
                f"mandatory criterion {criterion_id!r} is {verification.result} "
                "not PASS/WAIVED"
            )

    for verification in result.criteria:
        if verification.result is CriterionResult.NOT_VERIFIED:
            violations.append(
                f"criterion {verification.id!r} is NOT_VERIFIED"
            )

    unresolved_severities = {FindingSeverity.BLOCKER, FindingSeverity.HIGH}
    for finding in result.security_findings:
        if finding.severity in unresolved_severities:
            violations.append(
                f"unresolved {finding.severity} security finding {finding.id!r}"
            )

    if result.scope_violations:
        violations.append(
            f"{len(result.scope_violations)} unresolved scope violation(s)"
        )

    return violations


def assert_valid_pass(contract: TaskContract, result: VerificationResult) -> None:
    """Raise ``InvariantViolation`` if ``result`` claims PASS but should not.

    No-op for any decision other than PASS — this function only guards the
    PASS invariant, not REWORK/REJECT/MANUAL_REVIEW semantics.
    """

    if result.decision is not VerificationDecision.PASS:
        return
    violations = find_pass_invariant_violations(contract, result)
    if violations:
        raise InvariantViolation(
            f"VerificationResult {result.verification_id} claims PASS but "
            f"violates invariants: {'; '.join(violations)}"
        )


# ---------------------------------------------------------------------------
# ReworkContract scope containment
# ---------------------------------------------------------------------------


def is_scope_subset(child: ScopeRules, parent: ScopeRules) -> bool:
    """True if ``child`` cannot reach anything ``parent`` would deny.

    A rework's effective scope may only narrow the original contract's
    scope: every path it allows must already be allowed by the parent, and
    it must forbid at least everything the parent forbids. Numeric
    ceilings (max changed files/bytes) may only go down, never up.
    """

    if not set(child.allowed_path_rules).issubset(set(parent.allowed_path_rules)):
        return False
    if not set(parent.forbidden_path_rules).issubset(set(child.forbidden_path_rules)):
        return False
    if child.allow_new_files and not parent.allow_new_files:
        return False
    if not _ceiling_not_raised(child.max_changed_files, parent.max_changed_files):
        return False
    if not _ceiling_not_raised(child.max_changed_bytes, parent.max_changed_bytes):
        return False
    return True


def _ceiling_not_raised(child_value: int | None, parent_value: int | None) -> bool:
    """``None`` means "no ceiling". A child may only tighten, never loosen."""

    if parent_value is None:
        return True
    if child_value is None:
        return False
    return child_value <= parent_value


def assert_rework_scope_is_subset(
    parent_contract: TaskContract, effective_scope: ScopeRules
) -> None:
    if not is_scope_subset(effective_scope, parent_contract.scope):
        raise InvariantViolation(
            "ReworkContract.effective_scope is not a subset of the parent "
            "TaskContract.scope"
        )


# ---------------------------------------------------------------------------
# Evidence / Artifact integrity (Phase 2.2)
# ---------------------------------------------------------------------------


def assert_evidence_matches_artifact(evidence: EvidenceRecord, artifact: Artifact) -> None:
    """Reject an ``EvidenceRecord`` whose claimed content does not match the
    ``Artifact`` it is supposed to be backed by.

    Catches two distinct failure modes: the evidence pointing at a digest
    that does not match the artifact actually on disk, and an evidence
    record whose ``artifact_refs`` never even names the artifact.
    """

    violations: list[str] = []
    if evidence.content_digest != artifact.content_digest:
        violations.append(
            f"content_digest mismatch: evidence={evidence.content_digest!r} "
            f"artifact={artifact.content_digest!r}"
        )
    if artifact.artifact_id not in evidence.artifact_refs:
        violations.append(
            f"artifact {artifact.artifact_id!r} is not referenced by "
            f"evidence.artifact_refs={evidence.artifact_refs!r}"
        )
    if violations:
        raise InvariantViolation("; ".join(violations))
