"""Closed vocabularies used across the contract kernel.

These enums are the single source of truth for state names, decisions,
and classification codes referenced throughout ``docs/architecture``.
They intentionally mix lifecycle, decision, and capability vocabularies
in one module because the domain models module is the only consumer.
"""

from __future__ import annotations

from enum import StrEnum


class LifecycleState(StrEnum):
    """Run-level lifecycle states.

    Only the vocabulary is defined here. The transition table itself is
    out of scope for Phase 1.1 (see ``application/transitions.py`` in the
    future roadmap).
    """

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    CONTRACT_VALIDATING = "CONTRACT_VALIDATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    EXECUTING = "EXECUTING"
    FREEZING_RESULT = "FREEZING_RESULT"
    HOST_VALIDATING = "HOST_VALIDATING"
    VERIFYING = "VERIFYING"
    REWORK_CONTRACTING = "REWORK_CONTRACTING"
    AWAITING_MANUAL_REVIEW = "AWAITING_MANUAL_REVIEW"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    READY_FOR_MERGE = "READY_FOR_MERGE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_LIFECYCLE_STATES = frozenset(
    {LifecycleState.READY_FOR_MERGE, LifecycleState.FAILED, LifecycleState.CANCELLED}
)


class FailureCategory(StrEnum):
    """Distinguishes domain failures (e.g. failing tests) from infra failures."""

    DOMAIN = "DOMAIN"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class FailureCode(StrEnum):
    POLICY_HARD_DENY = "POLICY_HARD_DENY"
    POLICY_DENIED = "POLICY_DENIED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TEST_FAILED = "TEST_FAILED"
    REWORK_EXHAUSTED = "REWORK_EXHAUSTED"
    TIMED_OUT = "TIMED_OUT"
    RUN_TIMEOUT = "RUN_TIMEOUT"
    BASE_REVISION_STALE = "BASE_REVISION_STALE"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AgentRole(StrEnum):
    PLANNER = "PLANNER"
    WORKER = "WORKER"
    VERIFIER = "VERIFIER"


class SessionLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class WorkerResultStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class VerificationDecision(StrEnum):
    PASS = "PASS"
    REWORK = "REWORK"
    REJECT = "REJECT"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class CriterionResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIED = "NOT_VERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    WAIVED = "WAIVED"


class VerificationMethod(StrEnum):
    COMMAND = "COMMAND"
    ASSERTION = "ASSERTION"
    MANUAL = "MANUAL"


class FindingSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class PolicyOutcome(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class EvidenceTrustTier(StrEnum):
    HOST_OBSERVED = "HOST_OBSERVED"
    POLICY_ENGINE_GENERATED = "POLICY_ENGINE_GENERATED"
    PROVIDER_EVENT_OBSERVED = "PROVIDER_EVENT_OBSERVED"
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    USER_ATTESTED = "USER_ATTESTED"
    EXTERNAL_MCP_REPORTED = "EXTERNAL_MCP_REPORTED"


class RedactionStatus(StrEnum):
    NONE = "NONE"
    REDACTED = "REDACTED"
    QUARANTINED = "QUARANTINED"


class ProtocolStatus(StrEnum):
    """Outcome of the provider call itself, not of the underlying task."""

    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    LIMIT_REACHED = "LIMIT_REACHED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class ProviderErrorCode(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    INVALID_REQUEST = "INVALID_REQUEST"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    TOOL_DENIED = "TOOL_DENIED"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    PROCESS_EXIT = "PROCESS_EXIT"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


class AgentEventType(StrEnum):
    SESSION_STARTED = "SESSION_STARTED"
    TURN_STARTED = "TURN_STARTED"
    TEXT_DELTA = "TEXT_DELTA"
    MESSAGE_COMPLETED = "MESSAGE_COMPLETED"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_APPROVED = "TOOL_APPROVED"
    TOOL_DENIED = "TOOL_DENIED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    FILE_CHANGE_REPORTED = "FILE_CHANGE_REPORTED"
    USAGE_UPDATED = "USAGE_UPDATED"
    RATE_LIMITED = "RATE_LIMITED"
    WARNING = "WARNING"
    TURN_COMPLETED = "TURN_COMPLETED"
    ERROR = "ERROR"


class StructuredOutputSupport(StrEnum):
    NONE = "NONE"
    JSON_SCHEMA = "JSON_SCHEMA"


class StreamingSupport(StrEnum):
    NONE = "NONE"
    EVENTS = "EVENTS"
    PARTIAL_TOKENS = "PARTIAL_TOKENS"


class SessionResumeSupport(StrEnum):
    NONE = "NONE"
    PROCESS_LOCAL = "PROCESS_LOCAL"
    DURABLE = "DURABLE"


class McpControlSupport(StrEnum):
    NONE = "NONE"
    EXPLICIT = "EXPLICIT"
    STRICT = "STRICT"


class UsageReportingSupport(StrEnum):
    NONE = "NONE"
    TOKENS = "TOKENS"
    ESTIMATED_COST = "ESTIMATED_COST"


class DriverKind(StrEnum):
    SDK = "SDK"
    CLI = "CLI"
    APP_SERVER = "APP_SERVER"


class ActorType(StrEnum):
    USER = "USER"
    HARNESS = "HARNESS"
    PLANNER = "PLANNER"
    VERIFIER = "VERIFIER"
    WORKER = "WORKER"
    MCP_SERVER = "MCP_SERVER"
    HOST_TEST_RUNNER = "HOST_TEST_RUNNER"
    POLICY_ENGINE = "POLICY_ENGINE"
    ADAPTER = "ADAPTER"


class SubjectType(StrEnum):
    RUN = "RUN"
    TASK = "TASK"
    TASK_CONTRACT = "TASK_CONTRACT"
    REWORK_CONTRACT = "REWORK_CONTRACT"
    COMMAND_SPEC = "COMMAND_SPEC"
    COMMAND_RUN = "COMMAND_RUN"
    MCP_TOOL = "MCP_TOOL"
    NETWORK_DOMAIN = "NETWORK_DOMAIN"
    WORKSPACE_ACCESS = "WORKSPACE_ACCESS"
    ARTIFACT = "ARTIFACT"
    VERIFICATION = "VERIFICATION"
    CAPABILITY_REQUEST = "CAPABILITY_REQUEST"


class WorkspaceAccessLevel(StrEnum):
    NONE = "NONE"
    READ = "READ"
    READ_WRITE = "READ_WRITE"


class ArtifactMediaKind(StrEnum):
    TEXT = "TEXT"
    JSON = "JSON"
    BINARY = "BINARY"
    DIFF = "DIFF"
    LOG = "LOG"


class PendingActionKind(StrEnum):
    """What a Run is waiting on while parked in an ``AWAITING_*``/recovery state."""

    APPROVAL = "APPROVAL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    FINAL_APPROVAL = "FINAL_APPROVAL"
    RECOVERY_DECISION = "RECOVERY_DECISION"


class IsolationBackend(StrEnum):
    """Execution isolation strength (see L-03 / section 13's open question).

    Only ``TRUSTED_LOCAL`` is implemented as of Phase 3.2 — see
    ``execution/sandbox.py``. ``WSL2`` and ``CONTAINER`` are named here so
    the capability gap is structural (a real ``CAPABILITY_MISMATCH``) and
    not a silent downgrade, per the fail-closed principle used throughout.
    """

    TRUSTED_LOCAL = "TRUSTED_LOCAL"
    WSL2 = "WSL2"
    CONTAINER = "CONTAINER"
