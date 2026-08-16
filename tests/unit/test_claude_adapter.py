"""Tests for the Claude Provider Adapter (Phase 6).

Runs against real ``claude_agent_sdk`` message dataclasses fed through a
``FakeSdkClient`` — no subprocess, no network, no API key required for
the default suite ("keyless replay tests"). Only
``test_live_smoke_against_real_api`` touches the real API, and only when
explicitly opted in.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agent_harness.domain.enums import AgentEventType, AgentRole, ProtocolStatus
from agent_harness.domain.models import BudgetRequest, PolicyGrants
from agent_harness.providers.claude import ClaudeAgentAdapter
from agent_harness.providers.protocol import (
    AgentRunRequest,
    CancelRequest,
    ProviderCapabilityError,
    StartSessionRequest,
)
from datetime import datetime, timezone

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeSdkClient:
    """Replays pre-scripted message batches — one batch per connect()/query() call."""

    def __init__(self, options, batches: list[list]):
        self.options = options
        self._batches = list(batches)
        self._current_batch: list | None = None
        self.interrupted = False
        self.disconnected = False

    async def connect(self, prompt=None) -> None:
        self._current_batch = self._batches.pop(0)

    async def query(self, prompt, session_id: str = "default") -> None:
        self._current_batch = self._batches.pop(0)

    async def receive_response(self):
        assert self._current_batch is not None
        for message in self._current_batch:
            yield message

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def make_adapter(batches: list[list], created: list | None = None) -> ClaudeAgentAdapter:
    created_clients = created if created is not None else []

    def factory(options):
        client = FakeSdkClient(options, batches)
        created_clients.append(client)
        return client

    async def resolve_prompt(ref: str) -> str:
        return f"prompt for {ref}"

    def resolve_workspace_handle(handle: str) -> str:
        return "C:/fake/workspace"

    return ClaudeAgentAdapter(
        resolve_prompt=resolve_prompt,
        resolve_workspace_handle=resolve_workspace_handle,
        client_factory=factory,
    )


def make_run_request(**overrides) -> AgentRunRequest:
    data = dict(
        role=AgentRole.WORKER,
        task_contract_ref="contract-ref",
        task_contract_digest=VALID_DIGEST,
        context_snapshot_ref="context-ref",
        context_snapshot_digest=VALID_DIGEST,
        role_profile_ref="worker-profile",
        role_profile_digest=VALID_DIGEST,
        output_schema_id="worker_result",
        output_schema_version="1.0",
        output_schema_digest=VALID_DIGEST,
        effective_policy_grants=PolicyGrants(
            sandbox_profile="trusted_local",
            budgets=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        ),
        workspace_handle="workspace-handle-1",
        deadline=_utc_now(),
        allowed_tool_ids=["bash"],
        prompt_payload_artifact_ref="artifact://prompt-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )
    data.update(overrides)
    return AgentRunRequest(**data)


def make_start_session_request() -> StartSessionRequest:
    return StartSessionRequest(
        role=AgentRole.WORKER,
        role_profile_ref="worker-profile",
        role_profile_digest=VALID_DIGEST,
        contract_digest=VALID_DIGEST,
        context_snapshot_ref="context-ref",
        context_snapshot_digest=VALID_DIGEST,
        deadline=_utc_now(),
    )


def happy_path_batch() -> list:
    return [
        SystemMessage(subtype="init", data={"cwd": "C:/fake/workspace"}),
        AssistantMessage(
            content=[
                TextBlock(text="hello"),
                ToolUseBlock(id="t1", name="bash", input={"command": "ls"}),
            ],
            model="claude-opus-4-8",
            session_id="sess-1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="file1\nfile2", is_error=False)],
        ),
        ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=90,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            stop_reason="end_turn",
            total_cost_usd=0.01,
            usage={"input_tokens": 10, "output_tokens": 5},
            result="done",
        ),
    ]


# ---------------------------------------------------------------------------
# Recorded stream parsing
# ---------------------------------------------------------------------------


def test_recorded_stream_is_parsed_into_normalized_events():
    async def scenario():
        adapter = make_adapter([happy_path_batch()])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        events = [e async for e in adapter.stream_events(invocation)]
        result = await adapter.await_result(invocation)
        return events, result

    events, result = asyncio.run(scenario())

    event_types = [e.event_type for e in events]
    assert event_types == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.TURN_STARTED,
        AgentEventType.MESSAGE_COMPLETED,
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.TOOL_COMPLETED,
        AgentEventType.USAGE_UPDATED,
        AgentEventType.TURN_COMPLETED,
    ]
    assert [e.sequence for e in events] == list(range(len(events)))
    assert events[2].payload["text"] == "hello"
    assert events[3].payload["tool_name"] == "bash"

    assert result.protocol_status is ProtocolStatus.SUCCEEDED
    assert result.usage.turns == 1
    assert result.usage.input_tokens == 10
    assert result.usage.estimated_cost_usd == 0.01
    assert result.provider_session_ref == "sess-1"


def test_start_invocation_rejects_non_worker_role():
    async def scenario():
        adapter = make_adapter([happy_path_batch()])
        with pytest.raises(ProviderCapabilityError):
            await adapter.start_session(
                StartSessionRequest(
                    role=AgentRole.PLANNER,
                    role_profile_ref="p",
                    role_profile_digest=VALID_DIGEST,
                    contract_digest=VALID_DIGEST,
                    context_snapshot_ref="c",
                    context_snapshot_digest=VALID_DIGEST,
                    deadline=_utc_now(),
                )
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_calls_interrupt_and_forces_cancelled_status():
    async def scenario():
        created: list[FakeSdkClient] = []
        adapter = make_adapter([happy_path_batch()], created=created)
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        cancel_result = await adapter.cancel(
            invocation, CancelRequest(invocation_id=invocation.opaque_ref, reason="user stop")
        )
        final_result = await adapter.await_result(invocation)
        return created, cancel_result, final_result

    created, cancel_result, final_result = asyncio.run(scenario())

    assert created[0].interrupted is True
    assert cancel_result.protocol_status is ProtocolStatus.CANCELLED
    # Even though the scripted batch ended with a SUCCEEDED-shaped
    # ResultMessage, cancel() must still force the final status.
    assert final_result.protocol_status is ProtocolStatus.CANCELLED


# ---------------------------------------------------------------------------
# Schema invalid -> INVALID_OUTPUT
# ---------------------------------------------------------------------------


def test_schema_validation_failure_maps_to_invalid_output():
    batch = [
        SystemMessage(subtype="init", data={}),
        ResultMessage(
            subtype="error",
            duration_ms=50,
            duration_api_ms=40,
            is_error=True,
            num_turns=1,
            session_id="sess-2",
            stop_reason="schema_validation_failed",
            errors=["structured output did not match output_schema"],
        ),
    ]

    async def scenario():
        adapter = make_adapter([batch])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        return await adapter.await_result(invocation)

    result = asyncio.run(scenario())
    assert result.protocol_status is ProtocolStatus.INVALID_OUTPUT


def test_generic_provider_error_carries_message():
    batch = [
        ResultMessage(
            subtype="error",
            duration_ms=50,
            duration_api_ms=40,
            is_error=True,
            num_turns=1,
            session_id="sess-3",
            stop_reason="server_error",
            errors=["upstream 500"],
        ),
    ]

    async def scenario():
        adapter = make_adapter([batch])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        return await adapter.await_result(invocation)

    result = asyncio.run(scenario())
    assert result.protocol_status is ProtocolStatus.PROVIDER_ERROR
    assert result.provider_error is not None
    assert "upstream 500" in result.provider_error.message


# ---------------------------------------------------------------------------
# Tool gating (can_use_tool is a real, independent enforcement layer)
# ---------------------------------------------------------------------------


def test_can_use_tool_denies_tools_outside_allowed_tool_ids():
    async def scenario():
        adapter = make_adapter([happy_path_batch()])
        session = await adapter.start_session(make_start_session_request())
        request = make_run_request(allowed_tool_ids=["bash"])
        await adapter.start_invocation(session, request)
        # Build the same options the adapter would build, directly, to
        # exercise the can_use_tool callback in isolation.
        session_state = adapter._sessions[session.opaque_ref]  # noqa: SLF001 - test-only introspection
        options = adapter._build_options(request, session_state, cwd="C:/fake")  # noqa: SLF001
        allowed = await options.can_use_tool("bash", {}, None)
        denied = await options.can_use_tool("write_file", {}, None)
        return allowed, denied

    allowed, denied = asyncio.run(scenario())
    assert allowed.behavior == "allow"
    assert denied.behavior == "deny"


# ---------------------------------------------------------------------------
# Optional live smoke test — real API, opt-in only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_CLAUDE_SMOKE"),
    reason="live smoke test opt-in only (makes a real, billed Claude API call) "
    "-- set RUN_LIVE_CLAUDE_SMOKE=1 and ANTHROPIC_API_KEY to run it",
)
def test_live_smoke_against_real_api(tmp_path):
    from agent_harness.providers.claude import _default_client_factory

    async def resolve_prompt(ref: str) -> str:
        return "Reply with exactly the single word: pong"

    def resolve_workspace_handle(handle: str) -> str:
        return str(tmp_path)

    adapter = ClaudeAgentAdapter(
        resolve_prompt=resolve_prompt,
        resolve_workspace_handle=resolve_workspace_handle,
        client_factory=_default_client_factory,
    )

    async def scenario():
        health = await adapter.health_check()
        assert health.healthy, health.detail
        session = await adapter.start_session(make_start_session_request())
        request = make_run_request(allowed_tool_ids=[], max_turns=1)
        invocation = await adapter.start_invocation(session, request)
        events = [e async for e in adapter.stream_events(invocation)]
        result = await adapter.await_result(invocation)
        await adapter.close_session(session)
        return events, result

    events, result = asyncio.run(scenario())
    assert len(events) > 0
    assert result.protocol_status is ProtocolStatus.SUCCEEDED
