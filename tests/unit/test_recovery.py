"""Tests for the Recovery Coordinator (Phase 13)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_harness.application.recovery import (
    RECOVERABLE_STATES,
    STALE_NO_RECOVERY_PATH_STATES,
    check_base_revision_stale,
    run_recovery_scan,
)
from agent_harness.domain.enums import ActorType, LifecycleState
from agent_harness.domain.models import BudgetRequest, BudgetUsage, RepositoryRef, Run
from agent_harness.execution.git_client import GitClient
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import apply_transition, connect, get_run, insert_run

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not available"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "tester@example.com", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    (repo / "a.txt").write_bytes(b"hello\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "harness.db")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def seed_run(conn, *, updated_at: datetime) -> Run:
    run = Run(
        user_request_ref="req-1",
        user_request_digest="sha256:" + "0" * 64,
        repository_id="repo-1",
        budget_limits=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        budget_used=BudgetUsage(),
        created_at=updated_at,
        updated_at=updated_at,
    )
    insert_run(conn, run)
    return run


def advance(conn, run: Run, target: LifecycleState) -> Run:
    outcome = apply_transition(
        conn, run.run_id, target, now=run.updated_at, expected_state_version=run.state_version,
        actor_type=ActorType.HARNESS, actor_id="test-seed", correlation_id="seed",
    )
    return outcome.run


# ---------------------------------------------------------------------------
# run_recovery_scan
# ---------------------------------------------------------------------------


def test_recent_active_run_is_left_alone(db_conn):
    run = seed_run(db_conn, updated_at=_utc_now())
    advance(db_conn, run, LifecycleState.PLANNING)

    report = run_recovery_scan(db_conn, now=_utc_now(), stale_after=timedelta(minutes=30))

    assert report.outcomes == []
    assert get_run(db_conn, run.run_id).state is LifecycleState.PLANNING


def test_stale_run_in_recoverable_state_moves_to_recovery_required(db_conn):
    old = _utc_now() - timedelta(hours=2)
    run = seed_run(db_conn, updated_at=old)
    run = advance(db_conn, run, LifecycleState.PLANNING)
    run = advance(db_conn, run, LifecycleState.CONTRACT_VALIDATING)
    run = advance(db_conn, run, LifecycleState.PREPARING_WORKSPACE)

    report = run_recovery_scan(db_conn, now=_utc_now(), stale_after=timedelta(minutes=30))

    assert len(report.outcomes) == 1
    outcome = report.outcomes[0]
    assert outcome.run_id == run.run_id
    assert outcome.stale_state is LifecycleState.PREPARING_WORKSPACE
    assert outcome.action == "RECOVERY_REQUIRED"
    assert get_run(db_conn, run.run_id).state is LifecycleState.RECOVERY_REQUIRED


def test_stale_run_with_no_recovery_path_moves_to_failed_and_is_retriable(db_conn):
    old = _utc_now() - timedelta(hours=2)
    run = seed_run(db_conn, updated_at=old)
    run = advance(db_conn, run, LifecycleState.PLANNING)

    report = run_recovery_scan(db_conn, now=_utc_now(), stale_after=timedelta(minutes=30))

    assert len(report.outcomes) == 1
    assert report.outcomes[0].action == "FAILED"

    from agent_harness.persistence.sqlite import get_failure_records

    failures = get_failure_records(db_conn, run.run_id)
    assert len(failures) == 1
    assert failures[0].retriable is True
    final = get_run(db_conn, run.run_id)
    assert final.state is LifecycleState.FAILED
    assert final.disposition is LifecycleState.FAILED


def test_awaiting_states_are_never_touched_by_the_scan(db_conn):
    from agent_harness.application.transitions import PendingAction
    from agent_harness.domain.enums import PendingActionKind

    old = _utc_now() - timedelta(hours=2)
    run = seed_run(db_conn, updated_at=old)
    run = advance(db_conn, run, LifecycleState.PLANNING)
    run = advance(db_conn, run, LifecycleState.CONTRACT_VALIDATING)
    outcome = apply_transition(
        db_conn, run.run_id, LifecycleState.AWAITING_APPROVAL, now=old,
        expected_state_version=run.state_version, actor_type=ActorType.HARNESS,
        actor_id="test-seed", correlation_id="seed",
        pending_action=PendingAction(kind=PendingActionKind.APPROVAL, description="needs approval", requested_at=old),
    )

    report = run_recovery_scan(db_conn, now=_utc_now(), stale_after=timedelta(minutes=30))

    assert report.outcomes == []
    assert get_run(db_conn, run.run_id).state is LifecycleState.AWAITING_APPROVAL


def test_recoverable_and_no_recovery_path_sets_partition_all_active_states():
    from agent_harness.domain.enums import TERMINAL_LIFECYCLE_STATES

    awaiting_states = {
        LifecycleState.AWAITING_APPROVAL, LifecycleState.AWAITING_MANUAL_REVIEW,
        LifecycleState.AWAITING_FINAL_APPROVAL, LifecycleState.RECOVERY_REQUIRED,
        LifecycleState.CREATED,
    }
    all_states = set(LifecycleState)
    remaining = all_states - TERMINAL_LIFECYCLE_STATES - awaiting_states
    assert remaining == RECOVERABLE_STATES | STALE_NO_RECOVERY_PATH_STATES
    assert not (RECOVERABLE_STATES & STALE_NO_RECOVERY_PATH_STATES)


# ---------------------------------------------------------------------------
# check_base_revision_stale
# ---------------------------------------------------------------------------


def test_check_base_revision_stale_false_when_target_ref_unchanged(tmp_path):
    repo = make_repo(tmp_path)
    git_client = GitClient(tmp_path / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=repo)

    ref = RepositoryRef(
        repository_id="repo-1", base_commit_sha=head_sha, target_ref="refs/heads/main",
        expected_repository_fingerprint="sha256:" + "0" * 64,
    )
    assert check_base_revision_stale(git_client, repo, ref) is False


def test_check_base_revision_stale_true_after_target_ref_moves(tmp_path):
    repo = make_repo(tmp_path)
    git_client = GitClient(tmp_path / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=repo)

    (repo / "b.txt").write_bytes(b"more\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "second commit", cwd=repo)

    ref = RepositoryRef(
        repository_id="repo-1", base_commit_sha=head_sha, target_ref="refs/heads/main",
        expected_repository_fingerprint="sha256:" + "0" * 64,
    )
    assert check_base_revision_stale(git_client, repo, ref) is True
