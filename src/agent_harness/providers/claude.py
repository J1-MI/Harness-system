"""Claude Provider Adapter (Phase 6): the Worker role, backed by the real
``claude_agent_sdk`` (the Python SDK for Claude Code / Claude Agent SDK —
*not* the plain ``anthropic`` Messages API SDK; the architecture review is
explicit that the Worker needs session/tool/permission management, not a
single completion call).

Everything the SDK's own ``ClaudeAgentOptions`` offers for lockdown is used:

- ``setting_sources=[]`` — no auto-loading of project ``CLAUDE.md``,
  hooks, or user/project settings (the ``--bare`` equivalent the review
  calls out).
- ``strict_mcp_config=True`` with no ``mcp_servers`` — the Worker never
  connects to a project's own ``.mcp.json`` or any MCP server this phase
  didn't explicitly wire up (MCP governance is Phase 11).
- Tool control is **layered**, per the review's own correction: "Claude의
  allowed_tools는 도구 가시성 제한이 아니라 자동 승인 규칙이다." Visibility
  is restricted via ``tools`` (bare allowlist); auto-approval within that
  set via ``allowed_tools`` + ``permission_mode="dontAsk"``; and a
  ``can_use_tool`` callback re-checks every call against
  ``AgentRunRequest.allowed_tool_ids`` at runtime as a third, independent
  gate — even a bug in the first two layers can't grant a tool this
  callback denies.

What this phase does **not** claim: real OS sandboxing. The SDK's own
Bash/file tools run wherever this process runs — see Phase 3.2's
``trusted_local`` disclaimer, which applies unchanged here. Nothing in
this adapter should be read as adding isolation the execution plane
doesn't already have.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
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
from agent_harness.domain.models import ProviderError, ScopeRules, UsageRecord
from agent_harness.execution.scope_guard import path_matches_glob
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

__all__ = ["SdkClientLike", "ClaudeAgentAdapter"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sdk_version() -> str:
    try:
        return version("claude-agent-sdk")
    except PackageNotFoundError:  # pragma: no cover - dev environment only
        return "unknown"


def _json_safe(value: Any) -> Any:
    """Best-effort coercion of arbitrary SDK payloads into JSON-safe data."""

    try:
        return json.loads(json.dumps(value))
    except TypeError:
        return json.loads(json.dumps(value, default=repr))


def _as_payload(value: Any) -> dict:
    safe = _json_safe(value)
    return safe if isinstance(safe, dict) else {"value": safe}


# ---------------------------------------------------------------------------
# Tool-call path guard (Codex review B-01)
#
# The tool-name allowlist in can_use_tool never inspected *where* a
# Read/Write/Edit/Glob/Grep call actually pointed — an absolute or
# ``..``-relative path let Claude touch any host file this OS process
# could reach, entirely independent of PolicyGrants.path_rules. Key names
# below (file_path/notebook_path/path) are Claude Code's documented
# built-in tool schemas; this SDK package itself does not expose a typed
# tool_input shape to import instead.
# ---------------------------------------------------------------------------

_FILE_PATH_TOOL_INPUT_KEYS: dict[str, str] = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "path",
    "Grep": "path",
}

# Read-only exploration within the worktree is never scope-restricted (a
# Worker often needs to read a file it isn't allowed to edit, e.g. a
# lockfile or config it must respect) — only write-capable tools are also
# checked against ScopeRules.allowed_path_rules/forbidden_path_rules,
# not just worktree containment.
_SCOPE_CHECKED_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def _check_tool_path(
    tool_name: str, tool_input: dict, *, workspace_root: Path, scope: ScopeRules
) -> str | None:
    """A denial message if ``tool_input``'s path argument escapes the
    worktree or (for write-capable tools) the granted scope; ``None`` if
    the tool has no path-bearing argument this function recognizes, or
    the path checks out.
    """

    key = _FILE_PATH_TOOL_INPUT_KEYS.get(tool_name)
    if key is None:
        return None  # not a file-path-bearing tool this function knows about
    raw_path = tool_input.get(key)
    if not isinstance(raw_path, str) or not raw_path:
        return None  # tool call has no explicit path (e.g. defaults to cwd)

    resolved_root = workspace_root.resolve()
    try:
        candidate = Path(raw_path)
        resolved = (candidate if candidate.is_absolute() else workspace_root / candidate).resolve()
    except (TypeError, ValueError, OSError):
        return f"{tool_name} path {raw_path!r} could not be resolved"

    if resolved != resolved_root and resolved_root not in resolved.parents:
        return f"{tool_name} path {raw_path!r} resolves outside the workspace ({resolved})"

    if tool_name in _SCOPE_CHECKED_TOOLS:
        relative = resolved.relative_to(resolved_root).as_posix()
        if relative:  # empty string means the path IS the workspace root itself
            if any(path_matches_glob(relative, pattern) for pattern in scope.forbidden_path_rules):
                return f"{tool_name} path {raw_path!r} matches a forbidden_path_rules pattern"
            if not any(path_matches_glob(relative, pattern) for pattern in scope.allowed_path_rules):
                return f"{tool_name} path {raw_path!r} does not match any allowed_path_rules pattern"

    return None


# ---------------------------------------------------------------------------
# Injectable SDK client seam — real ClaudeSDKClient in production, a fake
# feeding real SDK dataclass instances in tests ("keyless replay tests").
# ---------------------------------------------------------------------------


class SdkClientLike(Protocol):
    async def connect(self, prompt: Any = None) -> None: ...

    async def query(self, prompt: Any, session_id: str = "default") -> None: ...

    def receive_response(self): ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


def _default_client_factory(options: ClaudeAgentOptions) -> SdkClientLike:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


async def _single_user_message_stream(prompt: str, session_id: str):
    """Wrap a plain prompt string into the SDK's streaming input shape.

    Mirrors exactly what ``ClaudeSDKClient.query()`` does internally for a
    bare string prompt — but the SDK only takes that shortcut when
    ``can_use_tool`` is unset. Since this adapter always sets
    ``can_use_tool`` (it's the independent runtime tool-gate), both
    ``connect()`` and ``query()`` must always receive an async iterable.
    """

    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Message -> AgentEvent normalization
# ---------------------------------------------------------------------------


def _content_block_events(blocks: list) -> list[tuple[AgentEventType, dict]]:
    events: list[tuple[AgentEventType, dict]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            events.append(
                (AgentEventType.MESSAGE_COMPLETED, {"block_type": "text", "text": block.text})
            )
        elif isinstance(block, ThinkingBlock):
            events.append(
                (AgentEventType.MESSAGE_COMPLETED, {"block_type": "thinking", "text": block.thinking})
            )
        elif isinstance(block, ToolUseBlock):
            events.append(
                (
                    AgentEventType.TOOL_REQUESTED,
                    {"tool_use_id": block.id, "tool_name": block.name, "input": _json_safe(block.input)},
                )
            )
        elif isinstance(block, ToolResultBlock):
            events.append(
                (
                    AgentEventType.TOOL_COMPLETED,
                    {
                        "tool_use_id": block.tool_use_id,
                        "is_error": bool(block.is_error),
                        "content": _json_safe(block.content),
                    },
                )
            )
        elif isinstance(block, ServerToolUseBlock):
            events.append(
                (
                    AgentEventType.TOOL_REQUESTED,
                    {"server": True, **_as_payload(_json_safe(getattr(block, "__dict__", {})))},
                )
            )
        elif isinstance(block, ServerToolResultBlock):
            events.append(
                (
                    AgentEventType.TOOL_COMPLETED,
                    {"server": True, **_as_payload(_json_safe(getattr(block, "__dict__", {})))},
                )
            )
    return events


def _events_for_message(message: Any) -> list[tuple[AgentEventType, dict]]:
    """Everything mapped from one raw SDK message, in emission order.

    Message types the SDK can yield but this adapter does not normalize
    (e.g. ``ConversationResetMessage``) yield no events rather than
    raising — an unrecognized message must not crash the drain loop.
    """

    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            return [(AgentEventType.SESSION_STARTED, {"subtype": message.subtype, **_as_payload(message.data)})]
        return [(AgentEventType.WARNING, {"subtype": message.subtype, **_as_payload(message.data)})]

    if isinstance(message, AssistantMessage):
        events: list[tuple[AgentEventType, dict]] = [
            (AgentEventType.TURN_STARTED, {"model": message.model})
        ]
        events.extend(_content_block_events(message.content))
        if message.error:
            events.append((AgentEventType.ERROR, {"error": message.error}))
        return events

    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            return [
                (AgentEventType.MESSAGE_COMPLETED, {"block_type": "text", "role": "user", "text": message.content})
            ]
        return _content_block_events(message.content)

    if isinstance(message, ResultMessage):
        return [
            (
                AgentEventType.USAGE_UPDATED,
                {
                    "num_turns": message.num_turns,
                    "total_cost_usd": message.total_cost_usd,
                    "usage": _as_payload(message.usage),
                },
            ),
            (
                AgentEventType.TURN_COMPLETED,
                {"is_error": message.is_error, "stop_reason": message.stop_reason, "result": message.result},
            ),
        ]

    if isinstance(message, RateLimitEvent):
        info = message.rate_limit_info
        return [
            (
                AgentEventType.RATE_LIMITED,
                _as_payload(_json_safe(getattr(info, "__dict__", str(info)))),
            )
        ]

    if isinstance(message, StreamEvent):
        return [(AgentEventType.TEXT_DELTA, _as_payload(message.event))]

    return []


_STOP_REASON_STATUS_HINTS: tuple[tuple[str, ProtocolStatus], ...] = (
    ("schema", ProtocolStatus.INVALID_OUTPUT),
    ("invalid_output", ProtocolStatus.INVALID_OUTPUT),
    ("output_invalid", ProtocolStatus.INVALID_OUTPUT),
    ("budget", ProtocolStatus.LIMIT_REACHED),
    ("max_turns", ProtocolStatus.LIMIT_REACHED),
    ("limit_reached", ProtocolStatus.LIMIT_REACHED),
    ("timeout", ProtocolStatus.TIMED_OUT),
)

_ERROR_CODE_HINTS: tuple[tuple[str, ProviderErrorCode], ...] = (
    ("auth", ProviderErrorCode.AUTHENTICATION),
    ("rate_limit", ProviderErrorCode.RATE_LIMIT),
    ("invalid_request", ProviderErrorCode.INVALID_REQUEST),
    ("budget", ProviderErrorCode.BUDGET_EXHAUSTED),
    ("timeout", ProviderErrorCode.TIMEOUT),
)


def _classify_protocol_status(message: ResultMessage) -> ProtocolStatus:
    if not message.is_error:
        return ProtocolStatus.SUCCEEDED
    reason = (message.stop_reason or "").lower()
    for needle, status in _STOP_REASON_STATUS_HINTS:
        if needle in reason:
            return status
    return ProtocolStatus.PROVIDER_ERROR


def _classify_error_code(message: ResultMessage) -> ProviderErrorCode:
    reason = (message.stop_reason or "").lower()
    for needle, code in _ERROR_CODE_HINTS:
        if needle in reason:
            return code
    return ProviderErrorCode.INTERNAL


def _build_result(
    invocation_id: str, message: ResultMessage, *, started_at: datetime
) -> AgentRunResult:
    usage_dict = message.usage or {}
    usage = UsageRecord(
        turns=message.num_turns,
        input_tokens=usage_dict.get("input_tokens"),
        output_tokens=usage_dict.get("output_tokens"),
        estimated_cost_usd=message.total_cost_usd,
        is_estimate=True,
    )

    protocol_status = _classify_protocol_status(message)
    provider_error: ProviderError | None = None
    if protocol_status is ProtocolStatus.PROVIDER_ERROR:
        code = _classify_error_code(message)
        provider_error = ProviderError(
            code=code,
            retriable=code
            in (ProviderErrorCode.RATE_LIMIT, ProviderErrorCode.TRANSIENT_NETWORK, ProviderErrorCode.INTERNAL),
            message="; ".join(message.errors) if message.errors else (message.result or "unknown error"),
            provider_original_code=str(message.api_error_status) if message.api_error_status else None,
        )

    structured_output = message.structured_output if isinstance(message.structured_output, dict) else None

    return AgentRunResult(
        invocation_id=invocation_id,
        protocol_status=protocol_status,
        structured_output=structured_output,
        provider_session_ref=message.session_id,
        usage=usage,
        provider_error=provider_error,
        started_at=started_at,
        completed_at=_utc_now(),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _SessionState:
    role: AgentRole
    claude_session_id: str
    resume_from: str | None = None
    sdk_client: SdkClientLike | None = None


@dataclass
class _InvocationState:
    session_opaque_ref: str
    started_at: datetime
    events: list[AgentEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False
    result: AgentRunResult | None = None
    drain_task: "asyncio.Task | None" = None


class ClaudeAgentAdapter:
    """``AgentProvider`` for the WORKER role, backed by ``claude_agent_sdk``.

    ``resolve_prompt``/``resolve_workspace_handle`` decouple this adapter
    from persistence specifics (Artifact refs, workspace lease lookups) —
    the caller resolves an ``AgentRunRequest``'s opaque refs into concrete
    values; this adapter never reaches into the blob store or workspace
    registry itself.
    """

    def __init__(
        self,
        *,
        resolve_prompt: Callable[[str], Awaitable[str]],
        resolve_workspace_handle: Callable[[str], str],
        model: str = "claude-opus-4-8",
        client_factory: Callable[[ClaudeAgentOptions], SdkClientLike] = _default_client_factory,
    ) -> None:
        self._resolve_prompt = resolve_prompt
        self._resolve_workspace_handle = resolve_workspace_handle
        self._model = model
        self._client_factory = client_factory
        self._sdk_version = _sdk_version()
        self._sessions: dict[str, _SessionState] = {}
        self._invocations: dict[str, _InvocationState] = {}

    @property
    def provider_id(self) -> str:
        return "claude-agent-sdk"

    @property
    def provider_version(self) -> str:
        return self._sdk_version

    async def health_check(self) -> ProviderHealth:
        has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
        return ProviderHealth(
            healthy=has_credentials,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            checked_at=_utc_now(),
            detail=None
            if has_credentials
            else "no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in environment",
        )

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_roles=[AgentRole.WORKER],
            structured_output=StructuredOutputSupport.JSON_SCHEMA,
            streaming=StreamingSupport.EVENTS,
            session_resume=SessionResumeSupport.PROCESS_LOCAL,
            session_fork=True,
            native_cancel=True,
            tool_approval_callbacks=True,
            tool_visibility_control=True,
            mcp_control=McpControlSupport.STRICT,
            usage_reporting=UsageReportingSupport.ESTIMATED_COST,
            sandbox_modes=["trusted_local"],
            network_controls=[],
            max_context=None,
            driver_kind=DriverKind.SDK,
            driver_version=self._sdk_version,
            capability_probe_timestamp=_utc_now(),
        )

    async def start_session(self, request: StartSessionRequest) -> ProviderSessionRef:
        if request.role is not AgentRole.WORKER:
            raise ProviderCapabilityError(
                f"ClaudeAgentAdapter only serves role WORKER, got {request.role!r}"
            )
        opaque_ref = f"claude-session-{new_id()}"
        self._sessions[opaque_ref] = _SessionState(
            role=request.role, claude_session_id=str(new_id())
        )
        return ProviderSessionRef(opaque_ref=opaque_ref, provider_id=self.provider_id, role=request.role)

    async def resume_session(self, request: ResumeSessionRequest) -> ProviderSessionRef:
        prior = self._sessions.get(request.prior_session.opaque_ref)
        if prior is None:
            raise ProviderCapabilityError(f"unknown session {request.prior_session.opaque_ref!r}")
        opaque_ref = f"claude-session-{new_id()}"
        self._sessions[opaque_ref] = _SessionState(
            role=prior.role,
            claude_session_id=prior.claude_session_id,
            resume_from=prior.claude_session_id,
        )
        return ProviderSessionRef(opaque_ref=opaque_ref, provider_id=self.provider_id, role=prior.role)

    def _make_can_use_tool(self, request: AgentRunRequest, *, workspace_root: Path):
        allowed = set(request.allowed_tool_ids)
        scope = request.effective_policy_grants.path_rules

        async def can_use_tool(tool_name: str, tool_input: dict, context: Any):
            if tool_name not in allowed:
                return PermissionResultDeny(
                    message=f"tool {tool_name!r} is not in this invocation's effective_policy_grants",
                    interrupt=False,
                )
            violation = _check_tool_path(tool_name, tool_input, workspace_root=workspace_root, scope=scope)
            if violation is not None:
                return PermissionResultDeny(message=violation, interrupt=False)
            return PermissionResultAllow()

        return can_use_tool

    def _build_options(
        self, request: AgentRunRequest, session: _SessionState, *, cwd: str
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            tools=list(request.allowed_tool_ids),
            allowed_tools=list(request.allowed_tool_ids),
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            resume=session.resume_from,
            session_id=session.claude_session_id,
            max_turns=request.max_turns,
            max_budget_usd=request.max_cost_usd,
            model=self._model,
            cwd=cwd,
            setting_sources=[],
            can_use_tool=self._make_can_use_tool(request, workspace_root=Path(cwd)),
        )

    async def start_invocation(
        self, session: ProviderSessionRef, request: AgentRunRequest
    ) -> ProviderInvocationRef:
        session_state = self._sessions.get(session.opaque_ref)
        if session_state is None:
            raise ProviderCapabilityError(f"unknown session {session.opaque_ref!r}")

        prompt = await self._resolve_prompt(request.prompt_payload_artifact_ref)
        cwd = self._resolve_workspace_handle(request.workspace_handle)
        options = self._build_options(request, session_state, cwd=cwd)

        # The SDK requires streaming-mode (AsyncIterable) prompts whenever
        # can_use_tool is set — a plain string raises ValueError at
        # connect()/query() time. We always set can_use_tool (it's the
        # independent runtime enforcement layer), so always stream.
        prompt_stream = _single_user_message_stream(prompt, session_state.claude_session_id)
        if session_state.sdk_client is None:
            session_state.sdk_client = self._client_factory(options)
            await session_state.sdk_client.connect(prompt_stream)
        else:
            await session_state.sdk_client.query(prompt_stream, session_id=session_state.claude_session_id)

        invocation_opaque = f"claude-invocation-{new_id()}"
        invocation_state = _InvocationState(
            session_opaque_ref=session.opaque_ref, started_at=_utc_now()
        )
        self._invocations[invocation_opaque] = invocation_state
        invocation_state.drain_task = asyncio.create_task(
            self._drain(invocation_opaque, session_state.sdk_client, invocation_state)
        )
        return ProviderInvocationRef(opaque_ref=invocation_opaque, provider_id=self.provider_id)

    async def _drain(
        self, invocation_id: str, sdk_client: SdkClientLike, state: _InvocationState
    ) -> None:
        sequence = 0
        try:
            async for message in sdk_client.receive_response():
                for event_type, payload in _events_for_message(message):
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
                if isinstance(message, ResultMessage):
                    state.result = _build_result(invocation_id, message, started_at=state.started_at)
        except Exception as exc:  # noqa: BLE001 - surfaced as a provider error result
            state.result = AgentRunResult(
                invocation_id=invocation_id,
                protocol_status=ProtocolStatus.PROVIDER_ERROR,
                provider_error=ProviderError(
                    code=ProviderErrorCode.INTERNAL, retriable=False, message=str(exc)
                ),
                started_at=state.started_at,
                completed_at=_utc_now(),
            )
        finally:
            async with state.condition:
                state.done = True
                state.condition.notify_all()

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

    async def cancel(
        self, invocation: ProviderInvocationRef, request: CancelRequest
    ) -> CancelResult:
        state = self._invocations.get(invocation.opaque_ref)
        if state is None:
            raise ProviderCapabilityError(f"unknown invocation {invocation.opaque_ref!r}")
        session_state = self._sessions.get(state.session_opaque_ref)
        if session_state is not None and session_state.sdk_client is not None:
            await session_state.sdk_client.interrupt()

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
        state = self._sessions.pop(session.opaque_ref, None)
        if state is not None and state.sdk_client is not None:
            await state.sdk_client.disconnect()
