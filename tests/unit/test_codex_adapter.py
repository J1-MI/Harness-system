"""Tests for the Codex Planner Adapter (Phase 7).

Runs against real ``openai_codex`` notification/turn dataclasses fed
through fake ``CodexClientLike``/``CodexThreadLike``/``CodexTurnHandleLike``
implementations — no subprocess, no network, no API key required
("keyless replay tests").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ThreadItem,
    ThreadTokenUsage,
    TokenUsageBreakdown,
    Turn,
    TurnError,
    TurnStatus,
)
from openai_codex.models import Notification, TurnCompletedNotification, TurnStartedNotification

from agent_harness.domain.enums import AgentRole, ProtocolStatus
from agent_harness.domain.models import BudgetRequest, PolicyGrants
from agent_harness.providers.codex import CodexPlannerAdapter
from agent_harness.providers.protocol import (
    AgentRunRequest,
    CancelRequest,
    ProviderCapabilityError,
    StartSessionRequest,
)

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_usage(input_tokens: int = 10, output_tokens: int = 5) -> ThreadTokenUsage:
    breakdown = TokenUsageBreakdown(
        cached_input_tokens=0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=0,
        total_tokens=input_tokens + output_tokens,
    )
    return ThreadTokenUsage(last=breakdown, total=breakdown, model_context_window=None)


def make_agent_message_item(text: str, item_id: str = "item-1") -> ThreadItem:
    return ThreadItem(AgentMessageThreadItem(id=item_id, text=text, type="agentMessage"))


def make_turn(
    *, turn_id: str = "turn-1", status: TurnStatus = TurnStatus.completed, text: str | None = "hello",
    error: TurnError | None = None,
) -> Turn:
    items = [make_agent_message_item(text)] if text is not None else []
    return Turn(id=turn_id, status=status, items=items, error=error)


def usage_updated(turn_id: str, usage: ThreadTokenUsage | None = None) -> Notification:
    from openai_codex.models import ThreadTokenUsageUpdatedNotification

    return Notification(
        method="thread/token_usage_updated",
        payload=ThreadTokenUsageUpdatedNotification(
            thread_id="thread-1", turn_id=turn_id, token_usage=usage or make_usage()
        ),
    )


def turn_started(turn: Turn) -> Notification:
    return Notification(method="turn/started", payload=TurnStartedNotification(thread_id="thread-1", turn=turn))


def turn_completed(turn: Turn) -> Notification:
    return Notification(
        method="turn/completed", payload=TurnCompletedNotification(thread_id="thread-1", turn=turn)
    )


class FakeTurnHandle:
    def __init__(self, notifications: list[Notification]):
        self.id = "turn-1"
        self._notifications = notifications
        self.interrupted = False

    async def stream(self):
        for notification in self._notifications:
            yield notification

    async def interrupt(self):
        self.interrupted = True


class FakeThread:
    def __init__(self, turn_batches: list[list[Notification]]):
        self.id = "thread-1"
        self._batches = list(turn_batches)
        self.turn_handles: list[FakeTurnHandle] = []

    async def turn(self, input, **kwargs):
        handle = FakeTurnHandle(self._batches.pop(0))
        self.turn_handles.append(handle)
        return handle


class FakeCodexClient:
    def __init__(self, turn_batches: list[list[Notification]]):
        self.thread = FakeThread(turn_batches)
        self.started_kwargs: dict | None = None
        self.closed = False

    async def thread_start(self, **kwargs):
        self.started_kwargs = kwargs
        return self.thread

    async def thread_resume(self, thread_id, **kwargs):
        self.started_kwargs = kwargs
        return self.thread

    async def login_api_key(self, api_key):
        pass

    async def close(self):
        self.closed = True


def make_adapter(turn_batches: list[list[Notification]]) -> tuple[CodexPlannerAdapter, FakeCodexClient]:
    fake_client = FakeCodexClient(turn_batches)

    async def resolve_prompt(ref: str) -> str:
        return f"prompt for {ref}"

    adapter = CodexPlannerAdapter(resolve_prompt=resolve_prompt, client_factory=lambda: fake_client)
    return adapter, fake_client


def make_start_session_request(role: AgentRole = AgentRole.PLANNER) -> StartSessionRequest:
    return StartSessionRequest(
        role=role,
        role_profile_ref="planner-profile",
        role_profile_digest=VALID_DIGEST,
        contract_digest=VALID_DIGEST,
        context_snapshot_ref="context-ref",
        context_snapshot_digest=VALID_DIGEST,
        deadline=_utc_now(),
    )


def make_run_request(**overrides) -> AgentRunRequest:
    data = dict(
        role=AgentRole.PLANNER,
        task_contract_ref="contract-ref",
        task_contract_digest=VALID_DIGEST,
        context_snapshot_ref="context-ref",
        context_snapshot_digest=VALID_DIGEST,
        role_profile_ref="planner-profile",
        role_profile_digest=VALID_DIGEST,
        output_schema_id="",
        output_schema_version="1.0",
        output_schema_digest=VALID_DIGEST,
        effective_policy_grants=PolicyGrants(
            sandbox_profile="read_only",
            budgets=BudgetRequest(timeout_seconds=600, max_turns=5, max_rework_iterations=0),
        ),
        workspace_handle="workspace-handle-1",
        deadline=_utc_now(),
        prompt_payload_artifact_ref="artifact://prompt-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )
    data.update(overrides)
    return AgentRunRequest(**data)


# ---------------------------------------------------------------------------
# Basic recorded-turn parsing + no-write guarantee
# ---------------------------------------------------------------------------


def test_successful_turn_is_parsed_and_thread_is_read_only_deny_all():
    turn = make_turn(text="the plan is ready")
    batch = [turn_started(turn), usage_updated(turn.id), turn_completed(turn)]

    async def scenario():
        adapter, fake_client = make_adapter([batch])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        events = [e async for e in adapter.stream_events(invocation)]
        result = await adapter.await_result(invocation)
        return fake_client, events, result

    fake_client, events, result = asyncio.run(scenario())

    # "no-write test": every thread this adapter starts must request
    # read_only sandbox + deny_all approvals — nothing it does can write.
    assert fake_client.started_kwargs["sandbox"] is Sandbox.read_only
    assert fake_client.started_kwargs["approval_mode"] is ApprovalMode.deny_all

    assert len(events) > 0
    assert result.protocol_status is ProtocolStatus.SUCCEEDED
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


def test_start_invocation_rejects_worker_role():
    async def scenario():
        adapter, _ = make_adapter([[]])
        with pytest.raises(ProviderCapabilityError):
            await adapter.start_session(
                StartSessionRequest(
                    role=AgentRole.WORKER,
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
# Invalid schema retry limit
# ---------------------------------------------------------------------------


def test_invalid_structured_output_retries_then_succeeds():
    bad_turn = make_turn(turn_id="turn-1", text="not valid json at all")
    good_payload = {
        "run_id": "22222222-2222-2222-2222-222222222222",
        "user_request_ref": "artifact://req",
        "user_request_digest": VALID_DIGEST,
        "repository_id": "repo-1",
        "state": "CREATED",
        "state_version": 0,
        "budget_limits": {"timeout_seconds": 600, "max_turns": 5, "max_rework_iterations": 0},
        "budget_used": {"turns_used": 0, "tokens_used": 0, "cost_used_usd": 0.0, "rework_used": 0},
        "current_task_id": None,
        "disposition": None,
        "created_at": _utc_now().isoformat(),
        "updated_at": _utc_now().isoformat(),
        "schema_version": "1.0",
    }
    import json

    good_turn = make_turn(turn_id="turn-2", text=json.dumps(good_payload))

    batches = [
        [turn_started(bad_turn), turn_completed(bad_turn)],
        [turn_started(good_turn), turn_completed(good_turn)],
    ]

    async def scenario():
        adapter, fake_client = make_adapter(batches)
        session = await adapter.start_session(make_start_session_request())
        request = make_run_request(output_schema_id="__test_run_schema__")
        # Register a fake schema id -> Run model mapping via monkeypatch of
        # the module-level registry the adapter reads from.
        import agent_harness.providers.codex as codex_module
        from agent_harness.domain.models import Run

        codex_module._SCHEMA_ID_TO_MODEL["__test_run_schema__"] = Run
        try:
            invocation = await adapter.start_invocation(session, request)
            events = [e async for e in adapter.stream_events(invocation)]
            result = await adapter.await_result(invocation)
        finally:
            del codex_module._SCHEMA_ID_TO_MODEL["__test_run_schema__"]
        return fake_client, events, result

    fake_client, events, result = asyncio.run(scenario())

    assert len(fake_client.thread.turn_handles) == 2  # one retry happened
    warning_events = [e for e in events if e.event_type.value == "WARNING"]
    assert len(warning_events) == 1
    assert warning_events[0].payload["attempt"] == 1
    assert result.protocol_status is ProtocolStatus.SUCCEEDED
    assert result.structured_output["run_id"] == good_payload["run_id"]


def test_invalid_structured_output_exhausts_retries_and_reports_invalid_output():
    bad_turn_1 = make_turn(turn_id="t1", text="nope")
    bad_turn_2 = make_turn(turn_id="t2", text="still nope")
    bad_turn_3 = make_turn(turn_id="t3", text="nope again")
    batches = [
        [turn_completed(bad_turn_1)],
        [turn_completed(bad_turn_2)],
        [turn_completed(bad_turn_3)],
    ]

    async def scenario():
        adapter, fake_client = make_adapter(batches)
        adapter._max_schema_retries = 2  # noqa: SLF001 - test-only override
        session = await adapter.start_session(make_start_session_request())
        request = make_run_request(output_schema_id="__test_run_schema_2__")
        import agent_harness.providers.codex as codex_module
        from agent_harness.domain.models import Run

        codex_module._SCHEMA_ID_TO_MODEL["__test_run_schema_2__"] = Run
        try:
            invocation = await adapter.start_invocation(session, request)
            [e async for e in adapter.stream_events(invocation)]
            result = await adapter.await_result(invocation)
        finally:
            del codex_module._SCHEMA_ID_TO_MODEL["__test_run_schema_2__"]
        return fake_client, result

    fake_client, result = asyncio.run(scenario())

    assert len(fake_client.thread.turn_handles) == 3  # initial + 2 retries
    assert result.protocol_status is ProtocolStatus.INVALID_OUTPUT


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_interrupts_active_turn_and_forces_cancelled():
    turn = make_turn(text="working...")
    batch = [turn_started(turn), turn_completed(turn)]

    async def scenario():
        adapter, fake_client = make_adapter([batch])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        cancel_result = await adapter.cancel(
            invocation, CancelRequest(invocation_id=invocation.opaque_ref, reason="stop")
        )
        final_result = await adapter.await_result(invocation)
        return fake_client, cancel_result, final_result

    fake_client, cancel_result, final_result = asyncio.run(scenario())

    assert fake_client.thread.turn_handles[0].interrupted is True
    assert cancel_result.protocol_status is ProtocolStatus.CANCELLED
    assert final_result.protocol_status is ProtocolStatus.CANCELLED


# ---------------------------------------------------------------------------
# Provider-level error surfacing
# ---------------------------------------------------------------------------


def test_turn_error_maps_to_provider_error():
    failed_turn = make_turn(
        turn_id="t-err", status=TurnStatus.failed, text=None, error=TurnError(message="upstream failure")
    )
    batch = [turn_completed(failed_turn)]

    async def scenario():
        adapter, _ = make_adapter([batch])
        session = await adapter.start_session(make_start_session_request())
        invocation = await adapter.start_invocation(session, make_run_request())
        return await adapter.await_result(invocation)

    result = asyncio.run(scenario())
    assert result.protocol_status is ProtocolStatus.PROVIDER_ERROR
    assert result.provider_error is not None
    assert "upstream failure" in result.provider_error.message


# ---------------------------------------------------------------------------
# Capability probe
# ---------------------------------------------------------------------------


def test_capabilities_reports_planner_and_verifier_roles():
    async def scenario():
        adapter, _ = make_adapter([[]])
        return await adapter.capabilities()

    capabilities = asyncio.run(scenario())
    assert AgentRole.PLANNER in capabilities.supported_roles
    assert AgentRole.VERIFIER in capabilities.supported_roles
    assert AgentRole.WORKER not in capabilities.supported_roles
    assert "read_only" in capabilities.sandbox_modes


def test_health_check_reflects_missing_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    async def scenario():
        adapter, _ = make_adapter([[]])
        return await adapter.health_check()

    health = asyncio.run(scenario())
    assert health.healthy is False
