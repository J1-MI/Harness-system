"""Usage aggregation and budget-ceiling enforcement.

Ties Phase 1.1's ``Run.budget_used``/``budget_limits`` and Phase 4's
``PolicyGrants.budgets`` to an actual check: sum ``UsageRecord``s across
invocations and refuse to let a Run proceed past its ceiling. Provider-
reported usage is a client-side estimate, not authoritative billing (see
``domain.models.UsageRecord.is_estimate`` and the Claude Agent SDK cost-
tracking caveat referenced in the architecture review) — ceilings here
are a safety margin, not a precise accounting system.
"""

from __future__ import annotations

from agent_harness.domain.models import BudgetRequest, BudgetUsage, UsageRecord

__all__ = ["BudgetExceededError", "accumulate_usage", "check_budget"]


class BudgetExceededError(RuntimeError):
    pass


def accumulate_usage(existing: BudgetUsage, usage: UsageRecord, *, rework: bool = False) -> BudgetUsage:
    """Return a new ``BudgetUsage`` with ``usage`` folded in.

    Never mutates ``existing`` — callers persist the returned value
    (typically as part of the same atomic Run update that Phase 2.1's
    ``apply_transition`` already performs).
    """

    return BudgetUsage(
        turns_used=existing.turns_used + (usage.turns or 0),
        tokens_used=existing.tokens_used + (usage.input_tokens or 0) + (usage.output_tokens or 0),
        cost_used_usd=existing.cost_used_usd + (usage.estimated_cost_usd or 0.0),
        rework_used=existing.rework_used + (1 if rework else 0),
    )


def check_budget(used: BudgetUsage, ceiling: BudgetRequest) -> None:
    """Raise ``BudgetExceededError`` if ``used`` has exceeded ``ceiling``.

    Only checks the axes ``BudgetRequest`` actually bounds — turns and
    rework iterations. Token/cost totals are tracked in ``BudgetUsage``
    but ``BudgetRequest`` has no token/cost ceiling field (Phase 1.1 chose
    turns/timeout/rework as the hard ceilings; token/cost are informational
    until a future phase adds an explicit cost ceiling).
    """

    violations: list[str] = []
    if used.turns_used > ceiling.max_turns:
        violations.append(f"turns_used={used.turns_used} > max_turns={ceiling.max_turns}")
    if used.rework_used > ceiling.max_rework_iterations:
        violations.append(
            f"rework_used={used.rework_used} > "
            f"max_rework_iterations={ceiling.max_rework_iterations}"
        )
    if violations:
        raise BudgetExceededError("; ".join(violations))
