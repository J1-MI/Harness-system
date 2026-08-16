"""Tests for the SQLite Run/Task/AgentInvocation store and atomic journal."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agent_harness.application.transitions import IllegalTransition, PendingAction
from agent_harness.domain.enums import (
    ActorType,
    AgentRole,
    FailureCategory,
    FailureCode,
    LifecycleState,
    PendingActionKind,
)
from agent_harness.domain.models import AgentInvocation, FailureRecord
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import (
    ConcurrentModificationError,
    RunNotFoundError,
    apply_transition,
    connect,
    get_failure_records,
    get_invocation,
    get_run,
    get_task,
    insert_invocation,
    insert_run,
    insert_task,
    list_journal_entries,
)
from tests.factories import VALID_DIGEST, make_run, new_id


class _FlakyConnection(sqlite3.Connection):
    """A Connection whose ``execute`` can be told to fail on one SQL prefix.

    ``sqlite3.Connection`` is a C type and cannot be monkeypatched at the
    class level (``TypeError: cannot set 'execute' attribute of immutable
    type``), so this subclass is used instead to simulate a mid-transaction
    crash deterministically.
    """

    fail_on_prefix: str | None = None

    def execute(self, sql, parameters=()):  # type: ignore[override]
        if self.fail_on_prefix and isinstance(sql, str) and sql.strip().startswith(
            self.fail_on_prefix
        ):
            raise sqlite3.OperationalError(f"simulated crash on: {sql.strip()[:40]}")
        return super().execute(sql, parameters)


def _connect_flaky() -> _FlakyConnection:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, factory=_FlakyConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON;")
    apply_migrations(connection)
    return connection


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def make_task_row(run_id: str, **overrides):
    from agent_harness.domain.models import Task

    data = dict(run_id=run_id, objective="Do the thing", attempt_count=0)
    data.update(overrides)
    return Task(**data)


def make_invocation(**overrides) -> AgentInvocation:
    data = dict(
        session_id=new_id(),
        role=AgentRole.WORKER,
        attempt=1,
        request_digest=VALID_DIGEST,
        deadline=_utc_now(),
        started_at=_utc_now(),
    )
    data.update(overrides)
    return AgentInvocation(**data)


def make_failure(run_id: str, **overrides) -> FailureRecord:
    data = dict(
        run_id=run_id,
        stage="EXECUTING",
        code=FailureCode.EXECUTION_FAILED,
        category=FailureCategory.INFRASTRUCTURE,
        retriable=False,
        sanitized_detail="worker process crashed",
        occurred_at=_utc_now(),
    )
    data.update(overrides)
    return FailureRecord(**data)


# ---------------------------------------------------------------------------
# Round-trip CRUD
# ---------------------------------------------------------------------------


def test_run_round_trips_through_sqlite(conn):
    run = make_run()
    insert_run(conn, run)
    assert get_run(conn, run.run_id) == run


def test_get_run_returns_none_for_unknown_id(conn):
    assert get_run(conn, "does-not-exist") is None


def test_task_round_trips_through_sqlite(conn):
    run = make_run()
    insert_run(conn, run)
    task = make_task_row(run.run_id)
    insert_task(conn, task)
    assert get_task(conn, task.task_id) == task


def test_invocation_round_trips_through_sqlite(conn):
    invocation = make_invocation()
    insert_invocation(conn, invocation)
    assert get_invocation(conn, invocation.invocation_id) == invocation


# ---------------------------------------------------------------------------
# apply_transition: happy path + journal hash chaining
# ---------------------------------------------------------------------------


def test_apply_transition_persists_new_state_and_journal_entry(conn):
    run = make_run(state=LifecycleState.CREATED, state_version=0)
    insert_run(conn, run)

    outcome = apply_transition(
        conn,
        run.run_id,
        LifecycleState.PLANNING,
        now=_utc_now(),
        expected_state_version=0,
        actor_type=ActorType.USER,
        actor_id="user-1",
        correlation_id="corr-1",
    )

    assert outcome.run.state is LifecycleState.PLANNING
    assert outcome.run.state_version == 1

    persisted = get_run(conn, run.run_id)
    assert persisted.state is LifecycleState.PLANNING
    assert persisted.state_version == 1

    entries = list_journal_entries(conn, run.run_id)
    assert len(entries) == 1
    assert entries[0].sequence == 0
    assert entries[0].state_before is LifecycleState.CREATED
    assert entries[0].state_after is LifecycleState.PLANNING
    assert entries[0].previous_entry_hash is None


def test_apply_transition_chains_journal_hashes_across_transitions(conn):
    run = make_run(state=LifecycleState.CREATED, state_version=0)
    insert_run(conn, run)

    apply_transition(
        conn,
        run.run_id,
        LifecycleState.PLANNING,
        now=_utc_now(),
        expected_state_version=0,
        actor_type=ActorType.USER,
        actor_id="user-1",
        correlation_id="corr-1",
    )
    apply_transition(
        conn,
        run.run_id,
        LifecycleState.CONTRACT_VALIDATING,
        now=_utc_now(),
        expected_state_version=1,
        actor_type=ActorType.HARNESS,
        actor_id="harness",
        correlation_id="corr-2",
    )

    entries = list_journal_entries(conn, run.run_id)
    assert [e.sequence for e in entries] == [0, 1]
    assert entries[1].previous_entry_hash == entries[0].entry_hash
    assert entries[1].previous_entry_hash != entries[1].entry_hash


def test_apply_transition_to_failed_persists_failure_record(conn):
    run = make_run(state=LifecycleState.PLANNING, state_version=0)
    insert_run(conn, run)
    failure = make_failure(run.run_id)

    outcome = apply_transition(
        conn,
        run.run_id,
        LifecycleState.FAILED,
        now=_utc_now(),
        expected_state_version=0,
        actor_type=ActorType.HARNESS,
        actor_id="harness",
        correlation_id="corr-1",
        failure=failure,
    )

    assert outcome.run.disposition is LifecycleState.FAILED
    persisted_failures = get_failure_records(conn, run.run_id)
    assert [f.failure_id for f in persisted_failures] == [failure.failure_id]


def test_apply_transition_to_awaiting_state_requires_pending_action(conn):
    run = make_run(state=LifecycleState.CONTRACT_VALIDATING, state_version=0)
    insert_run(conn, run)

    with pytest.raises(IllegalTransition):
        apply_transition(
            conn,
            run.run_id,
            LifecycleState.AWAITING_APPROVAL,
            now=_utc_now(),
            expected_state_version=0,
            actor_type=ActorType.HARNESS,
            actor_id="harness",
            correlation_id="corr-1",
        )

    apply_transition(
        conn,
        run.run_id,
        LifecycleState.AWAITING_APPROVAL,
        now=_utc_now(),
        expected_state_version=0,
        actor_type=ActorType.HARNESS,
        actor_id="harness",
        correlation_id="corr-2",
        pending_action=PendingAction(
            kind=PendingActionKind.APPROVAL,
            description="network access requested",
            requested_at=_utc_now(),
        ),
    )
    assert get_run(conn, run.run_id).state is LifecycleState.AWAITING_APPROVAL


def test_apply_transition_raises_for_unknown_run(conn):
    with pytest.raises(RunNotFoundError):
        apply_transition(
            conn,
            "no-such-run",
            LifecycleState.PLANNING,
            now=_utc_now(),
            expected_state_version=0,
            actor_type=ActorType.USER,
            actor_id="user-1",
            correlation_id="corr-1",
        )


# ---------------------------------------------------------------------------
# Concurrent update (optimistic concurrency)
# ---------------------------------------------------------------------------


def test_apply_transition_rejects_stale_expected_version(conn):
    run = make_run(state=LifecycleState.CREATED, state_version=0)
    insert_run(conn, run)

    # First caller wins.
    apply_transition(
        conn,
        run.run_id,
        LifecycleState.PLANNING,
        now=_utc_now(),
        expected_state_version=0,
        actor_type=ActorType.USER,
        actor_id="user-1",
        correlation_id="corr-1",
    )

    # A second caller that read the Run before the first one committed is
    # still holding expected_state_version=0 and must be rejected.
    with pytest.raises(ConcurrentModificationError):
        apply_transition(
            conn,
            run.run_id,
            LifecycleState.CANCELLED,
            now=_utc_now(),
            expected_state_version=0,
            actor_type=ActorType.USER,
            actor_id="user-2",
            correlation_id="corr-2",
        )

    # The rejected attempt must not have partially applied.
    persisted = get_run(conn, run.run_id)
    assert persisted.state is LifecycleState.PLANNING
    assert persisted.state_version == 1
    assert len(list_journal_entries(conn, run.run_id)) == 1


# ---------------------------------------------------------------------------
# Crash mid-transaction -> full rollback, no partial state
# ---------------------------------------------------------------------------


def test_apply_transition_rolls_back_completely_on_mid_transaction_crash():
    conn = _connect_flaky()
    try:
        run = make_run(state=LifecycleState.CREATED, state_version=0)
        insert_run(conn, run)

        conn.fail_on_prefix = "UPDATE runs"
        with pytest.raises(sqlite3.OperationalError):
            apply_transition(
                conn,
                run.run_id,
                LifecycleState.PLANNING,
                now=_utc_now(),
                expected_state_version=0,
                actor_type=ActorType.USER,
                actor_id="user-1",
                correlation_id="corr-1",
            )
        conn.fail_on_prefix = None

        # The journal INSERT ran before the simulated crash, but ROLLBACK
        # must have undone it along with everything else in the transaction.
        persisted = get_run(conn, run.run_id)
        assert persisted.state is LifecycleState.CREATED
        assert persisted.state_version == 0
        assert list_journal_entries(conn, run.run_id) == []
    finally:
        conn.close()


def test_database_is_usable_after_a_rolled_back_transaction():
    conn = _connect_flaky()
    try:
        run = make_run(state=LifecycleState.CREATED, state_version=0)
        insert_run(conn, run)

        conn.fail_on_prefix = "UPDATE runs"
        with pytest.raises(sqlite3.OperationalError):
            apply_transition(
                conn,
                run.run_id,
                LifecycleState.PLANNING,
                now=_utc_now(),
                expected_state_version=0,
                actor_type=ActorType.USER,
                actor_id="user-1",
                correlation_id="corr-1",
            )
        conn.fail_on_prefix = None

        # A subsequent, non-flaky transition must succeed normally.
        outcome = apply_transition(
            conn,
            run.run_id,
            LifecycleState.PLANNING,
            now=_utc_now(),
            expected_state_version=0,
            actor_type=ActorType.USER,
            actor_id="user-1",
            correlation_id="corr-2",
        )
        assert outcome.run.state is LifecycleState.PLANNING
    finally:
        conn.close()
