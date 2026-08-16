"""Deterministic replay of a previously recorded provider session.

Built on top of ``FakeAgentProvider`` rather than reimplementing Protocol
logic a second time: a "recording" is just JSON in the same shape as
``ProviderCapabilities``/``AgentEvent``/``AgentRunResult``, loaded and fed
into ``queue_invocation()`` calls. Useful for replaying an actual
historical Codex/Claude session without hitting the network, once real
adapters exist (Phase 6+) and can produce such a recording.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_harness.domain.enums import AgentRole
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import AgentEvent, AgentRunResult, ProviderCapabilities

__all__ = ["RecordingFormatError", "load_recording", "build_replay_provider"]


class RecordingFormatError(ValueError):
    pass


def load_recording(source: Path | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    path = Path(source)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordingFormatError(f"{path}: not valid JSON") from exc


def build_replay_provider(
    recording: dict[str, Any],
    *,
    provider_id: str | None = None,
    provider_version: str | None = None,
) -> FakeAgentProvider:
    """Build a ``FakeAgentProvider`` pre-loaded from a recorded session.

    Expected ``recording`` shape::

        {
          "provider_id": str,
          "provider_version": str,
          "capabilities": {...ProviderCapabilities fields...},
          "invocations": {
            "<ROLE>": [{"events": [...AgentEvent...], "result": {...AgentRunResult...}}, ...]
          }
        }

    Field shapes match the models' own JSON schemas directly, so a real
    recorder can dump those models as-is.
    """

    if "capabilities" not in recording:
        raise RecordingFormatError("recording missing 'capabilities'")
    capabilities = ProviderCapabilities.model_validate(recording["capabilities"])

    provider = FakeAgentProvider(
        capabilities=capabilities,
        provider_id=provider_id or recording.get("provider_id", "replay-provider"),
        provider_version=provider_version or recording.get("provider_version", "0.0.0-replay"),
    )

    for role_name, scripted_invocations in recording.get("invocations", {}).items():
        try:
            role = AgentRole(role_name)
        except ValueError as exc:
            raise RecordingFormatError(f"unknown role {role_name!r} in recording") from exc
        for entry in scripted_invocations:
            events = [AgentEvent.model_validate(e) for e in entry["events"]]
            result = AgentRunResult.model_validate(entry["result"])
            provider.queue_invocation(role, ScriptedInvocation(events=events, result=result))

    return provider
