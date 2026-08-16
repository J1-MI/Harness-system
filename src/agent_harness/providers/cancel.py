"""Cancel orchestration: capability-checked, always resolves to a terminal status.

Architecture review section 5: "먼저 Provider native cancel/interrupt를
시도한다... 짧은 grace period 후 provider host process를 종료한다." This
module owns only the negotiation step (never silently no-op if the
provider lacks ``native_cancel``); actually killing a hung provider host
process belongs to a Provider Host supervisor, which does not exist yet
(no real Provider adapters until Phase 6+).
"""

from __future__ import annotations

from agent_harness.providers.capabilities import CapabilityRequirement, require_capabilities
from agent_harness.providers.protocol import (
    AgentProvider,
    CancelRequest,
    CancelResult,
    ProviderCapabilities,
    ProviderInvocationRef,
)

__all__ = ["cancel_invocation"]


async def cancel_invocation(
    provider: AgentProvider,
    capabilities: ProviderCapabilities,
    invocation: ProviderInvocationRef,
    *,
    reason: str,
    force: bool = False,
) -> CancelResult:
    """Cancel ``invocation``, refusing to silently no-op on an uncancellable provider.

    Raises ``ProviderCapabilityError`` (fail-closed) if ``capabilities.
    native_cancel`` is false — callers must not proceed as if cancellation
    succeeded when the provider never actually offered it.
    """

    require_capabilities(capabilities, CapabilityRequirement(native_cancel=True))
    request = CancelRequest(invocation_id=invocation.opaque_ref, reason=reason, force=force)
    return await provider.cancel(invocation, request)
