"""Role -> Provider lookup, with capability probes cached per registration.

One physical provider process/adapter can serve multiple roles (e.g. the
same Codex adapter for both PLANNER and VERIFIER, per architecture review
M-02: role is data, not a separate Protocol) — the registry maps
``AgentRole`` to whichever ``AgentProvider`` instance is configured to
handle it, which may or may not be the same object across roles.
"""

from __future__ import annotations

from agent_harness.domain.enums import AgentRole
from agent_harness.providers.protocol import AgentProvider, ProviderCapabilities

__all__ = ["ProviderNotRegisteredError", "ProviderRegistry"]


class ProviderNotRegisteredError(LookupError):
    pass


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[AgentRole, AgentProvider] = {}
        self._capability_cache: dict[AgentRole, ProviderCapabilities] = {}

    def register(self, role: AgentRole, provider: AgentProvider) -> None:
        self._providers[role] = provider
        self._capability_cache.pop(role, None)

    def get(self, role: AgentRole) -> AgentProvider:
        try:
            return self._providers[role]
        except KeyError:
            raise ProviderNotRegisteredError(role) from None

    def registered_roles(self) -> frozenset[AgentRole]:
        return frozenset(self._providers.keys())

    async def probe(self, role: AgentRole) -> ProviderCapabilities:
        """Probe (and cache) a role's provider capabilities.

        Cached per registration, not per call — call ``register`` again
        (even with the same provider) to force a fresh probe, e.g. after a
        provider process restart that might have changed its driver
        version.
        """

        if role not in self._capability_cache:
            provider = self.get(role)
            self._capability_cache[role] = await provider.capabilities()
        return self._capability_cache[role]

    async def probe_all(self) -> dict[AgentRole, ProviderCapabilities]:
        return {role: await self.probe(role) for role in self._providers}
