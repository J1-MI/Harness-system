"""Behavioral tests for the deterministic Fake Provider."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

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
from agent_harness.domain.models import BudgetRequest, PolicyGrants
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    CancelRequest,
    ProviderCapabilities,
    ProviderCapabilityError,
    ResumeSessionRequest,
    StartSessionRequest,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_capabilities(**overrides) -> ProviderCapabilities:
    data = dict(
        supported_roles=[AgentRole.WORKER],
        structured_output=StructuredOutputSupport.JSON_SCHEMA,
        streaming=StreamingSupport.EVENTS,
        session_resume=SessionResumeSupport.PROCESS_LOCAL,
        session_fork=False,
        native_cancel=True,
        tool_approval_callbacks=False,
        tool_visibility_control=True,
        mcp_control=McpControlSupport.STRICT,
        usage_reporting=UsageReportingSupport.TOKENS,
        driver_kind=DriverKind.SDK,
        driver_version="0.0.0-fake",
        capability_probe_timestamp=_utc_now(),
    )
    data.update(overrides)
    return ProviderCapabilities(**data)


def make_run_request(**overrides) -> AgentRunRequest:
    data = dict(
        role=AgentRole.WORKER,
        task_contract_ref="contract-ref-1",
        task_contract_digest="sha256:" + "0" * 64,
        context_snapshot_ref="context-ref-1",
        context_snapshot_digest="sha256:" + "0" * 64,
        role_profile_ref="worker-profile-1",
        role_profile_digest="sha256:" + "0" * 64,
        output_schema_id="worker_result",
        output_schema_version="1.0",
        output_schema_digest="sha256:" + "0" * 64,
        effective_policy_grants=PolicyGrants(
            sandbox_profile="trusted_local",
            budgets=BudgetRequest(
                timeout_seconds=600, max_turns=10, max_rework_iterations=1
            ),
        ),
        workspace_handle="workspace-handle-1",
        deadline=_utc_now(),
        prompt_payload_artifact_ref="artifact://prompt-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )
    data.update(overrides)
    return AgentRunRequest(**data)


def make_scripted_invocation(invocation_id: str = "inv-1") -> ScriptedInvocation:
    events = [
        AgentEvent(
            invocation_id=invocation_id,
            sequence=0,
            event_type=AgentEventType.SESSION_STARTED,
            occurred_at=_utc_now(),
        ),
        AgentEvent(
            invocation_id=invocation_id,
            sequence=1,
            event_type=AgentEventType.TURN_STARTED,
            occurred_at=_utc_now(),
        ),
        AgentEvent(
            invocation_id=invocation_id,
            sequence=2,
            event_type=AgentEventType.TURN_COMPLETED,
            occurred_at=_utc_now(),
        ),
    ]
    result = AgentRunResult(
        invocation_id=invocation_id,
        protocol_status=ProtocolStatus.SUCCEEDED,
        structured_output={"status": "COMPLETED"},
        started_at=_utc_now(),
        completed_at=_utc_now(),
    )
    return ScriptedInvocation(events=events, result=result)


async def _drain_events(provider: FakeAgentProvider, invocation, after_cursor=None):
    return [
        event async for event in provider.stream_events(invocation, after_cursor=after_cursor)
    ]


# ---------------------------------------------------------------------------
# 12. event ordering
# ---------------------------------------------------------------------------


def test_fake_provider_emits_events_in_scripted_order():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities())
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation())
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        invocation = await provider.start_invocation(session, make_run_request())
        events = await _drain_events(provider, invocation)
        return events

    events = asyncio.run(scenario())
    assert [e.sequence for e in events] == [0, 1, 2]
    assert [e.event_type for e in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.TURN_STARTED,
        AgentEventType.TURN_COMPLETED,
    ]


def test_fake_provider_after_cursor_filters_already_seen_events():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities())
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation())
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        invocation = await provider.start_invocation(session, make_run_request())
        return await _drain_events(provider, invocation, after_cursor="0")

    events = asyncio.run(scenario())
    assert [e.sequence for e in events] == [1, 2]


def test_fake_provider_isolates_events_per_invocation():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities())
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation("inv-a"))
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation("inv-b"))
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        invocation_a = await provider.start_invocation(session, make_run_request())
        invocation_b = await provider.start_invocation(session, make_run_request())
        events_a = await _drain_events(provider, invocation_a)
        events_b = await _drain_events(provider, invocation_b)
        return events_a, events_b

    events_a, events_b = asyncio.run(scenario())
    assert {e.invocation_id for e in events_a} == {"inv-a"}
    assert {e.invocation_id for e in events_b} == {"inv-b"}


# ---------------------------------------------------------------------------
# 13. resume
# ---------------------------------------------------------------------------


def test_fake_provider_resume_returns_the_same_session():
    async def scenario():
        provider = FakeAgentProvider(
            capabilities=make_capabilities(session_resume=SessionResumeSupport.PROCESS_LOCAL)
        )
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        resumed = await provider.resume_session(
            ResumeSessionRequest(prior_session=session, resume_reason="continue after crash")
        )
        return session, resumed

    session, resumed = asyncio.run(scenario())
    assert resumed == session


def test_fake_provider_resume_unknown_session_raises():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities())
        from agent_harness.providers.protocol import ProviderSessionRef

        with pytest.raises(ProviderCapabilityError):
            await provider.resume_session(
                ResumeSessionRequest(
                    prior_session=ProviderSessionRef(
                        opaque_ref="never-existed",
                        provider_id="fake-provider",
                        role=AgentRole.WORKER,
                    ),
                    resume_reason="continue",
                )
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 14. cancel
# ---------------------------------------------------------------------------


def test_fake_provider_cancel_makes_await_result_report_cancelled():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities(native_cancel=True))
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation())
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        invocation = await provider.start_invocation(session, make_run_request())
        cancel_result = await provider.cancel(
            invocation, CancelRequest(invocation_id="inv-1", reason="user requested stop")
        )
        final_result = await provider.await_result(invocation)
        return cancel_result, final_result

    cancel_result, final_result = asyncio.run(scenario())
    assert cancel_result.protocol_status is ProtocolStatus.CANCELLED
    assert final_result.protocol_status is ProtocolStatus.CANCELLED


def test_fake_provider_cancel_without_native_cancel_capability_raises():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities(native_cancel=False))
        provider.queue_invocation(AgentRole.WORKER, make_scripted_invocation())
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        invocation = await provider.start_invocation(session, make_run_request())
        with pytest.raises(ProviderCapabilityError):
            await provider.cancel(
                invocation, CancelRequest(invocation_id="inv-1", reason="stop")
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 15. unsupported capability representation
# ---------------------------------------------------------------------------


def test_fake_provider_rejects_unsupported_role():
    async def scenario():
        provider = FakeAgentProvider(
            capabilities=make_capabilities(supported_roles=[AgentRole.PLANNER])
        )
        with pytest.raises(ProviderCapabilityError):
            await provider.start_session(
                StartSessionRequest(
                    role=AgentRole.WORKER,
                    role_profile_ref="worker-profile-1",
                    role_profile_digest="sha256:" + "0" * 64,
                    contract_digest="sha256:" + "0" * 64,
                    context_snapshot_ref="context-ref-1",
                    context_snapshot_digest="sha256:" + "0" * 64,
                    deadline=_utc_now(),
                )
            )

    asyncio.run(scenario())


def test_fake_provider_resume_fails_closed_when_capability_absent():
    async def scenario():
        provider = FakeAgentProvider(
            capabilities=make_capabilities(session_resume=SessionResumeSupport.NONE)
        )
        from agent_harness.providers.protocol import ProviderSessionRef

        with pytest.raises(ProviderCapabilityError):
            await provider.resume_session(
                ResumeSessionRequest(
                    prior_session=ProviderSessionRef(
                        opaque_ref="whatever",
                        provider_id="fake-provider",
                        role=AgentRole.WORKER,
                    ),
                    resume_reason="continue",
                )
            )

    asyncio.run(scenario())


def test_fake_provider_start_invocation_without_queued_script_fails_closed():
    async def scenario():
        provider = FakeAgentProvider(capabilities=make_capabilities())
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="worker-profile-1",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="context-ref-1",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        with pytest.raises(ProviderCapabilityError):
            await provider.start_invocation(session, make_run_request())

    asyncio.run(scenario())
