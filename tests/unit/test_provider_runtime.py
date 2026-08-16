"""Tests for the provider-neutral runtime (Phase 5): registry, capability
negotiation, event normalization, cancel, usage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agent_harness.application.usage import BudgetExceededError, accumulate_usage, check_budget
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
from agent_harness.domain.models import BudgetRequest, BudgetUsage, UsageRecord
from agent_harness.providers.cancel import cancel_invocation
from agent_harness.providers.capabilities import CapabilityRequirement, require_capabilities
from agent_harness.providers.event_stream import OutOfOrderEventError, normalize_events
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import (
    AgentEvent,
    AgentRunResult,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderInvocationRef,
    StartSessionRequest,
)
from agent_harness.providers.registry import ProviderNotRegisteredError, ProviderRegistry


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


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------


def test_registry_get_unregistered_role_raises():
    registry = ProviderRegistry()
    with pytest.raises(ProviderNotRegisteredError):
        registry.get(AgentRole.WORKER)


def test_registry_register_and_get_roundtrip():
    registry = ProviderRegistry()
    provider = FakeAgentProvider(capabilities=make_capabilities())
    registry.register(AgentRole.WORKER, provider)
    assert registry.get(AgentRole.WORKER) is provider
    assert registry.registered_roles() == frozenset({AgentRole.WORKER})


def test_registry_probe_caches_and_returns_capabilities():
    async def scenario():
        registry = ProviderRegistry()
        provider = FakeAgentProvider(capabilities=make_capabilities())
        registry.register(AgentRole.WORKER, provider)
        caps_a = await registry.probe(AgentRole.WORKER)
        caps_b = await registry.probe(AgentRole.WORKER)
        assert caps_a == caps_b

    asyncio.run(scenario())


def test_registry_probe_all_covers_every_registered_role():
    async def scenario():
        registry = ProviderRegistry()
        registry.register(
            AgentRole.WORKER,
            FakeAgentProvider(capabilities=make_capabilities(supported_roles=[AgentRole.WORKER])),
        )
        registry.register(
            AgentRole.PLANNER,
            FakeAgentProvider(capabilities=make_capabilities(supported_roles=[AgentRole.PLANNER])),
        )
        all_caps = await registry.probe_all()
        assert set(all_caps.keys()) == {AgentRole.WORKER, AgentRole.PLANNER}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Capability negotiation: fail-closed on missing capability
# ---------------------------------------------------------------------------


def test_require_capabilities_passes_when_all_met():
    require_capabilities(
        make_capabilities(),
        CapabilityRequirement(streaming=StreamingSupport.EVENTS, native_cancel=True),
    )  # must not raise


def test_require_capabilities_accepts_stronger_offered_level():
    require_capabilities(
        make_capabilities(streaming=StreamingSupport.PARTIAL_TOKENS),
        CapabilityRequirement(streaming=StreamingSupport.EVENTS),
    )  # PARTIAL_TOKENS satisfies a request for at least EVENTS


def test_require_capabilities_fails_closed_on_missing_streaming():
    with pytest.raises(ProviderCapabilityError):
        require_capabilities(
            make_capabilities(streaming=StreamingSupport.NONE),
            CapabilityRequirement(streaming=StreamingSupport.EVENTS),
        )


def test_require_capabilities_fails_closed_on_missing_role():
    with pytest.raises(ProviderCapabilityError):
        require_capabilities(
            make_capabilities(supported_roles=[AgentRole.PLANNER]),
            CapabilityRequirement(roles=frozenset({AgentRole.WORKER})),
        )


def test_require_capabilities_fails_closed_on_missing_native_cancel():
    with pytest.raises(ProviderCapabilityError):
        require_capabilities(
            make_capabilities(native_cancel=False),
            CapabilityRequirement(native_cancel=True),
        )


def test_require_capabilities_reports_every_violation_at_once():
    with pytest.raises(ProviderCapabilityError) as exc_info:
        require_capabilities(
            make_capabilities(streaming=StreamingSupport.NONE, native_cancel=False),
            CapabilityRequirement(streaming=StreamingSupport.EVENTS, native_cancel=True),
        )
    message = str(exc_info.value)
    assert "streaming" in message
    assert "native_cancel" in message


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def _event(invocation_id: str, sequence: int) -> AgentEvent:
    return AgentEvent(
        invocation_id=invocation_id,
        sequence=sequence,
        event_type=AgentEventType.TEXT_DELTA,
        occurred_at=_utc_now(),
    )


async def _to_list(async_iterable):
    return [item async for item in async_iterable]


async def _from_list(items):
    for item in items:
        yield item


def test_normalize_events_passes_through_increasing_sequence():
    async def scenario():
        events = [_event("inv-1", 0), _event("inv-1", 1), _event("inv-1", 2)]
        result = await _to_list(normalize_events(_from_list(events), invocation_id="inv-1"))
        assert [e.sequence for e in result] == [0, 1, 2]

    asyncio.run(scenario())


def test_normalize_events_skips_exact_duplicates():
    async def scenario():
        e0 = _event("inv-1", 0)
        events = [e0, e0, _event("inv-1", 1)]
        result = await _to_list(normalize_events(_from_list(events), invocation_id="inv-1"))
        assert [e.sequence for e in result] == [0, 1]

    asyncio.run(scenario())


def test_normalize_events_raises_on_out_of_order():
    async def scenario():
        events = [_event("inv-1", 0), _event("inv-1", 2), _event("inv-1", 1)]
        with pytest.raises(OutOfOrderEventError):
            await _to_list(normalize_events(_from_list(events), invocation_id="inv-1"))

    asyncio.run(scenario())


def test_normalize_events_raises_on_foreign_invocation_id():
    async def scenario():
        events = [_event("inv-1", 0), _event("inv-OTHER", 1)]
        with pytest.raises(OutOfOrderEventError):
            await _to_list(normalize_events(_from_list(events), invocation_id="inv-1"))

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Cancel: fail-closed without native_cancel
# ---------------------------------------------------------------------------


def test_cancel_invocation_fails_closed_without_native_cancel():
    async def scenario():
        capabilities = make_capabilities(native_cancel=False)
        provider = FakeAgentProvider(capabilities=capabilities)
        ref = ProviderInvocationRef(opaque_ref="inv-1", provider_id=provider.provider_id)
        with pytest.raises(ProviderCapabilityError):
            await cancel_invocation(provider, capabilities, ref, reason="test")

    asyncio.run(scenario())


def test_cancel_invocation_succeeds_with_native_cancel():
    async def scenario():
        capabilities = make_capabilities(native_cancel=True)
        provider = FakeAgentProvider(capabilities=capabilities)
        provider.queue_invocation(
            AgentRole.WORKER,
            ScriptedInvocation(
                events=[],
                result=AgentRunResult(
                    invocation_id="inv-1",
                    protocol_status=ProtocolStatus.SUCCEEDED,
                    started_at=_utc_now(),
                ),
            ),
        )
        session = await provider.start_session(
            StartSessionRequest(
                role=AgentRole.WORKER,
                role_profile_ref="p",
                role_profile_digest="sha256:" + "0" * 64,
                contract_digest="sha256:" + "0" * 64,
                context_snapshot_ref="c",
                context_snapshot_digest="sha256:" + "0" * 64,
                deadline=_utc_now(),
            )
        )
        from tests.contract.provider_conformance import _make_run_request

        invocation = await provider.start_invocation(session, _make_run_request())
        result = await cancel_invocation(provider, capabilities, invocation, reason="test")
        assert result.protocol_status is ProtocolStatus.CANCELLED

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Usage accumulation / budget enforcement
# ---------------------------------------------------------------------------


def test_accumulate_usage_sums_across_invocations():
    used = BudgetUsage()
    used = accumulate_usage(used, UsageRecord(turns=2, input_tokens=100, output_tokens=50))
    used = accumulate_usage(used, UsageRecord(turns=1, input_tokens=10, output_tokens=5))
    assert used.turns_used == 3
    assert used.tokens_used == 165


def test_accumulate_usage_counts_rework():
    used = BudgetUsage()
    used = accumulate_usage(used, UsageRecord(turns=1), rework=True)
    assert used.rework_used == 1


def test_check_budget_passes_within_ceiling():
    ceiling = BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=2)
    used = BudgetUsage(turns_used=5, rework_used=1)
    check_budget(used, ceiling)  # must not raise


def test_check_budget_raises_when_turns_exceeded():
    ceiling = BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=2)
    used = BudgetUsage(turns_used=11)
    with pytest.raises(BudgetExceededError):
        check_budget(used, ceiling)


def test_check_budget_raises_when_rework_exceeded():
    ceiling = BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=2)
    used = BudgetUsage(rework_used=3)
    with pytest.raises(BudgetExceededError):
        check_budget(used, ceiling)
