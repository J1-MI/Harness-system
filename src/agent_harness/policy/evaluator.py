"""The deterministic Policy Engine: requested capabilities -> PolicyDecision.

Priority order (architecture review section 3):

    Hard-coded safety invariants
    > administrator policy (PolicyCeiling)
    > deployment profile
    > user approval
    > repository suggestions
    > planner requests

This module implements the first two layers. "Deployment profile" and
"repository suggestions" are not modeled yet (no config-file loading in
this phase); "user approval" is ``policy/approvals.py``; "planner
requests" is ``TaskContract.requested_capabilities`` (Phase 1.1) — the
input to this evaluator, never its output.

Only ``evaluate_policy`` produces a ``PolicyDecision``. Nothing else in
the codebase may construct one and expect it to be trusted.
"""

from __future__ import annotations

from datetime import datetime

from agent_harness.domain.digests import compute_model_digest
from agent_harness.domain.enums import PolicyOutcome, SubjectType
from agent_harness.domain.models import PolicyDecision, PolicyGrants, RequestedCapabilities, ScopeRules
from agent_harness.policy.commands import intersect_command_ids, rejected_command_ids
from agent_harness.policy.models import PolicyCeiling
from agent_harness.policy.paths import intersect_scope

__all__ = ["evaluate_policy"]

_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64

# A hard-coded, code-level invariant — not overridable by PolicyCeiling
# config or by any Approval. Changing this list requires a code change,
# not a policy.yaml edit. Link-local cloud metadata endpoints are a
# well-known SSRF-to-credential-theft vector (section 8's credential
# exfiltration concerns).
_HARD_DENIED_NETWORK_DOMAINS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.azure.com",
        "fd00:ec2::254",
    }
)

_APPROVAL_GATED_CAPABILITIES = (
    "network_access",
    "package_install",
    "raw_shell",
    "mcp_tools",
    "external_systems",
    "database_targets",
)


def evaluate_policy(
    subject_type: SubjectType,
    subject_id: str,
    subject_digest: str,
    requested_capabilities: RequestedCapabilities,
    requested_scope: ScopeRules,
    *,
    ceiling: PolicyCeiling,
    evaluated_at: datetime,
) -> PolicyDecision:
    denials: list[str] = []

    hard_denied_domains = sorted(
        set(requested_capabilities.network_domains) & _HARD_DENIED_NETWORK_DOMAINS
    )
    if hard_denied_domains:
        denials.append(
            "HARD_DENY_NETWORK_METADATA_ENDPOINT:" + ",".join(hard_denied_domains)
        )

    if requested_capabilities.raw_shell and not ceiling.raw_shell_allowed:
        denials.append("CEILING_FORBIDS_RAW_SHELL")

    effective_scope = intersect_scope(requested_scope, ceiling.workspace_scope)

    granted_commands = intersect_command_ids(
        requested_capabilities.command_ids, ceiling.allowed_command_ids
    )
    forbidden_commands = rejected_command_ids(
        requested_capabilities.command_ids, ceiling.allowed_command_ids
    )
    if forbidden_commands:
        denials.append("CEILING_FORBIDS_COMMANDS:" + ",".join(forbidden_commands))

    requested_domains = set(requested_capabilities.network_domains)
    grantable_domains = requested_domains - _HARD_DENIED_NETWORK_DOMAINS
    granted_network_domains = sorted(grantable_domains & ceiling.allowed_network_domains)
    forbidden_domains = sorted(grantable_domains - ceiling.allowed_network_domains)
    if forbidden_domains:
        denials.append("CEILING_FORBIDS_NETWORK_DOMAINS:" + ",".join(forbidden_domains))

    granted_mcp_tools = sorted(set(requested_capabilities.mcp_tools) & ceiling.allowed_mcp_tools)
    forbidden_mcp = sorted(set(requested_capabilities.mcp_tools) - ceiling.allowed_mcp_tools)
    if forbidden_mcp:
        denials.append("CEILING_FORBIDS_MCP_TOOLS:" + ",".join(forbidden_mcp))

    granted_external = sorted(
        set(requested_capabilities.external_systems) & ceiling.allowed_external_systems
    )
    forbidden_external = sorted(
        set(requested_capabilities.external_systems) - ceiling.allowed_external_systems
    )
    if forbidden_external:
        denials.append("CEILING_FORBIDS_EXTERNAL_SYSTEMS:" + ",".join(forbidden_external))

    granted_database_targets = sorted(
        set(requested_capabilities.database_targets) & ceiling.allowed_database_targets
    )
    forbidden_db = sorted(
        set(requested_capabilities.database_targets) - ceiling.allowed_database_targets
    )
    if forbidden_db:
        denials.append("CEILING_FORBIDS_DATABASE_TARGETS:" + ",".join(forbidden_db))

    package_install_granted = (
        requested_capabilities.package_install and ceiling.package_install_allowed
    )
    if requested_capabilities.package_install and not ceiling.package_install_allowed:
        denials.append("CEILING_FORBIDS_PACKAGE_INSTALL")

    grants = PolicyGrants(
        path_rules=effective_scope,
        command_ids=granted_commands,
        workspace_access=requested_capabilities.workspace_access,
        network_rules=granted_network_domains,
        package_rules=["package_install"] if package_install_granted else [],
        mcp_tools=granted_mcp_tools,
        external_targets=granted_external,
        sandbox_profile=ceiling.sandbox_profile,
        budgets=ceiling.budget_ceiling,
    )

    approval_requirements: list[str] = []
    reason_codes: list[str] = []

    if denials:
        outcome = PolicyOutcome.DENY
        reason_codes = denials
    else:
        active_flags = {
            "network_access": bool(granted_network_domains),
            "package_install": package_install_granted,
            "raw_shell": requested_capabilities.raw_shell,
            "mcp_tools": bool(granted_mcp_tools),
            "external_systems": bool(granted_external),
            "database_targets": bool(granted_database_targets),
        }
        approval_requirements = [
            name for name in _APPROVAL_GATED_CAPABILITIES if active_flags[name]
        ]
        outcome = (
            PolicyOutcome.REQUIRE_APPROVAL if approval_requirements else PolicyOutcome.ALLOW
        )

    decision = PolicyDecision(
        subject_type=subject_type,
        subject_id=subject_id,
        subject_digest=subject_digest,
        policy_bundle_id=ceiling.policy_bundle_id,
        policy_bundle_digest=ceiling.policy_bundle_digest,
        engine_version=ceiling.engine_version,
        evaluated_at=evaluated_at,
        outcome=outcome,
        grants=grants,
        denials=denials,
        approval_requirements=approval_requirements,
        reason_codes=reason_codes,
        integrity_digest=_PLACEHOLDER_DIGEST,
    )
    real_digest = compute_model_digest(decision, exclude_fields={"integrity_digest"})
    return decision.model_copy(update={"integrity_digest": real_digest})
