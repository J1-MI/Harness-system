"""Reusable AgentProvider Protocol conformance suite (Phase 5).

Not itself a ``test_*.py`` file — pytest would try to collect
``run_conformance_suite`` as a test otherwise, since it starts with
``test``-adjacent semantics but isn't parametrized as one. Concrete
provider tests (``test_fake_provider_conformance.py``,
``test_replay_provider_conformance.py``) import and call
``run_conformance_suite`` against their own provider instance, proving
every conforming ``AgentProvider`` implementation satisfies the same
contract — not just whichever one a hand-written test happened to cover.

Callers must pre-queue at least 3 scripted invocations for
``AgentRole.WORKER`` before calling this (via ``FakeAgentProvider.
queue_invocation`` directly, or by loading a recording with ``providers.
replay.build_replay_provider``) — the suite consumes three: one for the
main happy-path exercise, one for an isolation check, one for cancel.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_harness.domain.enums import AgentRole, ProtocolStatus, SessionResumeSupport
from agent_harness.domain.models import BudgetRequest, PolicyGrants
from agent_harness.providers.protocol import (
    AgentProvider,
    AgentRunRequest,
    CancelRequest,
    ResumeSessionRequest,
    StartSessionRequest,
)

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_run_request() -> AgentRunRequest:
    return AgentRunRequest(
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
        workspace_handle="workspace-handle",
        deadline=_utc_now(),
        prompt_payload_artifact_ref="artifact://prompt",
        correlation_id="corr-1",
        idempotency_key="idem-1",
    )


async def run_conformance_suite(provider: AgentProvider) -> None:
    # --- health_check / capabilities ---------------------------------
    health = await provider.health_check()
    assert health.provider_id == provider.provider_id

    capabilities = await provider.capabilities()
    assert AgentRole.WORKER in capabilities.supported_roles

    start_request = StartSessionRequest(
        role=AgentRole.WORKER,
        role_profile_ref="worker-profile",
        role_profile_digest=VALID_DIGEST,
        contract_digest=VALID_DIGEST,
        context_snapshot_ref="context-ref",
        context_snapshot_digest=VALID_DIGEST,
        deadline=_utc_now(),
    )

    # --- session start/resume -----------------------------------------
    session = await provider.start_session(start_request)
    assert session.role is AgentRole.WORKER
    assert session.provider_id == provider.provider_id

    if capabilities.session_resume is not SessionResumeSupport.NONE:
        resumed = await provider.resume_session(
            ResumeSessionRequest(prior_session=session, resume_reason="conformance check")
        )
        assert resumed.opaque_ref == session.opaque_ref

    # --- invocation: events are sequential, non-empty, well-formed ----
    invocation = await provider.start_invocation(session, _make_run_request())
    events = [event async for event in provider.stream_events(invocation)]
    assert events, "conformance suite requires at least one scripted event"
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert all(event.invocation_id for event in events)

    result = await provider.await_result(invocation)
    assert result.protocol_status in ProtocolStatus
    assert result.started_at is not None

    # --- invocation isolation: a second invocation has its own events -
    invocation_2 = await provider.start_invocation(session, _make_run_request())
    events_2 = [event async for event in provider.stream_events(invocation_2)]
    assert invocation_2.opaque_ref != invocation.opaque_ref
    if events and events_2:
        assert {e.event_id for e in events}.isdisjoint({e.event_id for e in events_2})

    # --- cancel: only exercised if the provider claims native_cancel --
    if capabilities.native_cancel:
        invocation_3 = await provider.start_invocation(session, _make_run_request())
        cancel_result = await provider.cancel(
            invocation_3, CancelRequest(invocation_id=invocation_3.opaque_ref, reason="conformance check")
        )
        assert cancel_result.protocol_status is ProtocolStatus.CANCELLED
        final = await provider.await_result(invocation_3)
        assert final.protocol_status is ProtocolStatus.CANCELLED

    # --- close_session must not raise ----------------------------------
    await provider.close_session(session)
