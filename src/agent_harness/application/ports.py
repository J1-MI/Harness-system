"""Application-layer ports: what application code depends on, not how it's built.

Per the dependency rule in architecture review section 3
("application → policy/provider/execution/persistence ports;
infrastructure implementations → application ports"), application code
should type against these Protocols rather than concrete infrastructure
classes. ``providers.registry.ProviderRegistry`` satisfies
``ProviderRegistryPort`` structurally (no inheritance needed — Python
Protocols are structural).
"""

from __future__ import annotations

from typing import Protocol

from agent_harness.domain.enums import AgentRole
from agent_harness.providers.protocol import AgentProvider, ProviderCapabilities

__all__ = ["ProviderRegistryPort"]


class ProviderRegistryPort(Protocol):
    def get(self, role: AgentRole) -> AgentProvider: ...

    async def probe(self, role: AgentRole) -> ProviderCapabilities: ...
