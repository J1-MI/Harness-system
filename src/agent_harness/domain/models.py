"""Pydantic v2 domain models — the single source of truth for contract schemas.

Layering note: this module must not import from ``agent_harness.providers``,
``agent_harness.application``, or any other outer layer. ``UsageRecord`` and
``ProviderError`` live here (not in ``providers/protocol.py``) even though
section 7 of the architecture review lists them as "provider contract" data
models, precisely so that ``AgentInvocation`` (a domain aggregate) can store
normalized usage/error information without domain depending outward. The
providers layer imports these two types from here.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent_harness.domain.digests import Digest, IdentifierSlug, RelativePath, new_id
from agent_harness.domain.enums import (
    ActorType,
    AgentRole,
    ArtifactMediaKind,
    CriterionResult,
    EvidenceTrustTier,
    FailureCategory,
    FailureCode,
    FindingSeverity,
    LifecycleState,
    PolicyOutcome,
    ProviderErrorCode,
    RedactionStatus,
    SessionLifecycle,
    SubjectType,
    VerificationDecision,
    VerificationMethod,
    WorkerResultStatus,
    WorkspaceAccessLevel,
)

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def validate_commit_sha(value: str) -> str:
    """Reject symbolic refs; require a full lowercase-hex commit ID."""

    if not _COMMIT_SHA_PATTERN.match(value):
        raise ValueError(
            "base_commit_sha must be a full commit SHA (40 or 64 lowercase "
            "hex characters), not a symbolic ref"
        )
    return value


class HarnessModel(BaseModel):
    """Base for mutable-but-strict harness models."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ImmutableModel(HarnessModel):
    """Base for accepted/terminal records that must not change post-creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


SchemaVersion = Literal["1.0"]


# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------


class ScopeRules(HarnessModel):
    allowed_path_rules: list[RelativePath] = Field(default_factory=list)
    forbidden_path_rules: list[RelativePath] = Field(default_factory=list)
    allow_new_files: bool = True
    max_changed_files: int | None = Field(default=None, ge=0)
    max_changed_bytes: int | None = Field(default=None, ge=0)
    declared_generated_paths: list[RelativePath] = Field(default_factory=list)


class RequestedCapabilities(HarnessModel):
    """What a contract *asks for*. Never the source of effective permission.

    See ``PolicyGrants`` for the trusted, policy-engine-computed counterpart.
    """

    workspace_access: WorkspaceAccessLevel = WorkspaceAccessLevel.READ_WRITE
    command_ids: list[str] = Field(default_factory=list)
    raw_shell: bool = False
    network_domains: list[str] = Field(default_factory=list)
    package_install: bool = False
    external_systems: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    database_targets: list[str] = Field(default_factory=list)


class BudgetRequest(HarnessModel):
    timeout_seconds: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    max_rework_iterations: int = Field(ge=0)


class RepositoryRef(HarnessModel):
    repository_id: IdentifierSlug
    base_commit_sha: str
    target_ref: str
    expected_repository_fingerprint: Digest

    @field_validator("base_commit_sha")
    @classmethod
    def _check_full_sha(cls, value: str) -> str:
        return validate_commit_sha(value)


class ConstraintSet(HarnessModel):
    typed_constraints: list[str] = Field(default_factory=list)
    notes: str | None = None


class VerificationSpec(HarnessModel):
    method: VerificationMethod
    command_id: str | None = None
    assertion: str | None = None
    manual_instruction: str | None = None
    expected_exit_codes: list[int] = Field(default_factory=list)
    expected_output_match: str | None = None
    required_evidence_kinds: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_method_payload(self) -> "VerificationSpec":
        if self.method is VerificationMethod.COMMAND and not self.command_id:
            raise ValueError("command_id is required when method is COMMAND")
        if self.method is VerificationMethod.ASSERTION and not self.assertion:
            raise ValueError("assertion is required when method is ASSERTION")
        if (
            self.method is VerificationMethod.MANUAL
            and not self.manual_instruction
        ):
            raise ValueError(
                "manual_instruction is required when method is MANUAL"
            )
        return self


class AcceptanceCriterion(HarnessModel):
    id: str
    description: str = Field(min_length=1)
    mandatory: bool
    verification: VerificationSpec


class IntegrityRef(HarnessModel):
    canonical_digest: Digest
    supersedes_contract_id: str | None = None


# ---------------------------------------------------------------------------
# TaskContract
# ---------------------------------------------------------------------------


class TaskContract(ImmutableModel):
    """Codex-proposed / Harness-accepted task contract.

    Deliberately has no ``approval_required`` field and no effective
    capability field: ``requested_capabilities`` is a request only, never a
    grant. Effective permission lives exclusively in ``PolicyDecision.grants``.
    """

    schema_version: SchemaVersion = "1.0"
    contract_id: str = Field(default_factory=new_id)
    contract_revision: int = Field(ge=1)
    run_id: str
    task_id: str
    request_snapshot_ref: str
    objective: str = Field(min_length=1)

    repository: RepositoryRef
    scope: ScopeRules
    constraints: ConstraintSet
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    requested_capabilities: RequestedCapabilities
    budget_request: BudgetRequest
    required_evidence: list[str] = Field(default_factory=list)
    worker_profile_ref: str | None = None
    risk_hints: list[str] = Field(default_factory=list)
    integrity: IntegrityRef


# ---------------------------------------------------------------------------
# WorkerResult
# ---------------------------------------------------------------------------


class ReportedChangedFile(HarnessModel):
    path: RelativePath
    change_type: Literal["ADDED", "MODIFIED", "DELETED", "RENAMED"]
    previous_path: RelativePath | None = None


class ReportedCommand(HarnessModel):
    command_id: str
    reported_exit_code: int | None = None
    reported_duration_ms: int | None = Field(default=None, ge=0)


class ReportedTestRun(HarnessModel):
    name: str
    reported_passed: int | None = Field(default=None, ge=0)
    reported_failed: int | None = Field(default=None, ge=0)
    command_id: str | None = None


class WorkerResult(ImmutableModel):
    """Claude Worker's self-report. Trust tier is ``PROVIDER_REPORTED``.

    All claim-carrying fields use a ``reported_`` prefix so callers cannot
    mistake this for host-observed evidence.
    """

    schema_version: SchemaVersion = "1.0"
    task_id: str
    contract_id: str
    contract_digest: Digest
    invocation_id: str
    provider_session_ref: str

    status: WorkerResultStatus
    implementation_summary: str
    reported_changed_files: list[ReportedChangedFile] = Field(default_factory=list)
    reported_commands: list[ReportedCommand] = Field(default_factory=list)
    reported_tests: list[ReportedTestRun] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unverified_items: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)
    scope_exceptions_requested: list[str] = Field(default_factory=list)
    requested_additional_capabilities: RequestedCapabilities | None = None
    provider_artifact_refs: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None

    @model_validator(mode="after")
    def _check_blocked_reason(self) -> "WorkerResult":
        if self.status in (
            WorkerResultStatus.BLOCKED,
            WorkerResultStatus.FAILED,
        ) and not self.blocked_reason:
            raise ValueError(
                "blocked_reason is required when status is BLOCKED or FAILED"
            )
        return self


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


class CriterionVerification(HarnessModel):
    id: str
    result: CriterionResult
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str | None = None
    waiver_approval_ref: str | None = None

    @model_validator(mode="after")
    def _check_waiver(self) -> "CriterionVerification":
        if self.result is CriterionResult.WAIVED and not self.waiver_approval_ref:
            raise ValueError(
                "waiver_approval_ref is required when result is WAIVED"
            )
        return self


class Finding(HarnessModel):
    id: str
    severity: FindingSeverity
    description: str = Field(min_length=1)
    criterion_id: str | None = None


class VerificationResult(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    verification_id: str = Field(default_factory=new_id)
    task_id: str
    contract_digest: Digest
    result_snapshot_digest: Digest
    evidence_set_digest: Digest
    invocation_id: str

    decision: VerificationDecision
    criteria: list[CriterionVerification] = Field(min_length=1)
    scope_violations: list[str] = Field(default_factory=list)
    security_findings: list[Finding] = Field(default_factory=list)
    quality_findings: list[Finding] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    prohibited_changes: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_decision_payload(self) -> "VerificationResult":
        if self.decision is VerificationDecision.REWORK and not self.required_fixes:
            raise ValueError("required_fixes is required when decision is REWORK")
        if self.decision is VerificationDecision.REJECT and not self.remaining_risks and not self.scope_violations and not self.security_findings:
            raise ValueError(
                "REJECT requires an explicit reason via remaining_risks, "
                "scope_violations, or security_findings"
            )
        return self


# ---------------------------------------------------------------------------
# ReworkContract
# ---------------------------------------------------------------------------


class RequiredFix(HarnessModel):
    id: str
    description: str = Field(min_length=1)
    allowed_path_rules: list[RelativePath] = Field(default_factory=list)
    verification_method: VerificationMethod
    required_evidence: list[str] = Field(default_factory=list)


class ReworkContract(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    rework_id: str = Field(default_factory=new_id)
    task_id: str
    parent_contract_digest: Digest
    parent_verification_id: str
    parent_result_snapshot_digest: Digest
    attempt_number: int = Field(ge=1)
    failed_criteria_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)

    required_fixes: list[RequiredFix] = Field(min_length=1)
    prohibited_changes: list[str] = Field(default_factory=list)
    effective_scope: ScopeRules
    requested_additional_capabilities: RequestedCapabilities | None = None
    budget: BudgetRequest
    integrity_digest: Digest


# ---------------------------------------------------------------------------
# Run / Task
# ---------------------------------------------------------------------------


class BudgetUsage(HarnessModel):
    turns_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    cost_used_usd: float = Field(default=0.0, ge=0.0)
    rework_used: int = Field(default=0, ge=0)


class Run(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    run_id: IdentifierSlug = Field(default_factory=new_id)
    user_request_ref: str
    user_request_digest: Digest
    repository_id: IdentifierSlug
    state: LifecycleState = LifecycleState.CREATED
    state_version: int = Field(default=0, ge=0)
    budget_limits: BudgetRequest
    budget_used: BudgetUsage = Field(default_factory=BudgetUsage)
    current_task_id: str | None = None
    disposition: LifecycleState | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class Task(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    task_id: str = Field(default_factory=new_id)
    run_id: str
    objective: str = Field(min_length=1)
    dependency_ids: list[str] = Field(default_factory=list)
    active_contract_id: str | None = None
    active_contract_revision: int | None = None
    attempt_count: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# AgentSession / AgentInvocation
# ---------------------------------------------------------------------------


class UsageRecord(HarnessModel):
    """Normalized, client-side usage estimate.

    Provider-reported cost/token figures are estimates, not authoritative
    billing (see Claude Agent SDK cost-tracking docs referenced in the
    architecture review); ``is_estimate`` defaults to ``True`` accordingly.
    """

    turns: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    is_estimate: bool = True


class ProviderError(HarnessModel):
    code: ProviderErrorCode
    retriable: bool
    message: str
    provider_original_code: str | None = None
    raw_artifact_ref: str | None = None


class AgentSession(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    session_id: str = Field(default_factory=new_id)
    run_id: str
    task_id: str
    role: AgentRole
    provider_id: str
    provider_version: str
    session_ref: str
    contract_digest: Digest
    context_digest: Digest
    lifecycle: SessionLifecycle = SessionLifecycle.ACTIVE
    created_at: AwareDatetime
    closed_at: AwareDatetime | None = None


class AgentInvocation(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    invocation_id: str = Field(default_factory=new_id)
    session_id: str
    role: AgentRole
    attempt: int = Field(ge=1)
    request_digest: Digest
    deadline: AwareDatetime
    event_refs: list[str] = Field(default_factory=list)
    normalized_result_ref: str | None = None
    normalized_error: ProviderError | None = None
    usage: UsageRecord | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# PolicyDecision / Approval
# ---------------------------------------------------------------------------


class PolicyGrants(HarnessModel):
    """The only legitimate source of effective permission in the system."""

    path_rules: ScopeRules = Field(default_factory=ScopeRules)
    command_ids: list[str] = Field(default_factory=list)
    workspace_access: WorkspaceAccessLevel = WorkspaceAccessLevel.NONE
    network_rules: list[str] = Field(default_factory=list)
    package_rules: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    external_targets: list[str] = Field(default_factory=list)
    sandbox_profile: str
    budgets: BudgetRequest


class PolicyDecision(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    decision_id: str = Field(default_factory=new_id)
    subject_type: SubjectType
    subject_id: str
    subject_digest: Digest
    policy_bundle_id: str
    policy_bundle_digest: Digest
    engine_version: str
    evaluated_at: AwareDatetime

    outcome: PolicyOutcome
    grants: PolicyGrants
    denials: list[str] = Field(default_factory=list)
    approval_requirements: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    evaluated_fact_digests: list[Digest] = Field(default_factory=list)
    expires_at: AwareDatetime | None = None
    integrity_digest: Digest

    @model_validator(mode="after")
    def _check_outcome_payload(self) -> "PolicyDecision":
        if self.outcome is PolicyOutcome.DENY and not self.reason_codes:
            raise ValueError("reason_codes is required when outcome is DENY")
        if (
            self.outcome is PolicyOutcome.REQUIRE_APPROVAL
            and not self.approval_requirements
        ):
            raise ValueError(
                "approval_requirements is required when outcome is "
                "REQUIRE_APPROVAL"
            )
        return self


class Approval(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    approval_id: str = Field(default_factory=new_id)
    subject_type: SubjectType
    subject_id: str
    subject_digest: Digest
    policy_decision_id: str
    policy_decision_digest: Digest
    actor: str
    decision: Literal["APPROVED", "REJECTED"]
    scope: list[str] = Field(default_factory=list)
    requested_at: AwareDatetime
    decided_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    single_use: bool = True
    rationale: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Artifact / EvidenceRecord
# ---------------------------------------------------------------------------


class Artifact(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    artifact_id: str = Field(default_factory=new_id)
    media_type: str
    media_kind: ArtifactMediaKind
    size_bytes: int = Field(ge=0)
    content_digest: Digest
    storage_uri: str
    compression: str | None = None
    redaction_status: RedactionStatus = RedactionStatus.NONE
    created_at: AwareDatetime


class EvidenceProvenance(HarnessModel):
    producer_type: ActorType
    producer_id: str
    invocation_id: str | None = None
    collection_method: str
    trust_tier: EvidenceTrustTier


class EvidenceRecord(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    evidence_id: str = Field(default_factory=new_id)
    run_id: str
    task_id: str
    subject_type: SubjectType
    subject_id: str
    subject_digest: Digest

    kind: str = Field(min_length=1)
    provenance: EvidenceProvenance
    artifact_refs: list[str] = Field(min_length=1)
    media_type: str
    content_digest: Digest
    size_bytes: int = Field(ge=0)
    created_at: AwareDatetime
    redaction_status: RedactionStatus = RedactionStatus.NONE
    truncated: bool = False
    verification_relevance: list[str] = Field(default_factory=list)

    _SELF_REPORTING_ROLES = frozenset(
        {ActorType.WORKER, ActorType.PLANNER, ActorType.VERIFIER}
    )

    @model_validator(mode="after")
    def _check_host_observed_producer(self) -> "EvidenceRecord":
        if (
            self.provenance.trust_tier is EvidenceTrustTier.HOST_OBSERVED
            and self.provenance.producer_type in self._SELF_REPORTING_ROLES
        ):
            raise ValueError(
                "HOST_OBSERVED evidence cannot be produced by an agent role "
                "(provider claims may not be self-promoted to host-observed)"
            )
        return self


# ---------------------------------------------------------------------------
# JournalEntry / ContextSnapshot / FailureRecord
# ---------------------------------------------------------------------------


class JournalEntry(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    sequence: int = Field(ge=0)
    journal_entry_id: str = Field(default_factory=new_id)
    run_id: str
    timestamp_utc: AwareDatetime
    event_type: str
    actor_type: ActorType
    actor_id: str
    correlation_id: str
    state_before: LifecycleState | None = None
    state_after: LifecycleState | None = None
    state_version: int = Field(ge=0)
    payload_json: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    previous_entry_hash: Digest | None = None
    entry_hash: Digest


class ProvidedFileRef(HarnessModel):
    path: RelativePath
    content_digest: Digest


class ContextSnapshot(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    context_snapshot_id: str = Field(default_factory=new_id)
    run_id: str
    task_id: str
    prompt_template_digest: Digest
    contract_ref: str
    contract_digest: Digest
    policy_decision_ref: str | None = None
    policy_decision_digest: Digest | None = None
    provided_files: list[ProvidedFileRef] = Field(default_factory=list)
    tool_result_digests: list[Digest] = Field(default_factory=list)
    mcp_result_digests: list[Digest] = Field(default_factory=list)
    schema_digest: Digest
    provider_id: str
    provider_version: str
    role_profile_digest: Digest
    environment_capability_manifest: dict[str, Any] = Field(default_factory=dict)
    environment_variable_names: list[str] = Field(default_factory=list)
    created_at: AwareDatetime

    @field_validator("environment_variable_names")
    @classmethod
    def _check_names_not_values(cls, value: list[str]) -> list[str]:
        for name in value:
            if "=" in name:
                raise ValueError(
                    f"environment_variable_names must contain names only, "
                    f"not values: {name!r}"
                )
        return value


class FinalManifest(ImmutableModel):
    """The Phase 12 "final report manifest" for one Run — the operator-
    facing summary a ``harness report`` command produces.

    ``manifest_digest`` is a canonical digest over every other field
    (self-consistency: a byte-level change to the manifest changes the
    digest), computed the same way every other accepted digest in this
    codebase is (``compute_model_digest``, excluding this field itself).
    It is **not** a cryptographic signature — review section "감사의
    한계" lists a signed final report manifest as one of several possible
    upgrades for stronger audit guarantees (alongside an OS-keychain HMAC
    key or an external append-only anchor), not as something this phase
    implements. ``journal_head_hash`` (the hash-chained tail of the
    journal at generation time) is what actually lets a reader detect
    later journal tampering, within the hash chain's own known limits —
    it does not protect against a host-level attacker rewriting the whole
    SQLite DB and every blob.
    """

    schema_version: SchemaVersion = "1.0"
    manifest_id: str = Field(default_factory=new_id)
    run_id: str
    generated_at: AwareDatetime
    run_state: LifecycleState
    run_disposition: LifecycleState | None
    budget_used: BudgetUsage
    task_ids: list[str] = Field(default_factory=list)
    journal_entry_count: int = Field(ge=0)
    journal_head_hash: Digest | None
    evidence_ids: list[str] = Field(default_factory=list)
    failure: FailureRecord | None = None
    manifest_digest: Digest


class FailureRecord(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    failure_id: str = Field(default_factory=new_id)
    run_id: str
    task_id: str | None = None
    stage: str
    code: FailureCode
    category: FailureCategory
    retriable: bool
    sanitized_detail: str
    artifact_refs: list[str] = Field(default_factory=list)
    occurred_at: AwareDatetime


# ---------------------------------------------------------------------------
# WorkspaceLease / CommandSpec / CommandRun
# ---------------------------------------------------------------------------


class WorkspaceLease(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    lease_id: str = Field(default_factory=new_id)
    repository_id: IdentifierSlug
    worktree_path_handle: str
    base_commit_sha: str
    branch: str
    owner: str
    heartbeat_at: AwareDatetime
    sandbox_profile: str
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @field_validator("base_commit_sha")
    @classmethod
    def _check_full_sha(cls, value: str) -> str:
        return validate_commit_sha(value)


class CommandSpec(HarnessModel):
    schema_version: SchemaVersion = "1.0"
    command_id: str
    executable_identity: str
    argv_template: list[str] = Field(min_length=1)
    cwd_policy: str
    env_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(gt=0)
    network_class: Literal["NONE", "RESTRICTED", "FULL"] = "NONE"
    policy_version: str

    @field_validator("argv_template")
    @classmethod
    def _check_no_empty_tokens(cls, value: list[str]) -> list[str]:
        if any(token == "" for token in value):
            raise ValueError("argv_template must not contain empty tokens")
        return value


class McpToolSpec(HarnessModel):
    """An admin-registered, harness-owned MCP tool (Phase 11).

    Mirrors ``CommandSpec``'s shape: one record bundles both "which server"
    (``server_id``/``server_execution_digest``) and "which tool on it"
    (``tool_name``/``input_schema``/``classification``), since a tool only
    ever belongs to exactly one server — same precedent as ``CommandSpec``
    merging executable identity and command definition into one record.

    Never populated from a repository's ``.mcp.json`` or a user's global
    MCP config (architecture review: "Provider가 프로젝트 `.mcp.json`이나
    사용자 전역 MCP 설정에 직접 연결해서는 안 된다") — only
    ``execution.mcp_gateway.McpToolCatalog.register()`` creates entries.
    """

    schema_version: SchemaVersion = "1.0"
    mcp_tool_id: str
    server_id: str
    server_execution_digest: Digest
    tool_name: str
    allowed_roles: list[AgentRole] = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    classification: Literal["READ", "WRITE", "DESTRUCTIVE"]
    credential_scope: str
    egress_domains: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(gt=0)
    rate_limit_per_minute: int = Field(gt=0)
    max_result_bytes: int = Field(gt=0)
    requires_approval: bool
    policy_version: str


class CommandRun(ImmutableModel):
    schema_version: SchemaVersion = "1.0"
    command_run_id: str = Field(default_factory=new_id)
    command_spec_id: str
    resolved_argv: list[str] = Field(min_length=1)
    exit_code: int | None = None
    signal: str | None = None
    duration_ms: int = Field(ge=0)
    stdout_artifact_ref: str | None = None
    stderr_artifact_ref: str | None = None
    pre_manifest_ref: str | None = None
    post_manifest_ref: str | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
