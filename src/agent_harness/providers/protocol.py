"""Provider-neutral Protocol and its request/response data models.

One ``AgentProvider`` Protocol serves every role (planner/worker/verifier).
Role behaviour is expressed through ``AgentRole`` + a role profile, not
through separate ``PlannerProvider``/``WorkerProvider``/``VerifierProvider``
protocols — see finding M-02 in the architecture review.

Nothing here performs I/O, spawns processes, or talks to a real SDK; this
module only defines the shape of the contract. ``providers/fake.py``
provides the sole concrete implementation for Phase 1.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from agent_harness.domain.digests import Digest, new_id
from agent_harness.domain.enums import (
    AgentEventType,
    AgentRole,
    DriverKind,
    McpControlSupport,
    ProtocolStatus,
    SessionResumeSupport,
    StreamingSupport,
    StructuredOutputSupport,
    UsageReportingSupport,
)
from agent_harness.domain.models import (
    HarnessModel,
    ImmutableModel,
    PolicyGrants,
    ProviderError,
    UsageRecord,
)

__all__ = [
    "AgentProvider",
    "ProviderCapabilities",
    "ProviderHealth",
    "ProviderSessionRef",
    "ProviderInvocationRef",
    "StartSessionRequest",
    "ResumeSessionRequest",
    "AgentRunRequest",
    "AgentEvent",
    "AgentRunResult",
    "ProviderError",
    "UsageRecord",
    "CancelRequest",
    "CancelResult",
    "ProviderCapabilityError",
]


class ProviderCapabilityError(RuntimeError):
    """Raised when a request needs a capability the provider does not have.

    Per the architecture review, capability mismatches must fail closed —
    callers must never silently downgrade behaviour instead of raising.
    """


# ---------------------------------------------------------------------------
# Capability / health
# ---------------------------------------------------------------------------


class ProviderCapabilities(HarnessModel):
    supported_roles: list[AgentRole] = Field(min_length=1)
    structured_output: StructuredOutputSupport
    streaming: StreamingSupport
    session_resume: SessionResumeSupport
    session_fork: bool
    native_cancel: bool
    tool_approval_callbacks: bool
    tool_visibility_control: bool
    mcp_control: McpControlSupport
    usage_reporting: UsageReportingSupport
    sandbox_modes: list[str] = Field(default_factory=list)
    network_controls: list[str] = Field(default_factory=list)
    max_context: int | None = Field(default=None, gt=0)
    driver_kind: DriverKind
    driver_version: str
    capability_probe_timestamp: AwareDatetime


class ProviderHealth(HarnessModel):
    healthy: bool
    provider_id: str
    provider_version: str
    checked_at: AwareDatetime
    detail: str | None = None


# ---------------------------------------------------------------------------
# Session / invocation refs
# ---------------------------------------------------------------------------


class ProviderSessionRef(HarnessModel):
    opaque_ref: str
    provider_id: str
    role: AgentRole


class ProviderInvocationRef(HarnessModel):
    opaque_ref: str
    provider_id: str


class StartSessionRequest(HarnessModel):
    role: AgentRole
    role_profile_ref: str
    role_profile_digest: Digest
    contract_digest: Digest
    context_snapshot_ref: str
    context_snapshot_digest: Digest
    deadline: AwareDatetime


class ResumeSessionRequest(HarnessModel):
    prior_session: ProviderSessionRef
    resume_reason: str


# ---------------------------------------------------------------------------
# Invocation request / events / result
# ---------------------------------------------------------------------------


class AgentRunRequest(HarnessModel):
    """Everything a provider needs for one turn, and nothing it must not have.

    No raw absolute filesystem paths, API keys, or shell command strings —
    ``workspace_handle`` is an opaque handle resolved by the Workspace Tool
    Broker, not a path.
    """

    invocation_id: str = Field(default_factory=new_id)
    role: AgentRole

    task_contract_ref: str
    task_contract_digest: Digest
    rework_contract_ref: str | None = None
    rework_contract_digest: Digest | None = None

    context_snapshot_ref: str
    context_snapshot_digest: Digest
    role_profile_ref: str
    role_profile_digest: Digest

    output_schema_id: str
    output_schema_version: str
    output_schema_digest: Digest

    effective_policy_grants: PolicyGrants

    workspace_handle: str
    deadline: AwareDatetime

    max_turns: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0.0)

    allowed_tool_ids: list[str] = Field(default_factory=list)
    allowed_mcp_tool_ids: list[str] = Field(default_factory=list)

    prompt_payload_artifact_ref: str
    correlation_id: str
    idempotency_key: str


class AgentEvent(ImmutableModel):
    event_id: str = Field(default_factory=new_id)
    invocation_id: str
    sequence: int = Field(ge=0)
    event_type: AgentEventType
    provider_event_id: str | None = None
    occurred_at: AwareDatetime
    payload: dict = Field(default_factory=dict)
    raw_event_artifact_ref: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class AgentRunResult(ImmutableModel):
    """Protocol success and task success are independent axes.

    ``protocol_status == SUCCEEDED`` only means the provider call
    completed cleanly — it says nothing about whether the underlying
    implementation is correct.
    """

    invocation_id: str
    protocol_status: ProtocolStatus
    structured_output: dict | None = None
    raw_output_artifact_ref: str | None = None
    provider_session_ref: str | None = None
    last_event_cursor: str | None = None
    usage: UsageRecord | None = None
    provider_error: ProviderError | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check_status_payload(self) -> "AgentRunResult":
        if (
            self.protocol_status is ProtocolStatus.PROVIDER_ERROR
            and self.provider_error is None
        ):
            raise ValueError(
                "provider_error is required when protocol_status is "
                "PROVIDER_ERROR"
            )
        if (
            self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must not precede started_at")
        return self


class CancelRequest(HarnessModel):
    invocation_id: str
    reason: str
    force: bool = False


class CancelResult(HarnessModel):
    invocation_id: str
    protocol_status: ProtocolStatus
    cancelled_at: AwareDatetime
    detail: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentProvider(Protocol):
    """Role-neutral async provider contract.

    Implementations: ``providers/fake.py`` (Phase 1.1). Real Codex/Claude
    adapters are out of scope for this phase (see architecture review
    Phase 6/7/8).
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    async def health_check(self) -> ProviderHealth: ...

    async def capabilities(self) -> ProviderCapabilities: ...

    async def start_session(
        self, request: StartSessionRequest
    ) -> ProviderSessionRef: ...

    async def resume_session(
        self, request: ResumeSessionRequest
    ) -> ProviderSessionRef: ...

    async def start_invocation(
        self, session: ProviderSessionRef, request: AgentRunRequest
    ) -> ProviderInvocationRef: ...

    def stream_events(
        self,
        invocation: ProviderInvocationRef,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def await_result(
        self, invocation: ProviderInvocationRef
    ) -> AgentRunResult: ...

    async def cancel(
        self, invocation: ProviderInvocationRef, request: CancelRequest
    ) -> CancelResult: ...

    async def close_session(self, session: ProviderSessionRef) -> None: ...
