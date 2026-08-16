"""Runs the shared Protocol conformance suite against FakeAgentProvider."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import AgentEvent, AgentRunResult, ProviderCapabilities

from .provider_conformance import run_conformance_suite


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_capabilities(**overrides) -> ProviderCapabilities:
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


def _make_scripted_invocation(invocation_id: str) -> ScriptedInvocation:
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
            event_type=AgentEventType.TURN_COMPLETED,
            occurred_at=_utc_now(),
        ),
    ]
    result = AgentRunResult(
        invocation_id=invocation_id,
        protocol_status=ProtocolStatus.SUCCEEDED,
        started_at=_utc_now(),
        completed_at=_utc_now(),
    )
    return ScriptedInvocation(events=events, result=result)


def test_fake_provider_satisfies_conformance_suite():
    async def scenario():
        provider = FakeAgentProvider(capabilities=_make_capabilities())
        for i in range(3):
            provider.queue_invocation(AgentRole.WORKER, _make_scripted_invocation(f"inv-{i}"))
        await run_conformance_suite(provider)

    asyncio.run(scenario())


def test_fake_provider_without_native_cancel_still_satisfies_conformance_suite():
    async def scenario():
        provider = FakeAgentProvider(capabilities=_make_capabilities(native_cancel=False))
        for i in range(3):
            provider.queue_invocation(AgentRole.WORKER, _make_scripted_invocation(f"inv-{i}"))
        await run_conformance_suite(provider)

    asyncio.run(scenario())
