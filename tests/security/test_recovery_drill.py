"""Recovery drill (Phase 13): simulate a crash mid-pipeline, run the
Recovery Coordinator, and confirm the whole loop — crash -> RECOVERY_REQUIRED
-> CLI resume -> back to an active state — actually closes, using the real
Phase 12 CLI and Phase 13 recovery scan together rather than either in
isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness.application.recovery import run_recovery_scan
from agent_harness.domain.enums import ActorType, LifecycleState
from agent_harness.domain.models import BudgetRequest, BudgetUsage, Run
from agent_harness.interfaces.cli import app
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import apply_transition, connect, get_run, insert_run

runner = CliRunner()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def db(tmp_path) -> Path:
    return tmp_path / "harness.db"


def seed_crashed_run(db: Path, *, stale_state: LifecycleState, stale_since: datetime) -> Run:
    conn = connect(db)
    apply_migrations(conn)
    run = Run(
        user_request_ref="req-1", user_request_digest="sha256:" + "0" * 64, repository_id="repo-1",
        budget_limits=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        budget_used=BudgetUsage(), created_at=stale_since, updated_at=stale_since,
    )
    insert_run(conn, run)

    for target in [LifecycleState.PLANNING, LifecycleState.CONTRACT_VALIDATING, LifecycleState.PREPARING_WORKSPACE]:
        outcome = apply_transition(
            conn, run.run_id, target, now=stale_since, expected_state_version=run.state_version,
            actor_type=ActorType.HARNESS, actor_id="crashed-worker-process", correlation_id="pre-crash",
        )
        run = outcome.run
        if target is stale_state:
            break
    conn.close()
    return run


def test_recovery_drill_crash_mid_workspace_prep_then_cli_resume(db):
    stale_since = _utc_now() - timedelta(hours=6)
    run = seed_crashed_run(db, stale_state=LifecycleState.PREPARING_WORKSPACE, stale_since=stale_since)

    conn = connect(db)
    apply_migrations(conn)
    assert get_run(conn, run.run_id).state is LifecycleState.PREPARING_WORKSPACE

    # A fresh harness process starts up and runs its recovery scan —
    # simulating the crashed process never having gotten to finish EXECUTING.
    report = run_recovery_scan(conn, now=_utc_now(), stale_after=timedelta(minutes=30))
    conn.close()

    assert len(report.outcomes) == 1
    assert report.outcomes[0].action == "RECOVERY_REQUIRED"

    status_result = runner.invoke(app, ["status", run.run_id, "--db", str(db)])
    assert status_result.exit_code == 0
    assert "RECOVERY_REQUIRED" in status_result.output
    assert "stale in PREPARING_WORKSPACE" in status_result.output

    # An operator inspects the drill, decides it's safe to restart
    # workspace preparation, and resumes via the CLI — never straight
    # back into EXECUTING (that target is not offered/accepted).
    bad_resume = runner.invoke(app, ["resume", run.run_id, "--to", "EXECUTING", "--db", str(db)])
    assert bad_resume.exit_code == 1

    good_resume = runner.invoke(app, ["resume", run.run_id, "--to", "PREPARING_WORKSPACE", "--db", str(db)])
    assert good_resume.exit_code == 0

    conn = connect(db)
    final = get_run(conn, run.run_id)
    conn.close()
    assert final.state is LifecycleState.PREPARING_WORKSPACE


def test_recovery_drill_stale_planning_fails_cleanly_as_retriable(db):
    """A crash with no host-side lease (stale mid-PLANNING) has no
    RECOVERY_REQUIRED path in the state machine — the drill here is
    confirming that lands cleanly on a retriable FAILED, not stuck or
    silently retried."""

    stale_since = _utc_now() - timedelta(hours=6)
    run = seed_crashed_run(db, stale_state=LifecycleState.PLANNING, stale_since=stale_since)

    conn = connect(db)
    apply_migrations(conn)
    report = run_recovery_scan(conn, now=_utc_now(), stale_after=timedelta(minutes=30))
    conn.close()

    assert report.outcomes[0].action == "FAILED"

    report_result = runner.invoke(app, ["report", run.run_id, "--db", str(db)])
    assert report_result.exit_code == 0
    import json

    manifest = json.loads(report_result.output)
    assert manifest["run_state"] == "FAILED"
    assert manifest["failure"]["retriable"] is True
