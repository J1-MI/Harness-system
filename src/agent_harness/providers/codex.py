"""Codex Planner Adapter (Phase 7): the PLANNER role, backed by the real
``openai_codex`` package ("Python SDK for Codex" — the app-server-backed
SDK the architecture review recommends over hand-rolled CLI JSONL parsing).

Lockdown mirrors ``providers/claude.py``'s approach, adapted to what this
SDK actually exposes (introspected from the installed 0.144.4 package, not
guessed):

- ``sandbox=Sandbox.read_only`` on every thread — the Planner analyzes and
  proposes a ``TaskContract``; it never needs write access, so the
  RoleProfile does not request it (review: "read-only RoleProfile").
- ``approval_mode=ApprovalMode.deny_all`` — even if the model attempts a
  tool call that would need approval, it is auto-denied rather than
  auto-approved or left pending. Combined with ``read_only`` sandbox this
  is the "no-write test" guarantee: nothing this adapter does can mutate
  the filesystem.
- A fresh thread per Planner invocation (``thread_start``, not
  ``thread_resume``) by default — "fresh plan session" per the review,
  so a Planner run is never biased by a prior Worker/Verifier
  conversation. ``resume_session`` is still implemented (thread_resume
  exists and is a legitimate Protocol operation), just not the default
  path callers are expected to take for planning.

Structured output reuses Phase 1.1's schema kernel directly: when
``AgentRunRequest.output_schema_id`` names one of our own exported
contract models (``task_contract``, ``verification_result``, ...), the
adapter validates the Planner's final message against that exact Pydantic
model — no separate JSON Schema validator was written for this phase.
"""

from __future__ import annotations

import asyncio
import json
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Awaitable, Callable, Protocol

from openai_codex import ApprovalMode, Sandbox
from openai_codex.models import (
    AccountRateLimitsUpdatedNotification,
    AgentMessageDeltaNotification,
    ErrorNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    ThreadStartedNotification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStartedNotification,
)

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import (
    AgentEventType,
    AgentRole,
    DriverKind,
    McpControlSupport,
    ProtocolStatus,
    ProviderErrorCode,
    SessionResumeSupport,
    StreamingSupport,
    StructuredOutputSupport,
    UsageReportingSupport,
)
from agent_harness.domain.models import ProviderError, UsageRecord
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
from agent_harness.schema_export import EXPORTED_MODELS

__all__ = ["CodexClientLike", "CodexThreadLike", "CodexTurnHandleLike", "CodexPlannerAdapter"]

_SCHEMA_ID_TO_MODEL = {filename.removesuffix(".json"): model for filename, model in EXPORTED_MODELS}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sdk_version() -> str:
    try:
        return version("openai-codex")
    except PackageNotFoundError:  # pragma: no cover - dev environment only
        return "unknown"


def _safe_dump(obj: Any) -> dict:
    """Dump an ``openai_codex`` SDK object to a plain dict for our own
    event payloads.

    Codex review M-05: the installed ``openai_codex`` package's generated
    ``Turn`` model declares ``items_view: TurnItemsView | None = "full"``
    — its own field *default* is the raw string ``"full"`` instead of the
    ``TurnItemsView.full`` enum member (an upstream generator bug, not
    something this codebase constructs). Pydantic's serializer correctly
    flags that mismatch with a ``UserWarning`` on every ``model_dump`` of
    an SDK-default ``Turn`` — but the value it actually serializes is
    still correct (the raw string already equals the enum's own
    ``.value``), so this is a narrowly-scoped, documented suppression of
    that one known-harmless upstream warning, not a blanket one: any
    other ``UserWarning`` raised here still surfaces normally.
    """

    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message="Pydantic serializer warnings.*", category=UserWarning,
                )
                return obj.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    if isinstance(obj, dict):
        return obj
    return {"value": repr(obj)}


def _validate_structured_output(output_schema_id: str, text: str) -> dict | None:
    """Validate ``text`` against ``output_schema_id``.

    Returns the parsed dict if valid, or ``None`` if it doesn't parse or
    doesn't validate — the caller treats ``None`` as a retry trigger.
    Recognized schema IDs (our own Phase 1.1 exported contracts) are
    validated with the exact Pydantic model, not a generic JSON Schema
    validator; unrecognized IDs fall back to "is it valid JSON at all".
    """

    model_cls = _SCHEMA_ID_TO_MODEL.get(output_schema_id)
    if model_cls is None:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
    try:
        instance = model_cls.model_validate_json(text)
    except Exception:  # noqa: BLE001 - any validation failure triggers a retry
        return None
    return instance.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Injectable SDK client seam
# ---------------------------------------------------------------------------


class CodexTurnHandleLike(Protocol):
    id: str

    def stream(self): ...

    async def interrupt(self) -> Any: ...


class CodexThreadLike(Protocol):
    id: str

    async def turn(self, input: Any, **kwargs: Any) -> CodexTurnHandleLike: ...


class CodexClientLike(Protocol):
    async def thread_start(self, **kwargs: Any) -> CodexThreadLike: ...

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> CodexThreadLike: ...

    async def login_api_key(self, api_key: str) -> None: ...

    async def close(self) -> None: ...


def _default_client_factory() -> CodexClientLike:
    from openai_codex import AsyncCodex, CodexConfig

    return AsyncCodex(CodexConfig())


# ---------------------------------------------------------------------------
# Notification -> AgentEvent normalization
# ---------------------------------------------------------------------------

_TOOL_ITEM_MARKERS = ("CommandExecution", "McpToolCall", "DynamicToolCall", "CollabAgentToolCall")


def _events_for_item(item: Any, *, completed: bool) -> list[tuple[AgentEventType, dict]]:
    root = getattr(item, "root", item)
    kind = type(root).__name__
    payload = _safe_dump(root)

    if any(marker in kind for marker in _TOOL_ITEM_MARKERS):
        event_type = AgentEventType.TOOL_COMPLETED if completed else AgentEventType.TOOL_REQUESTED
        return [(event_type, {"item_kind": kind, **payload})]
    if "FileChange" in kind:
        return [(AgentEventType.FILE_CHANGE_REPORTED, {"item_kind": kind, **payload})]
    if "AgentMessage" in kind and completed:
        return [(AgentEventType.MESSAGE_COMPLETED, {"item_kind": kind, "block_type": "text", **payload})]
    if "Reasoning" in kind and completed:
        return [(AgentEventType.MESSAGE_COMPLETED, {"item_kind": kind, "block_type": "reasoning", **payload})]
    return []


def _events_for_notification(notification: Any) -> list[tuple[AgentEventType, dict]]:
    payload = notification.payload

    if isinstance(payload, ThreadStartedNotification):
        return [(AgentEventType.SESSION_STARTED, _safe_dump(payload.thread))]
    if isinstance(payload, TurnStartedNotification):
        return [(AgentEventType.TURN_STARTED, _safe_dump(payload.turn))]
    if isinstance(payload, ItemStartedNotification):
        return _events_for_item(payload.item, completed=False)
    if isinstance(payload, ItemCompletedNotification):
        return _events_for_item(payload.item, completed=True)
    if isinstance(payload, AgentMessageDeltaNotification):
        return [(AgentEventType.TEXT_DELTA, {"delta": payload.delta, "item_id": payload.item_id})]
    if isinstance(payload, TurnCompletedNotification):
        events: list[tuple[AgentEventType, dict]] = []
        events.append(
            (
                AgentEventType.TURN_COMPLETED,
                {
                    "status": payload.turn.status.value,
                    "error": _safe_dump(payload.turn.error) if payload.turn.error else None,
                },
            )
        )
        return events
    if isinstance(payload, ErrorNotification):
        return [(AgentEventType.ERROR, _safe_dump(payload.error))]
    if isinstance(payload, ThreadTokenUsageUpdatedNotification):
        return [(AgentEventType.USAGE_UPDATED, _safe_dump(payload))]
    if isinstance(payload, AccountRateLimitsUpdatedNotification):
        return [(AgentEventType.RATE_LIMITED, _safe_dump(payload))]
    return []


def _final_message_text(turn: Any) -> str | None:
    """The last ``agentMessage`` item's text, if any — the Planner's answer."""

    text: str | None = None
    for item in turn.items:
        root = getattr(item, "root", item)
        if type(root).__name__ == "AgentMessageThreadItem":
            text = root.text
    return text


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _SessionState:
    role: AgentRole
    thread_id: str | None = None
    thread: CodexThreadLike | None = None
    resume_from: str | None = None


@dataclass
class _InvocationState:
    session_opaque_ref: str
    started_at: datetime
    events: list[AgentEvent] = field(default_factory=list)
    condition: Any = None
    done: bool = False
    result: AgentRunResult | None = None
    active_turn: CodexTurnHandleLike | None = None
    cancel_requested: bool = False


class CodexPlannerAdapter:
    """``AgentProvider`` for the PLANNER role, backed by ``openai_codex``.

    ``resolve_prompt`` decouples this adapter from Artifact-store
    specifics, matching ``ClaudeAgentAdapter``'s seam. ``max_schema_retries``
    bounds the "invalid schema retry limit" behavior: when
    ``output_schema_id`` names a recognized contract and the model's final
    message fails to validate against it, the adapter retries the same
    turn (same input, same thread) up to this many additional times before
    giving up with ``ProtocolStatus.INVALID_OUTPUT``.
    """

    def __init__(
        self,
        *,
        resolve_prompt: Callable[[str], Awaitable[str]],
        model: str | None = None,
        max_schema_retries: int = 2,
        client_factory: Callable[[], CodexClientLike] = _default_client_factory,
    ) -> None:
        self._resolve_prompt = resolve_prompt
        self._model = model
        self._max_schema_retries = max_schema_retries
        self._client = client_factory()
        self._sdk_version = _sdk_version()
        self._sessions: dict[str, _SessionState] = {}
        self._invocations: dict[str, _InvocationState] = {}

    @property
    def provider_id(self) -> str:
        return "openai-codex-sdk"

    @property
    def provider_version(self) -> str:
        return self._sdk_version

    async def health_check(self) -> ProviderHealth:
        has_credentials = bool(os.environ.get("OPENAI_API_KEY")) or bool(
            os.environ.get("CODEX_API_KEY")
        )
        return ProviderHealth(
            healthy=has_credentials,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            checked_at=_utc_now(),
            detail=None if has_credentials else "no OPENAI_API_KEY or CODEX_API_KEY in environment",
        )

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_roles=[AgentRole.PLANNER, AgentRole.VERIFIER],
            structured_output=StructuredOutputSupport.JSON_SCHEMA,
            streaming=StreamingSupport.EVENTS,
            session_resume=SessionResumeSupport.PROCESS_LOCAL,
            session_fork=True,
            native_cancel=True,
            tool_approval_callbacks=False,
            tool_visibility_control=False,
            mcp_control=McpControlSupport.NONE,
            usage_reporting=UsageReportingSupport.TOKENS,
            sandbox_modes=["read_only", "workspace_write", "full_access"],
            network_controls=[],
            max_context=None,
            driver_kind=DriverKind.SDK,
            driver_version=self._sdk_version,
            capability_probe_timestamp=_utc_now(),
        )

    async def start_session(self, request: StartSessionRequest) -> ProviderSessionRef:
        if request.role not in (AgentRole.PLANNER, AgentRole.VERIFIER):
            raise ProviderCapabilityError(
                f"CodexPlannerAdapter only serves PLANNER/VERIFIER, got {request.role!r}"
            )
        opaque_ref = f"codex-session-{new_id()}"
        self._sessions[opaque_ref] = _SessionState(role=request.role)
        return ProviderSessionRef(opaque_ref=opaque_ref, provider_id=self.provider_id, role=request.role)

    async def resume_session(self, request: ResumeSessionRequest) -> ProviderSessionRef:
        prior = self._sessions.get(request.prior_session.opaque_ref)
        if prior is None or prior.thread_id is None:
            raise ProviderCapabilityError(
                f"unknown or not-yet-started session {request.prior_session.opaque_ref!r}"
            )
        opaque_ref = f"codex-session-{new_id()}"
        self._sessions[opaque_ref] = _SessionState(role=prior.role, resume_from=prior.thread_id)
        return ProviderSessionRef(opaque_ref=opaque_ref, provider_id=self.provider_id, role=prior.role)

    async def start_invocation(
        self, session: ProviderSessionRef, request: AgentRunRequest
    ) -> ProviderInvocationRef:
        session_state = self._sessions.get(session.opaque_ref)
        if session_state is None:
            raise ProviderCapabilityError(f"unknown session {session.opaque_ref!r}")

        if session_state.thread is None:
            if session_state.resume_from is not None:
                session_state.thread = await self._client.thread_resume(
                    session_state.resume_from,
                    approval_mode=ApprovalMode.deny_all,
                    sandbox=Sandbox.read_only,
                    model=self._model,
                )
            else:
                session_state.thread = await self._client.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    sandbox=Sandbox.read_only,
                    model=self._model,
                )
            session_state.thread_id = session_state.thread.id

        prompt = await self._resolve_prompt(request.prompt_payload_artifact_ref)

        model_cls = _SCHEMA_ID_TO_MODEL.get(request.output_schema_id)
        output_schema = model_cls.model_json_schema() if model_cls is not None else None
        # Codex is guided by output_schema natively; we still validate the
        # result against the exact Pydantic model afterward (defense in
        # depth — a model matching JSON Schema loosely doesn't guarantee
        # it survives our stricter semantic validators).
        first_turn_handle = await session_state.thread.turn(prompt, output_schema=output_schema)

        invocation_opaque = f"codex-invocation-{new_id()}"
        invocation_state = _InvocationState(
            session_opaque_ref=session.opaque_ref,
            started_at=_utc_now(),
            condition=asyncio.Condition(),
            # Set synchronously, before the background task is even
            # scheduled, so a cancel() called immediately after
            # start_invocation() returns never races an empty active_turn.
            active_turn=first_turn_handle,
        )
        self._invocations[invocation_opaque] = invocation_state
        asyncio.create_task(
            self._run_with_retries(
                invocation_opaque,
                session_state,
                request,
                prompt,
                output_schema,
                first_turn_handle,
                invocation_state,
            )
        )
        return ProviderInvocationRef(opaque_ref=invocation_opaque, provider_id=self.provider_id)

    async def _run_with_retries(
        self,
        invocation_id: str,
        session_state: _SessionState,
        request: AgentRunRequest,
        prompt: str,
        output_schema: dict | None,
        turn_handle: CodexTurnHandleLike,
        state: _InvocationState,
    ) -> None:
        sequence = 0
        attempt = 0
        final_turn = None
        final_error: Exception | None = None
        latest_usage = None

        while True:
            state.active_turn = turn_handle
            try:
                async for notification in turn_handle.stream():
                    for event_type, payload in _events_for_notification(notification):
                        event = AgentEvent(
                            invocation_id=invocation_id,
                            sequence=sequence,
                            event_type=event_type,
                            occurred_at=_utc_now(),
                            payload=payload,
                        )
                        sequence += 1
                        async with state.condition:
                            state.events.append(event)
                            state.condition.notify_all()
                    if isinstance(notification.payload, ThreadTokenUsageUpdatedNotification):
                        latest_usage = notification.payload.token_usage
                    if isinstance(notification.payload, TurnCompletedNotification):
                        final_turn = notification.payload.turn
            except Exception as exc:  # noqa: BLE001
                final_error = exc
                break

            if final_turn is None or state.cancel_requested:
                break
            if final_turn.status.value != "completed":
                break

            text = _final_message_text(final_turn)
            if not request.output_schema_id or text is None:
                break  # no structured output requested/produced: accept as-is
            validated = _validate_structured_output(request.output_schema_id, text)
            if validated is not None:
                break  # valid: accept

            attempt += 1
            if attempt > self._max_schema_retries:
                break  # exhausted retries: accept the (invalid) final_turn, will map to INVALID_OUTPUT

            warning_event = AgentEvent(
                invocation_id=invocation_id,
                sequence=sequence,
                event_type=AgentEventType.WARNING,
                occurred_at=_utc_now(),
                payload={
                    "reason": "structured_output_validation_failed",
                    "attempt": attempt,
                    "max_retries": self._max_schema_retries,
                },
            )
            sequence += 1
            async with state.condition:
                state.events.append(warning_event)
                state.condition.notify_all()
            final_turn = None  # retry: loop again with a fresh turn on the same thread
            turn_handle = await session_state.thread.turn(prompt, output_schema=output_schema)

        state.result = self._build_result(
            invocation_id, request, final_turn, final_error, latest_usage, started_at=state.started_at
        )
        async with state.condition:
            state.done = True
            state.condition.notify_all()

    def _build_result(
        self,
        invocation_id: str,
        request: AgentRunRequest,
        turn: Any,
        error: Exception | None,
        token_usage: Any,
        *,
        started_at: datetime,
    ) -> AgentRunResult:
        if error is not None:
            return AgentRunResult(
                invocation_id=invocation_id,
                protocol_status=ProtocolStatus.PROVIDER_ERROR,
                provider_error=ProviderError(
                    code=ProviderErrorCode.INTERNAL, retriable=False, message=str(error)
                ),
                started_at=started_at,
                completed_at=_utc_now(),
            )
        if turn is None:
            return AgentRunResult(
                invocation_id=invocation_id,
                protocol_status=ProtocolStatus.PROVIDER_ERROR,
                provider_error=ProviderError(
                    code=ProviderErrorCode.INTERNAL, retriable=True, message="turn never completed"
                ),
                started_at=started_at,
                completed_at=_utc_now(),
            )

        usage = None
        if token_usage is not None:
            usage = UsageRecord(
                input_tokens=token_usage.total.input_tokens,
                output_tokens=token_usage.total.output_tokens,
                is_estimate=True,
            )

        status_value = turn.status.value
        text = _final_message_text(turn)
        structured_output = (
            _validate_structured_output(request.output_schema_id, text)
            if request.output_schema_id and text is not None
            else None
        )

        if status_value == "interrupted":
            protocol_status = ProtocolStatus.CANCELLED
        elif status_value == "failed":
            protocol_status = ProtocolStatus.PROVIDER_ERROR
        elif request.output_schema_id and text is not None and structured_output is None:
            protocol_status = ProtocolStatus.INVALID_OUTPUT
        else:
            protocol_status = ProtocolStatus.SUCCEEDED

        provider_error = None
        if protocol_status is ProtocolStatus.PROVIDER_ERROR:
            provider_error = ProviderError(
                code=ProviderErrorCode.INTERNAL,
                retriable=False,
                message=(turn.error.message if turn.error else "turn failed"),
            )

        return AgentRunResult(
            invocation_id=invocation_id,
            protocol_status=protocol_status,
            structured_output=structured_output,
            usage=usage,
            provider_error=provider_error,
            started_at=started_at,
            completed_at=_utc_now(),
        )

    async def stream_events(self, invocation: ProviderInvocationRef, *, after_cursor: str | None = None):
        state = self._invocations.get(invocation.opaque_ref)
        if state is None:
            raise ProviderCapabilityError(f"unknown invocation {invocation.opaque_ref!r}")
        after_sequence = int(after_cursor) if after_cursor is not None else -1
        index = 0
        while True:
            async with state.condition:
                while index >= len(state.events) and not state.done:
                    await state.condition.wait()
                pending = list(state.events[index:])
                index = len(state.events)
                finished = state.done and index >= len(state.events)
            for event in pending:
                if event.sequence > after_sequence:
                    yield event
            if finished:
                return

    async def await_result(self, invocation: ProviderInvocationRef) -> AgentRunResult:
        state = self._invocations.get(invocation.opaque_ref)
        if state is None:
            raise ProviderCapabilityError(f"unknown invocation {invocation.opaque_ref!r}")
        async with state.condition:
            while not state.done:
                await state.condition.wait()
        assert state.result is not None
        return state.result

    async def cancel(self, invocation: ProviderInvocationRef, request: CancelRequest) -> CancelResult:
        state = self._invocations.get(invocation.opaque_ref)
        if state is None:
            raise ProviderCapabilityError(f"unknown invocation {invocation.opaque_ref!r}")
        state.cancel_requested = True
        if state.active_turn is not None:
            await state.active_turn.interrupt()

        async with state.condition:
            while not state.done:
                await state.condition.wait()

        cancelled_at = _utc_now()
        if state.result is not None and state.result.protocol_status is not ProtocolStatus.CANCELLED:
            state.result = state.result.model_copy(update={"protocol_status": ProtocolStatus.CANCELLED})

        return CancelResult(
            invocation_id=invocation.opaque_ref,
            protocol_status=ProtocolStatus.CANCELLED,
            cancelled_at=cancelled_at,
            detail=request.reason,
        )

    async def close_session(self, session: ProviderSessionRef) -> None:
        self._sessions.pop(session.opaque_ref, None)
