"""MCP Governance (Phase 11): the strict gateway between policy-granted
``mcp_tools`` and any actual MCP tool call.

Architecture review, "## MCP의 위치": MCP governance lives in the Control
Plane, actual tool invocation in the Execution Plane's MCP Gateway.
"Provider가 프로젝트 `.mcp.json`이나 사용자 전역 MCP 설정에 직접 연결해서는
안 된다" — a Provider (Claude/Codex adapter) must never reach an MCP server
on its own; every call goes through ``invoke_mcp_tool`` below, which the
Harness alone configures via ``McpToolCatalog.register()``.

This module never reads any `.mcp.json` or MCP config file — there is no
file-reading code path here at all. That is what "strict config" means
structurally, not just as a runtime check: a repo can ship whatever
`.mcp.json` it wants, and this gateway will never look at it. It also
never contacts a real MCP server itself — the actual network/stdio
transport is injected (mirrors ``execution.process``/``command_broker``'s
"the harness owns *when* something runs, not *how* it talks to the OS"
split), which keeps this module testable without a live MCP server.

Five things the review says the Harness — not the Provider, not the
repository — must decide are all enforced here, in this order:
registration (server ID + tool + role allowlist), the actual policy grant
(``PolicyGrants.mcp_tools``, the only legitimate permission source per
Phase 4), approval (only for ``requires_approval`` tools), input schema,
then timeout/rate-limit/result-size ceilings. "MCP의 `readOnlyHint`나
destructive annotation은 참고 정보일 뿐 신뢰 경계가 아니다" — this module
never reads such hints from a server; ``McpToolSpec.classification`` is
the Harness's own, separately registered judgment.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_harness.domain.digests import canonical_json_bytes, compute_digest
from agent_harness.domain.enums import ActorType, AgentRole, ArtifactMediaKind, EvidenceTrustTier, SubjectType
from agent_harness.domain.models import (
    Approval,
    EvidenceProvenance,
    EvidenceRecord,
    McpToolSpec,
    PolicyGrants,
)
from agent_harness.persistence.artifacts import ArtifactQuotaExceededError, write_blob

__all__ = [
    "McpGatewayError",
    "UnauthorizedMcpToolError",
    "McpApprovalRequiredError",
    "McpInputSchemaError",
    "McpRateLimitExceededError",
    "McpResultTooLargeError",
    "McpToolCatalog",
    "McpCredentialBroker",
    "McpRateLimiter",
    "McpCallResult",
    "invoke_mcp_tool",
]


class McpGatewayError(RuntimeError):
    """Base class for every rejection this gateway can raise.

    ``invoke_mcp_tool`` always attaches the rejection's audit
    ``EvidenceRecord`` as ``.evidence`` before raising, so a caller that
    wants to persist/inspect the audit trail for a *rejected* call can
    still get at it from the exception, not just from a successful
    ``McpCallResult``.
    """

    evidence: EvidenceRecord | None = None


class UnauthorizedMcpToolError(McpGatewayError):
    """The tool is unregistered, the role isn't allowed to call it, or it
    isn't present in this invocation's granted ``PolicyGrants.mcp_tools``."""


class McpApprovalRequiredError(McpGatewayError):
    """The tool is ``requires_approval`` and no valid, matching, unexpired,
    ``APPROVED`` ``Approval`` was supplied."""


class McpInputSchemaError(McpGatewayError):
    pass


class McpRateLimitExceededError(McpGatewayError):
    pass


class McpResultTooLargeError(McpGatewayError):
    pass


class McpToolCatalog:
    """Admin-registered MCP tools only — never auto-discovered.

    Mirrors ``execution.command_broker.CommandCatalog``. The only way a
    tool becomes callable is an explicit ``register()`` call; nothing in
    this class (or this module) reads a config file to populate itself.
    """

    def __init__(self) -> None:
        self._specs: dict[str, McpToolSpec] = {}

    def register(self, spec: McpToolSpec) -> None:
        self._specs[spec.mcp_tool_id] = spec

    def get(self, mcp_tool_id: str) -> McpToolSpec:
        try:
            return self._specs[mcp_tool_id]
        except KeyError:
            raise UnauthorizedMcpToolError(f"MCP tool {mcp_tool_id!r} is not registered") from None


class McpCredentialBroker:
    """Resolves a per-server credential only at call time.

    The resolved value is handed directly to the injected ``transport``
    and is never returned to the caller, logged, or written into any
    audit evidence — the same "credential never reaches anything but the
    one place that needs it" principle already applied to command
    execution's env allowlist (Phase 3.2).
    """

    def __init__(self, resolve: Callable[[str], Awaitable[str]]) -> None:
        self._resolve = resolve

    async def resolve(self, server_id: str) -> str:
        return await self._resolve(server_id)


class McpRateLimiter:
    """A plain sliding-window call counter, per ``mcp_tool_id``.

    Deliberately not global/module-level state: the caller owns one
    instance (typically per Run) and passes it in, same as every other
    piece of mutable state in this codebase.
    """

    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check_and_record(self, mcp_tool_id: str, limit_per_minute: int, *, now: float) -> None:
        window = self._calls[mcp_tool_id]
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit_per_minute:
            raise McpRateLimitExceededError(
                f"MCP tool {mcp_tool_id!r} exceeded {limit_per_minute} calls/minute"
            )
        window.append(now)


@dataclass
class McpCallResult:
    mcp_tool_id: str
    classification: str
    output: dict[str, Any]
    evidence: EvidenceRecord
    duration_ms: int


def _spec_digest(spec: McpToolSpec) -> str:
    return compute_digest(canonical_json_bytes(spec.model_dump(mode="json")))


def _validate_input_schema(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    """A deliberately partial structural check — required keys present,
    and each declared top-level property's JSON type matches — not a full
    JSON Schema validator. Adding a JSON Schema library is a reasonable
    future upgrade; this is enough to catch a materially wrong call shape
    without a new dependency for what amounts to input sanity-checking at
    one boundary.
    """

    _TYPE_MAP = {
        "string": str, "number": (int, float), "integer": int,
        "boolean": bool, "object": dict, "array": list,
    }
    for key in schema.get("required", []):
        if key not in payload:
            raise McpInputSchemaError(f"missing required field {key!r}")
    properties = schema.get("properties", {})
    for key, value in payload.items():
        declared = properties.get(key, {}).get("type")
        expected_type = _TYPE_MAP.get(declared)
        if expected_type is not None and not isinstance(value, expected_type):
            raise McpInputSchemaError(
                f"field {key!r} has type {type(value).__name__!r}, expected {declared!r}"
            )


def _check_approval(spec: McpToolSpec, approval: Approval | None, *, now: datetime) -> None:
    if approval is None:
        raise McpApprovalRequiredError(
            f"MCP tool {spec.mcp_tool_id!r} requires approval and none was supplied"
        )
    if approval.subject_type is not SubjectType.MCP_TOOL or approval.subject_id != spec.mcp_tool_id:
        raise McpApprovalRequiredError(
            f"approval is not bound to MCP tool {spec.mcp_tool_id!r} "
            f"(subject_type={approval.subject_type!r}, subject_id={approval.subject_id!r})"
        )
    if approval.subject_digest != _spec_digest(spec):
        raise McpApprovalRequiredError(
            "approval subject_digest does not match the current McpToolSpec — "
            "the registered spec changed since this approval was granted"
        )
    if approval.decision != "APPROVED":
        raise McpApprovalRequiredError(f"approval decision is {approval.decision!r}, not APPROVED")
    if approval.expires_at is not None and now > approval.expires_at:
        raise McpApprovalRequiredError(f"approval expired at {approval.expires_at.isoformat()}")


def _write_audit(
    *, data_root: Path, run_id: str, task_id: str, now: datetime,
    mcp_tool_id: str, server_id: str, outcome: str, detail: dict[str, Any],
) -> EvidenceRecord:
    """Every call — authorized or rejected — leaves an evidence trail.

    Never includes the resolved credential (the caller passes it straight
    to ``transport`` and this function never sees it).
    """

    payload = canonical_json_bytes({"outcome": outcome, "mcp_tool_id": mcp_tool_id, **detail})
    artifact = write_blob(
        data_root, payload, media_type="application/json", media_kind=ArtifactMediaKind.JSON,
        redact=True, now=now,
    )
    return EvidenceRecord(
        run_id=run_id,
        task_id=task_id,
        subject_type=SubjectType.MCP_TOOL,
        subject_id=mcp_tool_id,
        subject_digest=compute_digest(payload),
        kind=f"mcp_tool_call_{outcome.lower()}",
        provenance=EvidenceProvenance(
            producer_type=ActorType.MCP_SERVER,
            producer_id=server_id,
            collection_method="mcp_gateway_call",
            trust_tier=EvidenceTrustTier.EXTERNAL_MCP_REPORTED,
        ),
        artifact_refs=[artifact.artifact_id],
        media_type="application/json",
        content_digest=artifact.content_digest,
        size_bytes=artifact.size_bytes,
        created_at=now,
    )


async def invoke_mcp_tool(
    catalog: McpToolCatalog,
    credential_broker: McpCredentialBroker,
    rate_limiter: McpRateLimiter,
    *,
    mcp_tool_id: str,
    role: AgentRole,
    grants: PolicyGrants,
    input_payload: dict[str, Any],
    transport: Callable[[McpToolSpec, dict[str, Any], str], Awaitable[dict[str, Any]]],
    run_id: str,
    task_id: str,
    data_root: Path,
    now: datetime,
    approval: Approval | None = None,
) -> McpCallResult:
    """Gate, then (if allowed) actually make one MCP tool call.

    Raises a specific ``McpGatewayError`` subclass for every rejection
    reason and, in every case — success or rejection — the call is
    recorded via ``_write_audit`` before the result/exception reaches the
    caller. ``transport`` is the only thing that actually talks to an MCP
    server; this function never does network/stdio I/O itself.
    """

    audit_detail: dict[str, Any] = {"role": role.value}
    try:
        spec = catalog.get(mcp_tool_id)
        audit_detail["server_id"] = spec.server_id

        if role not in spec.allowed_roles:
            raise UnauthorizedMcpToolError(
                f"role {role.value!r} is not in {mcp_tool_id!r}'s allowed_roles={spec.allowed_roles!r}"
            )
        if mcp_tool_id not in grants.mcp_tools:
            raise UnauthorizedMcpToolError(
                f"MCP tool {mcp_tool_id!r} was not granted by policy (grants.mcp_tools={grants.mcp_tools!r})"
            )
        if spec.requires_approval:
            _check_approval(spec, approval, now=now)

        _validate_input_schema(input_payload, spec.input_schema)
        rate_limiter.check_and_record(mcp_tool_id, spec.rate_limit_per_minute, now=time.monotonic())

        credential = await credential_broker.resolve(spec.server_id)

        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                transport(spec, input_payload, credential), timeout=spec.timeout_seconds
            )
        except asyncio.TimeoutError:
            raise McpGatewayError(f"MCP tool {mcp_tool_id!r} timed out after {spec.timeout_seconds}s") from None
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            output_bytes = canonical_json_bytes(output)
            artifact = write_blob(
                data_root, output_bytes, media_type="application/json", media_kind=ArtifactMediaKind.JSON,
                max_size_bytes=spec.max_result_bytes, redact=True, now=now,
            )
        except ArtifactQuotaExceededError as exc:
            raise McpResultTooLargeError(str(exc)) from None

        evidence = _write_audit(
            data_root=data_root, run_id=run_id, task_id=task_id, now=now,
            mcp_tool_id=mcp_tool_id, server_id=spec.server_id, outcome="SUCCEEDED",
            detail={**audit_detail, "result_digest": artifact.content_digest, "duration_ms": duration_ms},
        )
        return McpCallResult(
            mcp_tool_id=mcp_tool_id, classification=spec.classification, output=output,
            evidence=evidence, duration_ms=duration_ms,
        )
    except McpGatewayError as exc:
        exc.evidence = _write_audit(
            data_root=data_root, run_id=run_id, task_id=task_id, now=now,
            mcp_tool_id=mcp_tool_id, server_id=audit_detail.get("server_id", "unknown"),
            outcome="REJECTED", detail={**audit_detail, "reason": str(exc), "error_type": type(exc).__name__},
        )
        raise
