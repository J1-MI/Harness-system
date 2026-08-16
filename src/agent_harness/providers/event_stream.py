"""Event stream normalization: monotonic sequence, duplicates, reordering.

Architecture review security control #15 ("schema downgrade·event
replay"): "duplicate/out-of-order event 탐지" -> "Adapter quarantine,
invocation 실패". A well-behaved provider emits ``AgentEvent.sequence``
strictly increasing per invocation; anything else is either a bug in the
adapter or a sign of the stream being tampered with, and either way must
not be silently absorbed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_harness.providers.protocol import AgentEvent

__all__ = ["OutOfOrderEventError", "normalize_events"]


class OutOfOrderEventError(RuntimeError):
    """A later event's sequence did not strictly increase from the last one seen."""


async def normalize_events(
    events: AsyncIterator[AgentEvent], *, invocation_id: str
) -> AsyncIterator[AgentEvent]:
    """Re-yield ``events``, enforcing strictly increasing sequence numbers.

    Exact-duplicate events (same sequence, e.g. a benign resumed-stream
    resend of an already-delivered event) are silently skipped —
    idempotent replay of something already processed is not a protocol
    violation. A sequence going *backwards* or repeating a *different*
    event at an already-seen sequence number is treated as a hard error:
    it means either the adapter is broken or the stream was tampered
    with, and either way the invocation must not proceed on unreliable
    event ordering.
    """

    last_sequence: int | None = None
    async for event in events:
        if event.invocation_id != invocation_id:
            raise OutOfOrderEventError(
                f"event for invocation {event.invocation_id!r} received while "
                f"normalizing stream for {invocation_id!r}"
            )
        if last_sequence is not None:
            if event.sequence == last_sequence:
                continue  # exact duplicate of the last event — skip, not fatal
            if event.sequence <= last_sequence:
                raise OutOfOrderEventError(
                    f"invocation {invocation_id}: event sequence {event.sequence} "
                    f"is not greater than the last seen sequence {last_sequence}"
                )
        last_sequence = event.sequence
        yield event
