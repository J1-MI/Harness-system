"""Tests for the Verifier orchestration service (Phase 8).

Reuses the Codex fakes from ``test_codex_adapter.py`` (same package) so the
Verifier runs through the real ``CodexPlannerAdapter`` machinery, just with
role=VERIFIER and a fake transport — no network, no API key.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from agent_harness.application.verification import (
    VerifiedVerification,
    build_verifier_prompt,
    find_missing_evidence_violations,
    run_verification,
)
from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import (
    ActorType,
    ArtifactMediaKind,
    EvidenceTrustTier,
    RedactionStatus,
    VerificationDecision,
)
from agent_harness.domain.models import Artifact, CommandRun, EvidenceProvenance, EvidenceRecord
from agent_harness.execution.command_broker import CommandExecution
from agent_harness.execution.evidence import FrozenValidationResult
from agent_harness.execution.process import ProcessResult
from agent_harness.execution.validation import ManifestDiff
from agent_harness.providers.codex import CodexPlannerAdapter
from tests.factories import make_acceptance_criterion, make_task_contract, make_worker_result
from tests.unit.test_codex_adapter import FakeCodexClient, make_agent_message_item, turn_completed

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_artifact(**overrides) -> Artifact:
    data = dict(
        media_type="application/json",
        media_kind=ArtifactMediaKind.JSON,
        size_bytes=10,
        content_digest=VALID_DIGEST,
        storage_uri="blob:sha256:" + "0" * 64,
        redaction_status=RedactionStatus.NONE,
        created_at=_utc_now(),
    )
    data.update(overrides)
    return Artifact(**data)


def make_evidence(evidence_id: str | None = None, **overrides) -> EvidenceRecord:
    data = dict(
        evidence_id=evidence_id or new_id(),
        run_id=new_id(),
        task_id=new_id(),
        subject_type="COMMAND_RUN",
        subject_id=new_id(),
        subject_digest=VALID_DIGEST,
        kind="command_exit_code",
        provenance=EvidenceProvenance(
            producer_type=ActorType.HOST_TEST_RUNNER,
            producer_id="host-runner-1",
            collection_method="direct_process_observation",
            trust_tier=EvidenceTrustTier.HOST_OBSERVED,
        ),
        artifact_refs=["artifact-1"],
        media_type="application/json",
        content_digest=VALID_DIGEST,
        size_bytes=10,
        created_at=_utc_now(),
    )
    data.update(overrides)
    return EvidenceRecord(**data)


def make_successful_check_execution(command_id: str = "pytest", exit_code: int = 0) -> CommandExecution:
    """A CommandExecution that would satisfy a COMMAND-verified criterion's
    deterministic check_execution gate (Codex review B-02)."""

    now = _utc_now()
    return CommandExecution(
        command_run=CommandRun(
            command_spec_id=command_id, resolved_argv=[command_id], exit_code=exit_code,
            duration_ms=10, started_at=now, completed_at=now,
        ),
        process_result=ProcessResult(
            argv=[command_id], exit_code=exit_code, timed_out=False, output_cap_exceeded=False,
            duration_ms=10, stdout=b"", stderr=b"", stdout_truncated=False, stderr_truncated=False,
            started_at=now, completed_at=now,
        ),
    )


def make_frozen_result(
    evidence: list[EvidenceRecord],
    *,
    scope_violations: list[str] | None = None,
    check_executions: list[CommandExecution] | None = None,
) -> FrozenValidationResult:
    return FrozenValidationResult(
        baseline_manifest=[],
        result_manifest=[],
        baseline_artifact=make_artifact(),
        result_artifact=make_artifact(),
        manifest_diff=ManifestDiff(),
        test_mutations=[],
        check_executions=(
            check_executions if check_executions is not None else [make_successful_check_execution()]
        ),
        evidence=evidence,
        scope_violations=scope_violations or [],
    )


def make_verifier_turn_batch(verification_json: dict):
    turn = make_agent_message_item(json.dumps(verification_json))
    from openai_codex.generated.v2_all import Turn, TurnStatus

    full_turn = Turn(id="turn-verify", status=TurnStatus.completed, items=[turn])
    return [turn_completed(full_turn)]


def make_provider_factory(verification_json: dict):
    fake_client = FakeCodexClient([make_verifier_turn_batch(verification_json)])

    def factory(resolve_prompt):
        return CodexPlannerAdapter(resolve_prompt=resolve_prompt, client_factory=lambda: fake_client)

    return factory, fake_client


def make_verification_json(*, contract_digest: str, decision: str, criteria: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "verification_id": new_id(),
        "task_id": new_id(),
        "contract_digest": contract_digest,
        "result_snapshot_digest": VALID_DIGEST,
        "evidence_set_digest": VALID_DIGEST,
        "invocation_id": new_id(),
        "decision": decision,
        "criteria": criteria,
        "scope_violations": [],
        "security_findings": [],
        "quality_findings": [],
        "required_fixes": [] if decision != "REWORK" else ["fix it"],
        "prohibited_changes": [],
        "remaining_risks": [] if decision != "REJECT" else ["too risky"],
    }


# ---------------------------------------------------------------------------
# Bias exclusion: Worker's self-report is never blended into evidence
# ---------------------------------------------------------------------------


def test_prompt_omits_worker_result_by_default():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1")])
    frozen_result = make_frozen_result(evidence=[])
    prompt = build_verifier_prompt(contract, frozen_result)
    assert "UNTRUSTED WORKER CLAIMS" not in prompt


def test_prompt_fences_worker_claims_as_untrusted_when_opted_in():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1")])
    frozen_result = make_frozen_result(evidence=[])
    worker_result = make_worker_result(implementation_summary="I definitely fixed everything perfectly")

    prompt_with = build_verifier_prompt(contract, frozen_result, untrusted_worker_claims=worker_result)
    prompt_without = build_verifier_prompt(contract, frozen_result)

    assert "I definitely fixed everything perfectly" not in prompt_without
    assert "UNTRUSTED WORKER CLAIMS" in prompt_with
    assert "I definitely fixed everything perfectly" in prompt_with
    # It must be clearly fenced, not mixed into the EVIDENCE RECORDS section.
    untrusted_index = prompt_with.index("UNTRUSTED WORKER CLAIMS")
    evidence_index = prompt_with.index("EVIDENCE RECORDS")
    assert evidence_index < untrusted_index


# ---------------------------------------------------------------------------
# Fresh session: run_verification always starts a brand-new provider+session
# ---------------------------------------------------------------------------


def test_run_verification_always_starts_a_fresh_session():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    evidence = make_evidence()
    frozen_result = make_frozen_result(evidence=[evidence])
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": [evidence.evidence_id]}],
    )
    factory, fake_client = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert isinstance(result, VerifiedVerification)
    # A fresh thread_start was used (never thread_resume).
    assert fake_client.started_kwargs is not None
    assert result.accepted_decision is VerificationDecision.PASS
    assert result.pass_invariant_violations == []


# ---------------------------------------------------------------------------
# Missing evidence PASS rejection
# ---------------------------------------------------------------------------


def test_pass_with_no_evidence_refs_is_downgraded_to_manual_review():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    frozen_result = make_frozen_result(evidence=[])  # no evidence collected at all
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": []}],  # model hallucinated a PASS
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.verification_result.decision is VerificationDecision.PASS  # what the model claimed
    assert result.accepted_decision is VerificationDecision.MANUAL_REVIEW  # what we actually accept
    assert any("no evidence" in v for v in result.pass_invariant_violations)


def test_pass_with_fabricated_evidence_ref_is_downgraded():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    real_evidence = make_evidence()
    frozen_result = make_frozen_result(evidence=[real_evidence])
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[
            {"id": "crit-1", "result": "PASS", "evidence_refs": ["evidence-that-does-not-exist"]}
        ],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.accepted_decision is VerificationDecision.MANUAL_REVIEW


def test_pass_with_real_evidence_ref_is_accepted():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    evidence = make_evidence()
    frozen_result = make_frozen_result(evidence=[evidence])
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": [evidence.evidence_id]}],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.accepted_decision is VerificationDecision.PASS
    assert result.pass_invariant_violations == []


def test_pass_with_deterministic_scope_violation_is_downgraded():
    """Codex review M-01: a PASS claim must not survive a nonempty
    frozen_result.scope_violations, even with real cited evidence and no
    other invariant violation — this is the deterministic Harness-side
    check, independent of whatever the model itself reports."""

    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    evidence = make_evidence()
    frozen_result = make_frozen_result(
        evidence=[evidence], scope_violations=["'secrets/prod.key' does not match any allowed_path_rules pattern"]
    )
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": [evidence.evidence_id]}],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.verification_result.decision is VerificationDecision.PASS  # what the model claimed
    assert result.accepted_decision is VerificationDecision.MANUAL_REVIEW  # what we actually accept
    assert any("secrets/prod.key" in v for v in result.pass_invariant_violations)


def test_pass_citing_a_failed_commands_evidence_is_downgraded():
    """Codex review B-02: a command that actually exited nonzero must
    reject a PASS deterministically, even if the Verifier cites real
    evidence produced by that same failed command."""

    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    evidence = make_evidence()
    frozen_result = make_frozen_result(
        evidence=[evidence], check_executions=[make_successful_check_execution("pytest", exit_code=1)]
    )
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": [evidence.evidence_id]}],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.accepted_decision is VerificationDecision.MANUAL_REVIEW
    assert any("exited 1" in v for v in result.pass_invariant_violations)


def test_pass_with_missing_check_execution_is_downgraded():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    evidence = make_evidence()
    frozen_result = make_frozen_result(evidence=[evidence], check_executions=[])
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="PASS",
        criteria=[{"id": "crit-1", "result": "PASS", "evidence_refs": [evidence.evidence_id]}],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.accepted_decision is VerificationDecision.MANUAL_REVIEW
    assert any("never executed" in v for v in result.pass_invariant_violations)


def test_find_missing_evidence_violations_ignores_non_pass_criteria():
    from agent_harness.domain.models import CriterionVerification
    from agent_harness.domain.enums import CriterionResult

    frozen_result = make_frozen_result(evidence=[])
    verification_result_criteria = [
        CriterionVerification(id="crit-1", result=CriterionResult.NOT_VERIFIED, evidence_refs=[])
    ]
    from tests.factories import make_verification_result

    verification_result = make_verification_result(
        decision=VerificationDecision.REWORK,
        criteria=verification_result_criteria,
        required_fixes=["fix it"],
    )
    assert find_missing_evidence_violations(verification_result, frozen_result) == []


# ---------------------------------------------------------------------------
# Non-PASS decisions pass through unchanged
# ---------------------------------------------------------------------------


def test_rework_decision_is_not_second_guessed():
    contract = make_task_contract(acceptance_criteria=[make_acceptance_criterion("crit-1", mandatory=True)])
    frozen_result = make_frozen_result(evidence=[])
    verification_json = make_verification_json(
        contract_digest=contract.integrity.canonical_digest,
        decision="REWORK",
        criteria=[{"id": "crit-1", "result": "FAIL", "evidence_refs": []}],
    )
    factory, _ = make_provider_factory(verification_json)

    async def scenario():
        return await run_verification(
            factory,
            contract,
            frozen_result,
            role_profile_ref="verifier-profile",
            role_profile_digest=VALID_DIGEST,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=VALID_DIGEST,
            deadline=_utc_now(),
            correlation_id="corr-1",
        )

    result = asyncio.run(scenario())
    assert result.accepted_decision is VerificationDecision.REWORK
