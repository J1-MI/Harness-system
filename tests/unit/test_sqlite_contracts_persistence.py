"""Tests for TaskContract/PolicyDecision/VerificationResult/ReworkContract
persistence (Codex implementation review B-04, partial).

Only proves the storage layer itself round-trips correctly — these
functions are not yet called from ``application.orchestrator`` for every
step (see ``docs/architecture/hardening-and-recovery.md`` and the Codex
review response notes for exactly what remains open).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import (
    connect,
    get_policy_decision,
    get_rework_contract,
    get_task_contract,
    get_verification_result,
    insert_policy_decision,
    insert_rework_contract,
    insert_run,
    insert_task_contract,
    insert_verification_result,
    list_policy_decisions_for_run,
    list_rework_contracts_for_run,
    list_task_contracts_for_run,
    list_verification_results_for_run,
)
from tests.factories import (
    make_policy_decision,
    make_rework_contract,
    make_run,
    make_task_contract,
    make_verification_result,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "harness.db")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def seeded_run(db_conn):
    run = make_run(repository_id="repo-1")
    insert_run(db_conn, run)
    return run


def test_task_contract_round_trips(db_conn, seeded_run):
    contract = make_task_contract(run_id=seeded_run.run_id, task_id="task-1")
    insert_task_contract(db_conn, contract, run_id=seeded_run.run_id, now=_utc_now())

    fetched = get_task_contract(db_conn, contract.contract_id)
    assert fetched == contract
    assert list_task_contracts_for_run(db_conn, seeded_run.run_id) == [contract]
    assert get_task_contract(db_conn, "does-not-exist") is None


def test_policy_decision_round_trips(db_conn, seeded_run):
    decision = make_policy_decision()
    insert_policy_decision(db_conn, decision, run_id=seeded_run.run_id)

    fetched = get_policy_decision(db_conn, decision.decision_id)
    assert fetched == decision
    assert list_policy_decisions_for_run(db_conn, seeded_run.run_id) == [decision]


def test_verification_result_round_trips(db_conn, seeded_run):
    result = make_verification_result(task_id="task-1")
    insert_verification_result(db_conn, result, run_id=seeded_run.run_id, now=_utc_now())

    fetched = get_verification_result(db_conn, result.verification_id)
    assert fetched == result
    assert list_verification_results_for_run(db_conn, seeded_run.run_id) == [result]


def test_rework_contract_round_trips(db_conn, seeded_run):
    rework = make_rework_contract(task_id="task-1")
    insert_rework_contract(db_conn, rework, run_id=seeded_run.run_id, now=_utc_now())

    fetched = get_rework_contract(db_conn, rework.rework_id)
    assert fetched == rework
    assert list_rework_contracts_for_run(db_conn, seeded_run.run_id) == [rework]


def test_multiple_task_contracts_for_the_same_run_are_all_listed(db_conn, seeded_run):
    first = make_task_contract(run_id=seeded_run.run_id, task_id="task-1", contract_revision=1)
    second = make_task_contract(run_id=seeded_run.run_id, task_id="task-1", contract_revision=2)
    insert_task_contract(db_conn, first, run_id=seeded_run.run_id, now=_utc_now())
    insert_task_contract(db_conn, second, run_id=seeded_run.run_id, now=_utc_now())

    fetched_ids = {c.contract_id for c in list_task_contracts_for_run(db_conn, seeded_run.run_id)}
    assert fetched_ids == {first.contract_id, second.contract_id}
