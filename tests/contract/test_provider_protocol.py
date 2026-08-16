"""Protocol-shape tests: conformance, capability model, result invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_harness.domain.enums import (
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
from agent_harness.domain.models import ProviderError
from agent_harness.providers.fake import FakeAgentProvider
from agent_harness.providers.protocol import AgentProvider, AgentRunResult, ProviderCapabilities


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


def test_fake_provider_satisfies_agent_provider_protocol():
    provider = FakeAgentProvider(capabilities=make_capabilities())
    assert isinstance(provider, AgentProvider)


def test_provider_capabilities_requires_at_least_one_supported_role():
    with pytest.raises(ValidationError):
        make_capabilities(supported_roles=[])


def test_provider_capabilities_rejects_extra_fields():
    payload = make_capabilities().model_dump(mode="json")
    payload["undeclared"] = True
    with pytest.raises(ValidationError):
        ProviderCapabilities.model_validate(payload)


def test_agent_run_result_requires_provider_error_when_status_is_provider_error():
    with pytest.raises(ValidationError):
        AgentRunResult(
            invocation_id="inv-1",
            protocol_status=ProtocolStatus.PROVIDER_ERROR,
            started_at=_utc_now(),
            provider_error=None,
        )


def test_agent_run_result_accepts_provider_error_when_present():
    result = AgentRunResult(
        invocation_id="inv-1",
        protocol_status=ProtocolStatus.PROVIDER_ERROR,
        started_at=_utc_now(),
        provider_error=ProviderError(
            code=ProviderErrorCode.TIMEOUT,
            retriable=True,
            message="provider timed out",
        ),
    )
    assert result.provider_error.code is ProviderErrorCode.TIMEOUT


def test_agent_run_result_rejects_completed_before_started():
    with pytest.raises(ValidationError):
        AgentRunResult(
            invocation_id="inv-1",
            protocol_status=ProtocolStatus.SUCCEEDED,
            started_at=_utc_now(),
            completed_at=_utc_now() - timedelta(seconds=10),
        )


def test_agent_run_result_is_immutable():
    result = AgentRunResult(
        invocation_id="inv-1",
        protocol_status=ProtocolStatus.SUCCEEDED,
        started_at=_utc_now(),
    )
    with pytest.raises(ValidationError):
        result.protocol_status = ProtocolStatus.CANCELLED
