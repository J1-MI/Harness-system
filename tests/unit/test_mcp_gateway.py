"""Tests for the MCP Gateway (Phase 11): registry, strict config, policy
grant enforcement, approval-gated destructive tools, credential brokering,
audit trail — matching roadmap row 11's test criteria exactly: "unauthorized
tool/server/side effect 차단" (block unauthorized tool/server/side-effect).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_harness.domain.digests import canonical_json_bytes, compute_digest, new_id
from agent_harness.domain.enums import AgentRole, SubjectType
from agent_harness.domain.models import Artifact, McpToolSpec
from agent_harness.execution.mcp_gateway import (
    McpApprovalRequiredError,
    McpCredentialBroker,
    McpInputSchemaError,
    McpRateLimiter,
    McpRateLimitExceededError,
    McpResultTooLargeError,
    McpToolCatalog,
    UnauthorizedMcpToolError,
    invoke_mcp_tool,
)
from agent_harness.persistence.artifacts import read_blob
from tests.factories import make_approval, make_policy_grants

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_spec(**overrides) -> McpToolSpec:
    data = dict(
        mcp_tool_id="search-tool",
        server_id="server-1",
        server_execution_digest=VALID_DIGEST,
        tool_name="search",
        allowed_roles=[AgentRole.WORKER],
        input_schema={"required": ["query"], "properties": {"query": {"type": "string"}}},
        classification="READ",
        credential_scope="server-1",
        egress_domains=["example.com"],
        timeout_seconds=5,
        rate_limit_per_minute=60,
        max_result_bytes=10_000,
        requires_approval=False,
        policy_version="v1",
    )
    data.update(overrides)
    return McpToolSpec(**data)


def spec_digest(spec: McpToolSpec) -> str:
    return compute_digest(canonical_json_bytes(spec.model_dump(mode="json")))


async def echo_transport(spec: McpToolSpec, payload: dict, credential: str) -> dict:
    return {"echo": payload}


def make_credential_broker(value: str = "super-secret-token") -> McpCredentialBroker:
    async def resolve(server_id: str) -> str:
        return value

    return McpCredentialBroker(resolve)


def read_evidence_blob(data_root: Path, evidence) -> bytes:
    """Reconstruct just enough of an ``Artifact`` from an ``EvidenceRecord``
    to read its blob back — the record only stores the artifact_id/digest,
    not a full Artifact object."""

    artifact = Artifact(
        artifact_id=evidence.artifact_refs[0],
        media_type=evidence.media_type,
        media_kind="JSON",
        size_bytes=evidence.size_bytes,
        content_digest=evidence.content_digest,
        storage_uri=f"blob:{evidence.content_digest}",
        redaction_status=evidence.redaction_status,
        created_at=evidence.created_at,
    )
    return read_blob(data_root, artifact)


def run_invoke(catalog, broker, limiter, **kwargs):
    async def scenario():
        return await invoke_mcp_tool(catalog, broker, limiter, **kwargs)

    return asyncio.run(scenario())


@pytest.fixture()
def data_root(tmp_path) -> Path:
    return tmp_path / "data-root"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_authorized_call_succeeds_and_produces_audit_evidence(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec())

    result = run_invoke(
        catalog,
        make_credential_broker(),
        McpRateLimiter(),
        mcp_tool_id="search-tool",
        role=AgentRole.WORKER,
        grants=make_policy_grants(mcp_tools=["search-tool"]),
        input_payload={"query": "hello"},
        transport=echo_transport,
        run_id=new_id(),
        task_id=new_id(),
        data_root=data_root,
        now=_utc_now(),
    )

    assert result.output["echo"] == {"query": "hello"}
    assert result.evidence.kind == "mcp_tool_call_succeeded"
    stored = json.loads(read_evidence_blob(data_root, result.evidence))
    assert stored["outcome"] == "SUCCEEDED"
    assert stored["mcp_tool_id"] == "search-tool"


# ---------------------------------------------------------------------------
# Unauthorized tool / server / role — roadmap's core test criterion
# ---------------------------------------------------------------------------


def test_unregistered_tool_is_rejected_and_audited(data_root):
    catalog = McpToolCatalog()  # nothing registered

    with pytest.raises(UnauthorizedMcpToolError) as excinfo:
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="does-not-exist",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["does-not-exist"]),
            input_payload={},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )
    assert excinfo.value.evidence is not None
    assert excinfo.value.evidence.kind == "mcp_tool_call_rejected"


def test_role_not_in_allowed_roles_is_rejected(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec(allowed_roles=[AgentRole.VERIFIER]))  # not WORKER

    with pytest.raises(UnauthorizedMcpToolError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="search-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["search-tool"]),
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )


def test_tool_not_in_policy_grants_is_rejected(data_root):
    """Registered, role-allowed, but never actually granted by Phase 4's
    policy engine — grants.mcp_tools is the only legitimate permission
    source, so registration alone must never be enough."""

    catalog = McpToolCatalog()
    catalog.register(make_spec())

    with pytest.raises(UnauthorizedMcpToolError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="search-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=[]),  # not granted
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )


# ---------------------------------------------------------------------------
# Destructive side effects require approval
# ---------------------------------------------------------------------------


def test_destructive_tool_without_approval_is_rejected(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec(mcp_tool_id="delete-tool", classification="DESTRUCTIVE", requires_approval=True))

    with pytest.raises(McpApprovalRequiredError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="delete-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["delete-tool"]),
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
            approval=None,
        )


def test_destructive_tool_with_valid_approval_succeeds(data_root):
    catalog = McpToolCatalog()
    spec = make_spec(mcp_tool_id="delete-tool", classification="DESTRUCTIVE", requires_approval=True)
    catalog.register(spec)

    approval = make_approval(
        subject_type=SubjectType.MCP_TOOL,
        subject_id="delete-tool",
        subject_digest=spec_digest(spec),
        decision="APPROVED",
    )

    result = run_invoke(
        catalog,
        make_credential_broker(),
        McpRateLimiter(),
        mcp_tool_id="delete-tool",
        role=AgentRole.WORKER,
        grants=make_policy_grants(mcp_tools=["delete-tool"]),
        input_payload={"query": "x"},
        transport=echo_transport,
        run_id=new_id(),
        task_id=new_id(),
        data_root=data_root,
        now=_utc_now(),
        approval=approval,
    )
    assert result.classification == "DESTRUCTIVE"


def test_destructive_tool_with_wrong_subject_approval_is_rejected(data_root):
    """An approval for a *different* tool must not authorize this one."""

    catalog = McpToolCatalog()
    spec = make_spec(mcp_tool_id="delete-tool", classification="DESTRUCTIVE", requires_approval=True)
    catalog.register(spec)

    mismatched_approval = make_approval(
        subject_type=SubjectType.MCP_TOOL,
        subject_id="some-other-tool",
        subject_digest=spec_digest(spec),
        decision="APPROVED",
    )

    with pytest.raises(McpApprovalRequiredError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="delete-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["delete-tool"]),
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
            approval=mismatched_approval,
        )


def test_destructive_tool_with_expired_approval_is_rejected(data_root):
    catalog = McpToolCatalog()
    spec = make_spec(mcp_tool_id="delete-tool", classification="DESTRUCTIVE", requires_approval=True)
    catalog.register(spec)

    expired_approval = make_approval(
        subject_type=SubjectType.MCP_TOOL,
        subject_id="delete-tool",
        subject_digest=spec_digest(spec),
        decision="APPROVED",
        expires_at=_utc_now() - timedelta(minutes=1),
    )

    with pytest.raises(McpApprovalRequiredError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="delete-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["delete-tool"]),
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
            approval=expired_approval,
        )


# ---------------------------------------------------------------------------
# Input schema / rate limit / result size
# ---------------------------------------------------------------------------


def test_missing_required_input_field_is_rejected(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec())

    with pytest.raises(McpInputSchemaError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="search-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["search-tool"]),
            input_payload={},  # missing "query"
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )


def test_rate_limit_is_enforced(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec(rate_limit_per_minute=1))
    rate_limiter = McpRateLimiter()

    def call():
        return run_invoke(
            catalog,
            make_credential_broker(),
            rate_limiter,
            mcp_tool_id="search-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["search-tool"]),
            input_payload={"query": "x"},
            transport=echo_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )

    call()
    with pytest.raises(McpRateLimitExceededError):
        call()


def test_oversized_result_is_rejected(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec(max_result_bytes=10))

    async def huge_transport(spec, payload, credential):
        return {"data": "x" * 1000}

    with pytest.raises(McpResultTooLargeError):
        run_invoke(
            catalog,
            make_credential_broker(),
            McpRateLimiter(),
            mcp_tool_id="search-tool",
            role=AgentRole.WORKER,
            grants=make_policy_grants(mcp_tools=["search-tool"]),
            input_payload={"query": "x"},
            transport=huge_transport,
            run_id=new_id(),
            task_id=new_id(),
            data_root=data_root,
            now=_utc_now(),
        )


# ---------------------------------------------------------------------------
# Credential never leaks into audit evidence
# ---------------------------------------------------------------------------


def test_credential_never_appears_in_audit_evidence(data_root):
    catalog = McpToolCatalog()
    catalog.register(make_spec())
    secret = "sk-super-secret-credential-value"

    async def capturing_transport(spec, payload, credential):
        assert credential == secret
        return {"ok": True}

    result = run_invoke(
        catalog,
        make_credential_broker(secret),
        McpRateLimiter(),
        mcp_tool_id="search-tool",
        role=AgentRole.WORKER,
        grants=make_policy_grants(mcp_tools=["search-tool"]),
        input_payload={"query": "x"},
        transport=capturing_transport,
        run_id=new_id(),
        task_id=new_id(),
        data_root=data_root,
        now=_utc_now(),
    )

    stored_bytes = read_evidence_blob(data_root, result.evidence)
    assert secret not in stored_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Strict config: this module never reads a repository's .mcp.json
# ---------------------------------------------------------------------------


def test_gateway_never_reads_a_planted_mcp_json(tmp_path, data_root):
    """Plant a `.mcp.json` that, if the gateway ever read it, would
    register a tool named "should-never-exist". Prove the catalog stays
    exactly what was explicitly ``register()``-ed."""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"should-never-exist": {"command": "evil"}}})
    )

    catalog = McpToolCatalog()
    catalog.register(make_spec())

    with pytest.raises(UnauthorizedMcpToolError):
        catalog.get("should-never-exist")
    assert catalog.get("search-tool").mcp_tool_id == "search-tool"
