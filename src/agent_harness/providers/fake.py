"""Deterministic, network-free ``AgentProvider`` implementation.

Exists to let ``domain``/``application`` code exercise the Protocol
end-to-end without a real Codex/Claude SDK. It is intentionally a thin
conformance double, not a simulator: callers script every event and
result up front via ``queue_invocation`` and get back exactly that,
deterministically, with no real sleeping.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_harness.domain.enums import AgentRole, ProtocolStatus, SessionResumeSupport
from agent_harness.providers.protocol import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    CancelRequest,
    CancelResult,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderHealth,
    ProviderInvocationRef,
    ProviderSessionRef,
    ResumeSessionRequest,
    StartSessionRequest,
)


@dataclass
class ScriptedInvocation:
    """One pre-recorded provider turn used to drive the Fake Provider.

    Not a wire contract — purely test/dev scaffolding, so it is a plain
    dataclass rather than a Pydantic model.
    """

    events: list[AgentEvent]
    result: AgentRunResult


class _FakeInvocationState:
    __slots__ = ("script", "cancelled", "cancel_result")

    def __init__(self, script: ScriptedInvocation) -> None:
        self.script = script
        self.cancelled = False
        self.cancel_result: CancelResult | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeAgentProvider:
    """Minimal, deterministic Protocol-conformance double.

    Each invocation gets an isolated event sequence: ``queue_invocation``
    appends to a per-role FIFO, and ``start_invocation`` pops exactly one
    entry, so two concurrent invocations never see each other's events.
    """

    def __init__(
        self,
        *,
        capabilities: ProviderCapabilities,
        provider_id: str = "fake-provider",
        provider_version: str = "0.1.0",
    ) -> None:
        self._provider_id = provider_id
        self._provider_version = provider_version
        self._capabilities = capabilities
        self._session_counter = itertools.count(1)
        self._invocation_counter = itertools.count(1)
        self._sessions: dict[str, ProviderSessionRef] = {}
        self._pending_scripts: dict[AgentRole, list[ScriptedInvocation]] = {}
        self._invocations: dict[str, _FakeInvocationState] = {}

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def provider_version(self) -> str:
        return self._provider_version

    def queue_invocation(self, role: AgentRole, script: ScriptedInvocation) -> None:
        """Register the next scripted turn for ``role``, FIFO per role."""

        self._pending_scripts.setdefault(role, []).append(script)

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            healthy=True,
            provider_id=self._provider_id,
            provider_version=self._provider_version,
            checked_at=_utc_now(),
        )

    async def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def start_session(
        self, request: StartSessionRequest
    ) -> ProviderSessionRef:
        if request.role not in self._capabilities.supported_roles:
            raise ProviderCapabilityError(
                f"role {request.role!r} is not in supported_roles"
            )
        session_ref = ProviderSessionRef(
            opaque_ref=f"fake-session-{next(self._session_counter)}",
            provider_id=self._provider_id,
            role=request.role,
        )
        self._sessions[session_ref.opaque_ref] = session_ref
        return session_ref

    async def resume_session(
        self, request: ResumeSessionRequest
    ) -> ProviderSessionRef:
        if self._capabilities.session_resume is SessionResumeSupport.NONE:
            raise ProviderCapabilityError(
                "resume_session called but session_resume capability is NONE"
            )
        prior_ref = request.prior_session.opaque_ref
        if prior_ref not in self._sessions:
            raise ProviderCapabilityError(f"unknown session {prior_ref!r}")
        return self._sessions[prior_ref]

    async def start_invocation(
        self, session: ProviderSessionRef, request: AgentRunRequest
    ) -> ProviderInvocationRef:
        queue = self._pending_scripts.get(session.role, [])
        if not queue:
            raise ProviderCapabilityError(
                f"no scripted invocation queued for role {session.role!r}; "
                "call queue_invocation() first"
            )
        script = queue.pop(0)
        invocation_ref = ProviderInvocationRef(
            opaque_ref=f"fake-invocation-{next(self._invocation_counter)}",
            provider_id=self._provider_id,
        )
        self._invocations[invocation_ref.opaque_ref] = _FakeInvocationState(script)
        return invocation_ref

    async def stream_events(
        self,
        invocation: ProviderInvocationRef,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = self._require_invocation(invocation)
        after_sequence = int(after_cursor) if after_cursor is not None else -1
        for event in state.script.events:
            if event.sequence > after_sequence:
                yield event

    async def await_result(
        self, invocation: ProviderInvocationRef
    ) -> AgentRunResult:
        state = self._require_invocation(invocation)
        if state.cancelled:
            cancelled_at = (
                state.cancel_result.cancelled_at
                if state.cancel_result is not None
                else _utc_now()
            )
            return AgentRunResult(
                invocation_id=state.script.result.invocation_id,
                protocol_status=ProtocolStatus.CANCELLED,
                started_at=state.script.result.started_at,
                completed_at=cancelled_at,
            )
        return state.script.result

    async def cancel(
        self, invocation: ProviderInvocationRef, request: CancelRequest
    ) -> CancelResult:
        if not self._capabilities.native_cancel:
            raise ProviderCapabilityError(
                "cancel called but native_cancel capability is False"
            )
        state = self._require_invocation(invocation)
        state.cancelled = True
        result = CancelResult(
            invocation_id=request.invocation_id,
            protocol_status=ProtocolStatus.CANCELLED,
            cancelled_at=_utc_now(),
            detail=request.reason,
        )
        state.cancel_result = result
        return result

    async def close_session(self, session: ProviderSessionRef) -> None:
        self._sessions.pop(session.opaque_ref, None)

    def _require_invocation(
        self, invocation: ProviderInvocationRef
    ) -> _FakeInvocationState:
        state = self._invocations.get(invocation.opaque_ref)
        if state is None:
            raise ProviderCapabilityError(
                f"unknown invocation {invocation.opaque_ref!r}"
            )
        return state
