"""Command-ID grant resolution: only what is both requested and ceiling-allowed."""

from __future__ import annotations

__all__ = ["intersect_command_ids", "rejected_command_ids"]


def intersect_command_ids(
    requested: list[str], allowed_command_ids: frozenset[str]
) -> list[str]:
    """The command IDs actually grantable: requested ∩ admin allowlist."""

    return sorted(set(requested) & allowed_command_ids)


def rejected_command_ids(
    requested: list[str], allowed_command_ids: frozenset[str]
) -> list[str]:
    """Requested command IDs the admin ceiling does not permit at all.

    Kept separate from ``intersect_command_ids`` so callers can surface a
    clear reason (rather than a silent narrowing) when a Planner asks for
    a command_id that was never registered/approved.
    """

    return sorted(set(requested) - allowed_command_ids)
