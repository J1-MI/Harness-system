"""Tests for the pure LifecycleState transition engine (Phase 1.2).

The expected-transition table below is transcribed independently from
architecture review section 5, not imported from
``application/transitions.py`` — the point is to catch typos in the
implementation's own table, not to check the table against itself.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from agent_harness.domain.enums import LifecycleState as S
from agent_harness.domain.enums import PendingActionKind
from agent_harness.application.transitions import (
    IllegalTransition,
    PendingAction,
    can_transition,
    transition_run,
    validate_transition,
)
from tests.factories import make_run

EXPECTED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PLANNING, S.CANCELLED}),
    S.PLANNING: frozenset({S.CONTRACT_VALIDATING, S.FAILED, S.CANCELLED}),
    S.CONTRACT_VALIDATING: frozenset(
        {S.AWAITING_APPROVAL, S.PREPARING_WORKSPACE, S.PLANNING, S.FAILED, S.CANCELLED}
    ),
    S.AWAITING_APPROVAL: frozenset(
        {S.PREPARING_WORKSPACE, S.PLANNING, S.FAILED, S.CANCELLED}
    ),
    S.PREPARING_WORKSPACE: frozenset(
        {S.EXECUTING, S.RECOVERY_REQUIRED, S.FAILED, S.CANCELLED}
    ),
    S.EXECUTING: frozenset(
        {S.FREEZING_RESULT, S.AWAITING_APPROVAL, S.RECOVERY_REQUIRED, S.FAILED, S.CANCELLED}
    ),
    S.FREEZING_RESULT: frozenset(
        {S.HOST_VALIDATING, S.RECOVERY_REQUIRED, S.FAILED, S.CANCELLED}
    ),
    S.HOST_VALIDATING: frozenset(
        {S.VERIFYING, S.RECOVERY_REQUIRED, S.FAILED, S.CANCELLED}
    ),
    S.VERIFYING: frozenset(
        {
            S.REWORK_CONTRACTING,
            S.AWAITING_MANUAL_REVIEW,
            S.AWAITING_FINAL_APPROVAL,
            S.FAILED,
            S.CANCELLED,
        }
    ),
    S.REWORK_CONTRACTING: frozenset({S.CONTRACT_VALIDATING, S.FAILED, S.CANCELLED}),
    S.AWAITING_MANUAL_REVIEW: frozenset(
        {S.REWORK_CONTRACTING, S.AWAITING_FINAL_APPROVAL, S.FAILED, S.CANCELLED}
    ),
    S.AWAITING_FINAL_APPROVAL: frozenset(
        {S.READY_FOR_MERGE, S.REWORK_CONTRACTING, S.FAILED, S.CANCELLED}
    ),
    # RECOVERY_REQUIRED resumes into any restartable state (everything
    # except EXECUTING and itself) or terminates.
    S.RECOVERY_REQUIRED: frozenset(
        {
            S.CREATED,
            S.PLANNING,
            S.CONTRACT_VALIDATING,
            S.AWAITING_APPROVAL,
            S.AWAITING_MANUAL_REVIEW,
            S.AWAITING_FINAL_APPROVAL,
            S.PREPARING_WORKSPACE,
            S.FREEZING_RESULT,
            S.HOST_VALIDATING,
            S.VERIFYING,
            S.REWORK_CONTRACTING,
            S.FAILED,
            S.CANCELLED,
        }
    ),
    S.READY_FOR_MERGE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({S.READY_FOR_MERGE, S.FAILED, S.CANCELLED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Exhaustive matrix: every (current, target) pair against the spec table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target", list(itertools.product(S, S)), ids=lambda s: s.value
)
def test_can_transition_matches_spec_table(current, target):
    expected = target in EXPECTED_TRANSITIONS[current]
    assert can_transition(current, target) is expected, (
        f"{current} -> {target}: expected allowed={expected}"
    )


def test_expected_table_covers_every_state_pair_at_least_once():
    # Sanity check on the test's own fixture data, not the implementation.
    assert set(EXPECTED_TRANSITIONS.keys()) == set(S)


# ---------------------------------------------------------------------------
# Terminal state immutability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES, key=str))
@pytest.mark.parametrize("target", list(S), ids=lambda s: s.value)
def test_terminal_states_reject_every_transition_attempt(terminal_state, target):
    with pytest.raises(IllegalTransition):
        validate_transition(terminal_state, target)


@pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES, key=str))
def test_transition_run_rejects_mutation_from_terminal_state(terminal_state):
    run = make_run(state=terminal_state, disposition=terminal_state)
    with pytest.raises(IllegalTransition):
        transition_run(run, S.PLANNING, now=_utc_now())


# ---------------------------------------------------------------------------
# FailureRecord linkage
# ---------------------------------------------------------------------------


def _make_failure(run_id: str):
    from agent_harness.domain.enums import FailureCategory, FailureCode
    from agent_harness.domain.models import FailureRecord

    return FailureRecord(
        run_id=run_id,
        stage="PLANNING",
        code=FailureCode.EXECUTION_FAILED,
        category=FailureCategory.INFRASTRUCTURE,
        retriable=False,
        sanitized_detail="planner process crashed",
        occurred_at=_utc_now(),
    )


def test_transition_to_failed_requires_failure_record():
    run = make_run(state=S.PLANNING)
    with pytest.raises(IllegalTransition):
        transition_run(run, S.FAILED, now=_utc_now())


def test_transition_to_failed_with_failure_record_succeeds():
    run = make_run(state=S.PLANNING)
    outcome = transition_run(
        run, S.FAILED, now=_utc_now(), failure=_make_failure(run.run_id)
    )
    assert outcome.run.state is S.FAILED
    assert outcome.run.disposition is S.FAILED
    assert outcome.failure is not None


def test_transition_rejects_failure_record_on_non_failed_target():
    run = make_run(state=S.PLANNING)
    with pytest.raises(IllegalTransition):
        transition_run(
            run,
            S.CONTRACT_VALIDATING,
            now=_utc_now(),
            failure=_make_failure(run.run_id),
        )


# ---------------------------------------------------------------------------
# PendingAction linkage
# ---------------------------------------------------------------------------


def test_transition_to_awaiting_approval_requires_pending_action():
    run = make_run(state=S.CONTRACT_VALIDATING)
    with pytest.raises(IllegalTransition):
        transition_run(run, S.AWAITING_APPROVAL, now=_utc_now())


def test_transition_to_awaiting_approval_with_pending_action_succeeds():
    run = make_run(state=S.CONTRACT_VALIDATING)
    outcome = transition_run(
        run,
        S.AWAITING_APPROVAL,
        now=_utc_now(),
        pending_action=PendingAction(
            kind=PendingActionKind.APPROVAL,
            description="network access to pypi.org requested",
            requested_at=_utc_now(),
        ),
    )
    assert outcome.run.state is S.AWAITING_APPROVAL
    assert outcome.pending_action is not None
    assert outcome.run.disposition is None


def test_transition_rejects_pending_action_on_non_waiting_target():
    run = make_run(state=S.AWAITING_APPROVAL)
    with pytest.raises(IllegalTransition):
        transition_run(
            run,
            S.PREPARING_WORKSPACE,
            now=_utc_now(),
            pending_action=PendingAction(
                kind=PendingActionKind.APPROVAL,
                description="stale pending action",
                requested_at=_utc_now(),
            ),
        )


def test_recovery_required_entry_requires_pending_action():
    run = make_run(state=S.EXECUTING)
    with pytest.raises(IllegalTransition):
        transition_run(run, S.RECOVERY_REQUIRED, now=_utc_now())


def test_recovery_required_cannot_resume_directly_into_executing():
    assert not can_transition(S.RECOVERY_REQUIRED, S.EXECUTING)


# ---------------------------------------------------------------------------
# state_version / updated_at bookkeeping
# ---------------------------------------------------------------------------


def test_transition_run_increments_state_version_and_preserves_identity():
    run = make_run(state=S.CREATED, state_version=0)
    outcome = transition_run(run, S.PLANNING, now=_utc_now())

    assert outcome.run.state is S.PLANNING
    assert outcome.run.state_version == 1
    assert outcome.run.run_id == run.run_id
    # transition_run must not mutate the input in place
    assert run.state is S.CREATED
    assert run.state_version == 0


def test_transition_run_does_not_set_disposition_for_non_terminal_target():
    run = make_run(state=S.CREATED)
    outcome = transition_run(run, S.PLANNING, now=_utc_now())
    assert outcome.run.disposition is None
