"""Runs the shared Protocol conformance suite against a replay-loaded provider.

Builds a recording the way a real recorder would: construct the actual
Pydantic models, then ``model_dump(mode="json")`` them into the recording
shape ``providers.replay`` expects.
"""

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
from agent_harness.providers.protocol import AgentEvent, AgentRunResult, ProviderCapabilities
from agent_harness.providers.replay import RecordingFormatError, build_replay_provider, load_recording

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
        driver_version="0.0.0-replay",
        capability_probe_timestamp=_utc_now(),
    )
    data.update(overrides)
    return ProviderCapabilities(**data)


def _make_recording(invocation_count: int = 3) -> dict:
    invocations = []
    for i in range(invocation_count):
        invocation_id = f"recorded-inv-{i}"
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
        invocations.append(
            {
                "events": [e.model_dump(mode="json") for e in events],
                "result": result.model_dump(mode="json"),
            }
        )
    return {
        "provider_id": "recorded-codex-session",
        "provider_version": "1.2.3",
        "capabilities": _make_capabilities().model_dump(mode="json"),
        "invocations": {"WORKER": invocations},
    }


def test_replay_provider_satisfies_conformance_suite():
    async def scenario():
        recording = _make_recording()
        provider = build_replay_provider(recording)
        await run_conformance_suite(provider)

    asyncio.run(scenario())


def test_replay_provider_preserves_provider_identity_from_recording():
    recording = _make_recording()
    provider = build_replay_provider(recording)
    assert provider.provider_id == "recorded-codex-session"
    assert provider.provider_version == "1.2.3"


def test_load_recording_from_json_file(tmp_path):
    import json

    recording = _make_recording(invocation_count=1)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(recording), encoding="utf-8")

    loaded = load_recording(path)
    assert loaded["provider_id"] == "recorded-codex-session"


def test_build_replay_provider_rejects_missing_capabilities():
    with pytest.raises(RecordingFormatError):
        build_replay_provider({"invocations": {}})


def test_build_replay_provider_rejects_unknown_role():
    recording = _make_recording()
    recording["invocations"]["NOT_A_REAL_ROLE"] = recording["invocations"].pop("WORKER")
    with pytest.raises(RecordingFormatError):
        build_replay_provider(recording)
