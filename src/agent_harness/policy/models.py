"""Admin-owned policy ceiling — the upper bound nothing may exceed.

``config``(loading files) is explicitly not this module's job — this is
already-validated, in-memory configuration. Loading ``policy.yaml`` from
disk is out of scope for Phase 4; callers construct a ``PolicyCeiling``
directly (or via test factories) for now.
"""

from __future__ import annotations

from agent_harness.domain.digests import Digest
from agent_harness.domain.models import BudgetRequest, HarnessModel, ScopeRules

__all__ = ["PolicyCeiling"]


class PolicyCeiling(HarnessModel):
    """The administrator policy layer from section 3's priority order:

    ``Hard-coded safety invariants > administrator policy > deployment
    profile > user approval > repository suggestions > planner requests``.

    Nothing computed by ``policy/evaluator.py`` may ever exceed this,
    regardless of what is requested or later approved.
    """

    policy_bundle_id: str
    policy_bundle_digest: Digest
    engine_version: str

    workspace_scope: ScopeRules
    allowed_command_ids: frozenset[str]
    raw_shell_allowed: bool
    allowed_network_domains: frozenset[str]
    package_install_allowed: bool
    allowed_mcp_tools: frozenset[str]
    allowed_external_systems: frozenset[str]
    allowed_database_targets: frozenset[str]
    sandbox_profile: str
    budget_ceiling: BudgetRequest
