"""Shared builders for valid domain model instances used across tests.

Each ``make_*`` function returns a fully valid instance with sensible
defaults; pass keyword overrides to vary one field at a time. Kept
separate from ``conftest.py`` fixtures because most tests need several
independently-overridable nested objects, not a single fixed fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import (
    ActorType,
    AgentRole,
    CriterionResult,
    EvidenceTrustTier,
    FindingSeverity,
    LifecycleState,
    PolicyOutcome,
    SubjectType,
    VerificationDecision,
    VerificationMethod,
    WorkerResultStatus,
    WorkspaceAccessLevel,
)
from agent_harness.domain.models import (
    AcceptanceCriterion,
    Approval,
    BudgetRequest,
    ConstraintSet,
    CriterionVerification,
    EvidenceProvenance,
    EvidenceRecord,
    IntegrityRef,
    PolicyDecision,
    PolicyGrants,
    RepositoryRef,
    RequestedCapabilities,
    RequiredFix,
    ReworkContract,
    Run,
    ScopeRules,
    TaskContract,
    VerificationResult,
    VerificationSpec,
    WorkerResult,
)

VALID_SHA = "a" * 40
VALID_DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def in_future(seconds: int = 3600) -> datetime:
    return utc_now() + timedelta(seconds=seconds)


def make_repository_ref(**overrides) -> RepositoryRef:
    data = dict(
        repository_id="repo-1",
        base_commit_sha=VALID_SHA,
        target_ref="refs/heads/main",
        expected_repository_fingerprint=VALID_DIGEST,
    )
    data.update(overrides)
    return RepositoryRef(**data)


def make_scope(**overrides) -> ScopeRules:
    data = dict(
        allowed_path_rules=["src/**", "tests/**"],
        forbidden_path_rules=[".git/**"],
        allow_new_files=True,
        max_changed_files=20,
        max_changed_bytes=200_000,
        declared_generated_paths=[],
    )
    data.update(overrides)
    return ScopeRules(**data)


def make_verification_spec(**overrides) -> VerificationSpec:
    data = dict(
        method=VerificationMethod.COMMAND,
        command_id="pytest",
        expected_exit_codes=[0],
        required_evidence_kinds=["test_run"],
    )
    data.update(overrides)
    return VerificationSpec(**data)


def make_acceptance_criterion(
    criterion_id: str = "crit-1", mandatory: bool = True, **overrides
) -> AcceptanceCriterion:
    data = dict(
        id=criterion_id,
        description="tests pass",
        mandatory=mandatory,
        verification=make_verification_spec(),
    )
    data.update(overrides)
    return AcceptanceCriterion(**data)


def make_requested_capabilities(**overrides) -> RequestedCapabilities:
    data = dict(
        workspace_access=WorkspaceAccessLevel.READ_WRITE,
        command_ids=["pytest"],
        raw_shell=False,
        network_domains=[],
        package_install=False,
        external_systems=[],
        mcp_tools=[],
        database_targets=[],
    )
    data.update(overrides)
    return RequestedCapabilities(**data)


def make_task_contract(**overrides) -> TaskContract:
    data = dict(
        contract_revision=1,
        run_id=new_id(),
        task_id=new_id(),
        request_snapshot_ref="artifact://request-1",
        objective="Implement the thing",
        repository=make_repository_ref(),
        scope=make_scope(),
        constraints=ConstraintSet(),
        acceptance_criteria=[make_acceptance_criterion()],
        requested_capabilities=make_requested_capabilities(),
        budget_request=BudgetRequest(
            timeout_seconds=600, max_turns=10, max_rework_iterations=2
        ),
        integrity=IntegrityRef(canonical_digest=VALID_DIGEST),
    )
    data.update(overrides)
    return TaskContract(**data)


def make_worker_result(**overrides) -> WorkerResult:
    data = dict(
        task_id=new_id(),
        contract_id=new_id(),
        contract_digest=VALID_DIGEST,
        invocation_id=new_id(),
        provider_session_ref="fake-session-1",
        status=WorkerResultStatus.COMPLETED,
        implementation_summary="Implemented the thing.",
    )
    data.update(overrides)
    return WorkerResult(**data)


def make_criterion_verification(
    criterion_id: str = "crit-1", result: CriterionResult = CriterionResult.PASS, **overrides
) -> CriterionVerification:
    data = dict(id=criterion_id, result=result, evidence_refs=["evidence-1"])
    data.update(overrides)
    return CriterionVerification(**data)


def make_verification_result(**overrides) -> VerificationResult:
    data = dict(
        task_id=new_id(),
        contract_digest=VALID_DIGEST,
        result_snapshot_digest=VALID_DIGEST,
        evidence_set_digest=VALID_DIGEST,
        invocation_id=new_id(),
        decision=VerificationDecision.PASS,
        criteria=[make_criterion_verification()],
    )
    data.update(overrides)
    return VerificationResult(**data)


def make_rework_contract(**overrides) -> ReworkContract:
    data = dict(
        task_id=new_id(),
        parent_contract_digest=VALID_DIGEST,
        parent_verification_id=new_id(),
        parent_result_snapshot_digest=VALID_DIGEST,
        attempt_number=1,
        failed_criteria_ids=["crit-1"],
        required_fixes=[
            RequiredFix(
                id="fix-1",
                description="Fix the failing assertion",
                allowed_path_rules=["src/**"],
                verification_method=VerificationMethod.COMMAND,
            )
        ],
        effective_scope=make_scope(),
        budget=BudgetRequest(
            timeout_seconds=600, max_turns=5, max_rework_iterations=1
        ),
        integrity_digest=VALID_DIGEST,
    )
    data.update(overrides)
    return ReworkContract(**data)


def make_policy_grants(**overrides) -> PolicyGrants:
    data = dict(
        sandbox_profile="trusted_local",
        budgets=BudgetRequest(
            timeout_seconds=600, max_turns=10, max_rework_iterations=2
        ),
    )
    data.update(overrides)
    return PolicyGrants(**data)


def make_policy_decision(**overrides) -> PolicyDecision:
    data = dict(
        subject_type=SubjectType.TASK_CONTRACT,
        subject_id=new_id(),
        subject_digest=VALID_DIGEST,
        policy_bundle_id="bundle-1",
        policy_bundle_digest=VALID_DIGEST,
        engine_version="0.1.0",
        evaluated_at=utc_now(),
        outcome=PolicyOutcome.ALLOW,
        grants=make_policy_grants(),
        integrity_digest=VALID_DIGEST,
    )
    data.update(overrides)
    return PolicyDecision(**data)


def make_approval(**overrides) -> Approval:
    data = dict(
        subject_type=SubjectType.TASK_CONTRACT,
        subject_id=new_id(),
        subject_digest=VALID_DIGEST,
        policy_decision_id=new_id(),
        policy_decision_digest=VALID_DIGEST,
        actor="user@example.com",
        decision="APPROVED",
        requested_at=utc_now(),
        decided_at=utc_now(),
        rationale="Looks safe.",
    )
    data.update(overrides)
    return Approval(**data)


def make_evidence_record(**overrides) -> EvidenceRecord:
    data = dict(
        run_id=new_id(),
        task_id=new_id(),
        subject_type=SubjectType.COMMAND_RUN,
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
        size_bytes=128,
        created_at=utc_now(),
    )
    data.update(overrides)
    return EvidenceRecord(**data)


def make_run(**overrides) -> Run:
    data = dict(
        user_request_ref="artifact://user-request-1",
        user_request_digest=VALID_DIGEST,
        repository_id="repo-1",
        state=LifecycleState.CREATED,
        state_version=0,
        budget_limits=BudgetRequest(
            timeout_seconds=3600, max_turns=50, max_rework_iterations=3
        ),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    data.update(overrides)
    return Run(**data)


def make_policy_ceiling(**overrides):
    from agent_harness.policy.models import PolicyCeiling

    data = dict(
        policy_bundle_id="bundle-1",
        policy_bundle_digest=VALID_DIGEST,
        engine_version="0.1.0",
        workspace_scope=make_scope(),
        allowed_command_ids=frozenset({"pytest", "npm-install"}),
        raw_shell_allowed=False,
        allowed_network_domains=frozenset({"pypi.org"}),
        package_install_allowed=False,
        allowed_mcp_tools=frozenset(),
        allowed_external_systems=frozenset(),
        allowed_database_targets=frozenset(),
        sandbox_profile="trusted_local",
        budget_ceiling=BudgetRequest(
            timeout_seconds=3600, max_turns=50, max_rework_iterations=3
        ),
    )
    data.update(overrides)
    return PolicyCeiling(**data)
