"""Tests for the final manifest builder (Phase 12)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_harness.application.reporting import RunNotFoundForReportError, build_final_manifest
from agent_harness.domain.enums import ActorType, LifecycleState
from agent_harness.domain.models import Task
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import apply_transition, connect, insert_run, insert_task
from tests.factories import make_run


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


def test_build_final_manifest_raises_for_unknown_run(db_conn):
    with pytest.raises(RunNotFoundForReportError):
        build_final_manifest(db_conn, "does-not-exist", generated_at=_utc_now())


def test_build_final_manifest_reflects_run_and_journal_state(db_conn):
    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="do the thing")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    outcome = apply_transition(
        db_conn, run.run_id, LifecycleState.PLANNING, now=_utc_now(),
        expected_state_version=run.state_version, actor_type=ActorType.HARNESS,
        actor_id="test", correlation_id="corr-1",
    )
    run = outcome.run

    manifest = build_final_manifest(db_conn, run.run_id, generated_at=_utc_now())

    assert manifest.run_id == run.run_id
    assert manifest.run_state is LifecycleState.PLANNING
    assert manifest.run_disposition is None
    assert manifest.journal_entry_count == 1
    assert manifest.journal_head_hash is not None
    assert manifest.failure is None


def test_build_final_manifest_digest_changes_when_content_does(db_conn):
    run = make_run(repository_id="repo-1")
    insert_run(db_conn, run)
    manifest_a = build_final_manifest(db_conn, run.run_id, generated_at=_utc_now())

    apply_transition(
        db_conn, run.run_id, LifecycleState.PLANNING, now=_utc_now(),
        expected_state_version=run.state_version, actor_type=ActorType.HARNESS,
        actor_id="test", correlation_id="corr-1",
    )
    manifest_b = build_final_manifest(db_conn, run.run_id, generated_at=_utc_now())

    assert manifest_a.manifest_digest != manifest_b.manifest_digest
