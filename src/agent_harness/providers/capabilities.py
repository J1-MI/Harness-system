"""Capability negotiation: fail closed, never silently downgrade.

Architecture review section 7: "Capability는 정적 설정만 믿지 않고 실제
설치 버전에 대해 probe한다. 요구 capability가 없으면 downgrade하지 말고
CAPABILITY_MISMATCH로 fail closed한다." ``require_capabilities`` is the
one place that check happens — callers state what they need, this module
either says nothing (capability met) or raises (capability missing), and
never returns a "best effort" partial match.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.domain.enums import (
    AgentRole,
    McpControlSupport,
    SessionResumeSupport,
    StreamingSupport,
    StructuredOutputSupport,
    UsageReportingSupport,
)
from agent_harness.providers.protocol import ProviderCapabilities, ProviderCapabilityError

__all__ = ["CapabilityRequirement", "require_capabilities"]

# Ordered supports: a provider offering a "stronger" level satisfies a
# requirement for a "weaker" one. Providers never need to advertise every
# individual level a caller might ask for, just their actual ceiling.
_STREAMING_RANK = {
    StreamingSupport.NONE: 0,
    StreamingSupport.EVENTS: 1,
    StreamingSupport.PARTIAL_TOKENS: 2,
}
_RESUME_RANK = {
    SessionResumeSupport.NONE: 0,
    SessionResumeSupport.PROCESS_LOCAL: 1,
    SessionResumeSupport.DURABLE: 2,
}
_STRUCTURED_OUTPUT_RANK = {
    StructuredOutputSupport.NONE: 0,
    StructuredOutputSupport.JSON_SCHEMA: 1,
}
_MCP_CONTROL_RANK = {
    McpControlSupport.NONE: 0,
    McpControlSupport.EXPLICIT: 1,
    McpControlSupport.STRICT: 2,
}
_USAGE_REPORTING_RANK = {
    UsageReportingSupport.NONE: 0,
    UsageReportingSupport.TOKENS: 1,
    UsageReportingSupport.ESTIMATED_COST: 2,
}


@dataclass(frozen=True)
class CapabilityRequirement:
    """Everything left ``None`` means "no requirement on this axis"."""

    roles: frozenset[AgentRole] | None = None
    structured_output: StructuredOutputSupport | None = None
    streaming: StreamingSupport | None = None
    session_resume: SessionResumeSupport | None = None
    session_fork: bool | None = None
    native_cancel: bool | None = None
    tool_approval_callbacks: bool | None = None
    tool_visibility_control: bool | None = None
    mcp_control: McpControlSupport | None = None
    usage_reporting: UsageReportingSupport | None = None


def require_capabilities(
    capabilities: ProviderCapabilities, requirement: CapabilityRequirement
) -> None:
    """Raise ``ProviderCapabilityError`` listing every unmet requirement.

    All axes are checked (not just the first failure) so a caller sees
    the whole gap in one error instead of discovering it one capability
    at a time.
    """

    violations: list[str] = []

    if requirement.roles is not None:
        missing_roles = requirement.roles - set(capabilities.supported_roles)
        if missing_roles:
            violations.append(f"unsupported roles: {sorted(missing_roles)}")

    if (
        requirement.structured_output is not None
        and _STRUCTURED_OUTPUT_RANK[capabilities.structured_output]
        < _STRUCTURED_OUTPUT_RANK[requirement.structured_output]
    ):
        violations.append(
            f"structured_output {capabilities.structured_output} < required "
            f"{requirement.structured_output}"
        )

    if (
        requirement.streaming is not None
        and _STREAMING_RANK[capabilities.streaming] < _STREAMING_RANK[requirement.streaming]
    ):
        violations.append(
            f"streaming {capabilities.streaming} < required {requirement.streaming}"
        )

    if (
        requirement.session_resume is not None
        and _RESUME_RANK[capabilities.session_resume] < _RESUME_RANK[requirement.session_resume]
    ):
        violations.append(
            f"session_resume {capabilities.session_resume} < required "
            f"{requirement.session_resume}"
        )

    if (
        requirement.mcp_control is not None
        and _MCP_CONTROL_RANK[capabilities.mcp_control] < _MCP_CONTROL_RANK[requirement.mcp_control]
    ):
        violations.append(
            f"mcp_control {capabilities.mcp_control} < required {requirement.mcp_control}"
        )

    if (
        requirement.usage_reporting is not None
        and _USAGE_REPORTING_RANK[capabilities.usage_reporting]
        < _USAGE_REPORTING_RANK[requirement.usage_reporting]
    ):
        violations.append(
            f"usage_reporting {capabilities.usage_reporting} < required "
            f"{requirement.usage_reporting}"
        )

    for flag_name in (
        "session_fork",
        "native_cancel",
        "tool_approval_callbacks",
        "tool_visibility_control",
    ):
        required_value = getattr(requirement, flag_name)
        if required_value is True and not getattr(capabilities, flag_name):
            violations.append(f"{flag_name} is required but not offered")

    if violations:
        raise ProviderCapabilityError(
            f"CAPABILITY_MISMATCH for provider {capabilities.driver_kind}/"
            f"{capabilities.driver_version}: {'; '.join(violations)}"
        )
