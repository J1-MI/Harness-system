"""State Machine Kernel — pure ``LifecycleState`` transitions (Phase 1.2).

Scope, per the architecture review roadmap (section 11, row "1.2 — State
Machine Kernel") and the state machine review (section 5):

- a transition table encoding exactly the allowed-next-state table from
  section 5
- terminal-state immutability (``READY_FOR_MERGE``/``FAILED``/``CANCELLED``
  accept no further transitions)
- a "pending action" concept for states where the Run is waiting on
  something external (approval, manual review, a recovery decision)
- linking a ``FailureRecord`` to any transition into ``FAILED``

Explicitly out of scope: no persistence, no journal writes, no process/
worktree/provider side effects. ``transition_run`` is a pure function — it
takes a ``Run`` and returns a *new* ``Run`` rather than mutating one or
touching any I/O. Wiring this into SQLite + journal atomicity is Phase 2.1.

Design note on "pending action": section 6's domain model table does not
list a ``pending_action`` field on ``Run``, and section 10's atomic-
transition pseudocode treats "pending action" as a separate row to
insert/update, not a Run column. So ``PendingAction`` here is a side
value returned alongside the updated ``Run`` (via ``TransitionOutcome``),
not a new field bolted onto the already-shipped, schema-exported ``Run``
model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, Field

from agent_harness.domain.enums import TERMINAL_LIFECYCLE_STATES, LifecycleState, PendingActionKind
from agent_harness.domain.models import FailureRecord, HarnessModel, Run

__all__ = [
    "TRANSITIONS",
    "PENDING_ACTION_REQUIRED_STATES",
    "RECOVERY_RESTART_TARGETS",
    "PendingAction",
    "TransitionOutcome",
    "IllegalTransition",
    "can_transition",
    "validate_transition",
    "transition_run",
]


class IllegalTransition(ValueError):
    """Raised when a requested ``LifecycleState`` transition is not allowed."""


# ---------------------------------------------------------------------------
# Transition table (section 5 "허용 전이" table, transcribed exactly)
# ---------------------------------------------------------------------------

# States RECOVERY_REQUIRED may resume into: the union of "immediately
# restartable" and "reconcile 후 restartable" from section 5. EXECUTING is
# deliberately excluded — a crash mid-EXECUTING must never auto-resume
# straight back into EXECUTING; recovery has to pass back through a safe
# state first (matches "EXECUTING 중 crash가 발생하면 자동으로 같은 명령을
# 재실행해서는 안 된다").
RECOVERY_RESTART_TARGETS: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.CREATED,
        LifecycleState.PLANNING,
        LifecycleState.CONTRACT_VALIDATING,
        LifecycleState.AWAITING_APPROVAL,
        LifecycleState.AWAITING_MANUAL_REVIEW,
        LifecycleState.AWAITING_FINAL_APPROVAL,
        LifecycleState.PREPARING_WORKSPACE,
        LifecycleState.FREEZING_RESULT,
        LifecycleState.HOST_VALIDATING,
        LifecycleState.VERIFYING,
        LifecycleState.REWORK_CONTRACTING,
    }
)

TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.CREATED: frozenset(
        {LifecycleState.PLANNING, LifecycleState.CANCELLED}
    ),
    LifecycleState.PLANNING: frozenset(
        {
            LifecycleState.CONTRACT_VALIDATING,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.CONTRACT_VALIDATING: frozenset(
        {
            LifecycleState.AWAITING_APPROVAL,
            LifecycleState.PREPARING_WORKSPACE,
            LifecycleState.PLANNING,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.AWAITING_APPROVAL: frozenset(
        {
            LifecycleState.PREPARING_WORKSPACE,
            LifecycleState.PLANNING,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.PREPARING_WORKSPACE: frozenset(
        {
            LifecycleState.EXECUTING,
            LifecycleState.RECOVERY_REQUIRED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.EXECUTING: frozenset(
        {
            LifecycleState.FREEZING_RESULT,
            LifecycleState.AWAITING_APPROVAL,
            LifecycleState.RECOVERY_REQUIRED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.FREEZING_RESULT: frozenset(
        {
            LifecycleState.HOST_VALIDATING,
            LifecycleState.RECOVERY_REQUIRED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.HOST_VALIDATING: frozenset(
        {
            LifecycleState.VERIFYING,
            LifecycleState.RECOVERY_REQUIRED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.VERIFYING: frozenset(
        {
            LifecycleState.REWORK_CONTRACTING,
            LifecycleState.AWAITING_MANUAL_REVIEW,
            LifecycleState.AWAITING_FINAL_APPROVAL,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.REWORK_CONTRACTING: frozenset(
        {
            LifecycleState.CONTRACT_VALIDATING,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.AWAITING_MANUAL_REVIEW: frozenset(
        {
            LifecycleState.REWORK_CONTRACTING,
            LifecycleState.AWAITING_FINAL_APPROVAL,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.AWAITING_FINAL_APPROVAL: frozenset(
        {
            LifecycleState.READY_FOR_MERGE,
            LifecycleState.REWORK_CONTRACTING,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
    ),
    LifecycleState.RECOVERY_REQUIRED: frozenset(
        RECOVERY_RESTART_TARGETS | {LifecycleState.FAILED, LifecycleState.CANCELLED}
    ),
    # Terminal states: no outgoing transitions.
    LifecycleState.READY_FOR_MERGE: frozenset(),
    LifecycleState.FAILED: frozenset(),
    LifecycleState.CANCELLED: frozenset(),
}

assert set(TRANSITIONS.keys()) == set(LifecycleState), (
    "TRANSITIONS must cover every LifecycleState, including terminal ones "
    "(mapped to an empty set)"
)

# States where the Run is parked waiting on something external. Entering one
# requires attaching a PendingAction describing what is being waited on.
PENDING_ACTION_REQUIRED_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.AWAITING_APPROVAL,
        LifecycleState.AWAITING_MANUAL_REVIEW,
        LifecycleState.AWAITING_FINAL_APPROVAL,
        LifecycleState.RECOVERY_REQUIRED,
    }
)


class PendingAction(HarnessModel):
    """What the Harness is waiting on while a Run sits in an ``AWAITING_*``
    or ``RECOVERY_REQUIRED`` state. Not persisted by this phase — Phase 2.1
    decides the storage shape."""

    kind: PendingActionKind
    description: str = Field(min_length=1)
    requested_at: AwareDatetime


class TransitionOutcome(HarnessModel):
    """Result of applying one transition: the new Run plus any side record."""

    run: Run
    pending_action: PendingAction | None = None
    failure: FailureRecord | None = None


# ---------------------------------------------------------------------------
# Pure transition functions
# ---------------------------------------------------------------------------


def can_transition(current: LifecycleState, target: LifecycleState) -> bool:
    return target in TRANSITIONS[current]


def validate_transition(current: LifecycleState, target: LifecycleState) -> None:
    if current in TERMINAL_LIFECYCLE_STATES:
        raise IllegalTransition(
            f"{current} is a terminal state; no further transitions are allowed "
            f"(attempted -> {target})"
        )
    if not can_transition(current, target):
        raise IllegalTransition(f"{current} -> {target} is not an allowed transition")


def transition_run(
    run: Run,
    target: LifecycleState,
    *,
    now: datetime,
    failure: FailureRecord | None = None,
    pending_action: PendingAction | None = None,
) -> TransitionOutcome:
    """Apply one validated transition to ``run`` and return a new Run.

    ``run`` is never mutated in place — a fresh copy with ``state``,
    ``state_version``, ``updated_at`` (and, on a terminal transition,
    ``disposition``) updated is returned instead.
    """

    validate_transition(run.state, target)

    if target is LifecycleState.FAILED and failure is None:
        raise IllegalTransition(
            "transitioning to FAILED requires a FailureRecord explaining why"
        )
    if target is not LifecycleState.FAILED and failure is not None:
        raise IllegalTransition(
            f"a FailureRecord was supplied but target is {target}, not FAILED"
        )

    if target in PENDING_ACTION_REQUIRED_STATES and pending_action is None:
        raise IllegalTransition(
            f"transitioning to {target} requires a PendingAction describing "
            "what is being waited on"
        )
    if target not in PENDING_ACTION_REQUIRED_STATES and pending_action is not None:
        raise IllegalTransition(
            f"a PendingAction was supplied but target {target} is not a "
            "pending-action state"
        )

    updates: dict[str, object] = {
        "state": target,
        "state_version": run.state_version + 1,
        "updated_at": now,
    }
    if target in TERMINAL_LIFECYCLE_STATES:
        updates["disposition"] = target

    new_run = run.model_copy(update=updates)
    return TransitionOutcome(run=new_run, pending_action=pending_action, failure=failure)
