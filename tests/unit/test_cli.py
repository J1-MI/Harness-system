"""Tests for the Harness CLI (Phase 12), via ``typer.testing.CliRunner``.

Covers the roadmap's two named test criteria directly: "crash-safe
commands" (each command opens its own connection and performs one bounded
atomic operation — verified indirectly by checking every command leaves
the DB in a fully-committed, self-consistent state) and "ambiguous
approval 방지" (approve/reject refuse to act on a Run that isn't actually
awaiting anything).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_harness.application.transitions import PendingAction
from agent_harness.domain.enums import ActorType, LifecycleState, PendingActionKind
from agent_harness.domain.models import BudgetRequest, BudgetUsage, Run
from agent_harness.interfaces.cli import app
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import apply_transition, connect, get_run, insert_run
from tests.unit.test_workspace import make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not available"
)

runner = CliRunner()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seed_run(db: Path, *, repository_id: str = "repo-1") -> Run:
    conn = connect(db)
    apply_migrations(conn)
    run = Run(
        user_request_ref="req-1",
        user_request_digest="sha256:" + "0" * 64,
        repository_id=repository_id,
        budget_limits=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        budget_used=BudgetUsage(),
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    insert_run(conn, run)
    conn.close()
    return run


def advance(db: Path, run_id: str, *states_and_pending) -> Run:
    """Walk a freshly-seeded Run through a sequence of
    ``(LifecycleState, PendingAction | None)`` transitions directly via
    ``apply_transition`` (bypassing the CLI) so tests can set up whatever
    starting state they need."""

    conn = connect(db)
    try:
        run = get_run(conn, run_id)
        for target, pending_action in states_and_pending:
            outcome = apply_transition(
                conn, run_id, target, now=_utc_now(), expected_state_version=run.state_version,
                actor_type=ActorType.HARNESS, actor_id="test-seed", correlation_id="seed",
                pending_action=pending_action,
            )
            run = outcome.run
        return run
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path) -> Path:
    return tmp_path / "harness.db"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_unknown_run_exits_nonzero(db):
    result = runner.invoke(app, ["status", "does-not-exist", "--db", str(db)])
    assert result.exit_code == 1
    assert "no Run found" in result.output


def test_status_shows_pending_action_when_awaiting(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (
            LifecycleState.AWAITING_APPROVAL,
            PendingAction(kind=PendingActionKind.APPROVAL, description="needs raw_shell", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["status", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "AWAITING_APPROVAL" in result.output
    assert "needs raw_shell" in result.output


# ---------------------------------------------------------------------------
# approve / reject: ambiguous-approval prevention
# ---------------------------------------------------------------------------


def test_approve_refuses_run_not_awaiting_anything(db):
    run = seed_run(db)
    advance(db, run.run_id, (LifecycleState.PLANNING, None))

    result = runner.invoke(app, ["approve", run.run_id, "--db", str(db)])
    assert result.exit_code == 1
    assert "is not awaiting approval" in result.output

    conn = connect(db)
    assert get_run(conn, run.run_id).state is LifecycleState.PLANNING  # untouched
    conn.close()


def test_reject_refuses_run_not_awaiting_anything(db):
    run = seed_run(db)
    result = runner.invoke(app, ["reject", run.run_id, "--db", str(db)])
    assert result.exit_code == 1
    assert "is not awaiting approval" in result.output


def test_approve_awaiting_approval_moves_to_preparing_workspace(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (
            LifecycleState.AWAITING_APPROVAL,
            PendingAction(kind=PendingActionKind.APPROVAL, description="needs approval", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["approve", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "PREPARING_WORKSPACE" in result.output

    conn = connect(db)
    assert get_run(conn, run.run_id).state is LifecycleState.PREPARING_WORKSPACE
    conn.close()


def test_reject_awaiting_approval_moves_to_failed_with_failure_record(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (
            LifecycleState.AWAITING_APPROVAL,
            PendingAction(kind=PendingActionKind.APPROVAL, description="needs approval", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["reject", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "FAILED" in result.output

    from agent_harness.persistence.sqlite import get_failure_records

    conn = connect(db)
    final_run = get_run(conn, run.run_id)
    failures = get_failure_records(conn, run.run_id)
    conn.close()
    assert final_run.state is LifecycleState.FAILED
    assert final_run.disposition is LifecycleState.FAILED
    assert len(failures) == 1


def test_approve_awaiting_final_approval_moves_to_ready_for_merge(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (LifecycleState.PREPARING_WORKSPACE, None),
        (LifecycleState.EXECUTING, None),
        (LifecycleState.FREEZING_RESULT, None),
        (LifecycleState.HOST_VALIDATING, None),
        (LifecycleState.VERIFYING, None),
        (
            LifecycleState.AWAITING_FINAL_APPROVAL,
            PendingAction(kind=PendingActionKind.FINAL_APPROVAL, description="verified PASS", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["approve", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "READY_FOR_MERGE" in result.output


def test_reject_awaiting_final_approval_moves_to_rework_contracting(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (LifecycleState.PREPARING_WORKSPACE, None),
        (LifecycleState.EXECUTING, None),
        (LifecycleState.FREEZING_RESULT, None),
        (LifecycleState.HOST_VALIDATING, None),
        (LifecycleState.VERIFYING, None),
        (
            LifecycleState.AWAITING_FINAL_APPROVAL,
            PendingAction(kind=PendingActionKind.FINAL_APPROVAL, description="verified PASS", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["reject", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "REWORK_CONTRACTING" in result.output


def test_approve_awaiting_manual_review_moves_to_awaiting_final_approval(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (LifecycleState.PREPARING_WORKSPACE, None),
        (LifecycleState.EXECUTING, None),
        (LifecycleState.FREEZING_RESULT, None),
        (LifecycleState.HOST_VALIDATING, None),
        (LifecycleState.VERIFYING, None),
        (
            LifecycleState.AWAITING_MANUAL_REVIEW,
            PendingAction(kind=PendingActionKind.MANUAL_REVIEW, description="needs human look", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["approve", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "AWAITING_FINAL_APPROVAL" in result.output


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_refuses_when_not_recovery_required(db):
    run = seed_run(db)
    result = runner.invoke(app, ["resume", run.run_id, "--to", "PLANNING", "--db", str(db)])
    assert result.exit_code == 1
    assert "is not in RECOVERY_REQUIRED" in result.output


def test_resume_refuses_unsafe_target(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (LifecycleState.PREPARING_WORKSPACE, None),
        (
            LifecycleState.RECOVERY_REQUIRED,
            PendingAction(kind=PendingActionKind.RECOVERY_DECISION, description="crash mid-workspace-prep", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["resume", run.run_id, "--to", "EXECUTING", "--db", str(db)])
    assert result.exit_code == 1
    assert "not a safe recovery target" in result.output


def test_resume_succeeds_to_valid_target(db):
    run = seed_run(db)
    advance(
        db, run.run_id,
        (LifecycleState.PLANNING, None),
        (LifecycleState.CONTRACT_VALIDATING, None),
        (LifecycleState.PREPARING_WORKSPACE, None),
        (
            LifecycleState.RECOVERY_REQUIRED,
            PendingAction(kind=PendingActionKind.RECOVERY_DECISION, description="crash mid-workspace-prep", requested_at=_utc_now()),
        ),
    )
    result = runner.invoke(app, ["resume", run.run_id, "--to", "PREPARING_WORKSPACE", "--db", str(db)])
    assert result.exit_code == 0
    assert "PREPARING_WORKSPACE" in result.output


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_moves_active_run_to_cancelled(db):
    run = seed_run(db)
    advance(db, run.run_id, (LifecycleState.PLANNING, None))
    result = runner.invoke(app, ["cancel", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    assert "CANCELLED" in result.output


def test_cancel_refuses_already_terminal_run(db):
    run = seed_run(db)
    advance(db, run.run_id, (LifecycleState.CANCELLED, None))
    result = runner.invoke(app, ["cancel", run.run_id, "--db", str(db)])
    assert result.exit_code == 1
    assert "already terminal" in result.output


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_unknown_run_errors(db):
    result = runner.invoke(app, ["report", "does-not-exist", "--db", str(db)])
    assert result.exit_code == 1


def test_report_prints_manifest_json(db):
    run = seed_run(db)
    result = runner.invoke(app, ["report", run.run_id, "--db", str(db)])
    assert result.exit_code == 0
    manifest = json.loads(result.output)
    assert manifest["run_id"] == run.run_id
    assert manifest["manifest_digest"].startswith("sha256:")


def test_report_writes_to_file_with_out_option(db, tmp_path):
    run = seed_run(db)
    out_path = tmp_path / "manifest.json"
    result = runner.invoke(app, ["report", run.run_id, "--db", str(db), "--out", str(out_path)])
    assert result.exit_code == 0
    assert json.loads(out_path.read_text())["run_id"] == run.run_id


# ---------------------------------------------------------------------------
# demo (fake-provider pipeline) + cleanup
# ---------------------------------------------------------------------------


def test_demo_command_pipeline_reaches_ready_for_merge(tmp_path, db):
    repo = make_repo(tmp_path, "source-repo")
    data_root = tmp_path / "data-root"

    result = runner.invoke(
        app,
        [
            "demo", str(repo), "add a demo file",
            "--db", str(db), "--data-root", str(data_root), "--auto-approve",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "READY_FOR_MERGE" in result.output


def test_cleanup_removes_worktree_for_terminal_run(tmp_path, db):
    repo = make_repo(tmp_path, "source-repo")
    data_root = tmp_path / "data-root"

    run_result = runner.invoke(
        app,
        [
            "demo", str(repo), "add a demo file",
            "--db", str(db), "--data-root", str(data_root), "--auto-approve",
        ],
    )
    assert run_result.exit_code == 0, run_result.output

    conn = connect(db)
    from agent_harness.persistence.sqlite import list_journal_entries

    # The demo run's run_id isn't printed, so recover it via the only Run row.
    run_id = conn.execute("SELECT run_id FROM runs LIMIT 1").fetchone()["run_id"]
    conn.close()

    from agent_harness.execution.workspace import worktree_path_for

    worktree_path = worktree_path_for(data_root, repository_id="cli-demo-repo", run_id=run_id)
    assert worktree_path.exists()

    cleanup_result = runner.invoke(
        app,
        ["cleanup", run_id, "--db", str(db), "--data-root", str(data_root), "--source-repo", str(repo)],
    )
    assert cleanup_result.exit_code == 0, cleanup_result.output
    assert not worktree_path.exists()


def test_cleanup_purge_never_deletes_a_blob_another_run_still_references(tmp_path, db):
    """Codex review M-04: write_blob dedups identical bytes to one file on
    disk, but each call still inserts a fresh Artifact row — two
    unrelated Runs producing byte-identical evidence share the blob.
    Purging one Run must not corrupt the other's evidence trail."""

    from agent_harness.domain.enums import (
        ArtifactMediaKind,
        EvidenceTrustTier,
        RedactionStatus,
        SubjectType,
    )
    from agent_harness.domain.models import EvidenceProvenance, EvidenceRecord
    from agent_harness.persistence.artifacts import blob_path_for_digest, write_blob
    from agent_harness.persistence.sqlite import insert_artifact, insert_evidence_record

    conn = connect(db)
    apply_migrations(conn)

    data_root = tmp_path / "data-root"
    shared_content = b"ok\n"

    run_a = seed_run(db, repository_id="repo-a")
    run_b = seed_run(db, repository_id="repo-b")

    artifact_a = write_blob(
        data_root, shared_content, media_type="text/plain", media_kind=ArtifactMediaKind.TEXT,
        redact=False, now=_utc_now(),
    )
    artifact_b = write_blob(
        data_root, shared_content, media_type="text/plain", media_kind=ArtifactMediaKind.TEXT,
        redact=False, now=_utc_now(),
    )
    assert artifact_a.content_digest == artifact_b.content_digest  # same blob, two Artifact rows
    insert_artifact(conn, artifact_a)
    insert_artifact(conn, artifact_b)

    def make_evidence(run_id: str, artifact: "Artifact") -> "EvidenceRecord":
        return EvidenceRecord(
            run_id=run_id, task_id="task-1", subject_type=SubjectType.COMMAND_RUN, subject_id="cmd-1",
            subject_digest="sha256:" + "0" * 64, kind="command_exit_code",
            provenance=EvidenceProvenance(
                producer_type=ActorType.HOST_TEST_RUNNER, producer_id="host-runner-1",
                collection_method="direct_process_observation", trust_tier=EvidenceTrustTier.HOST_OBSERVED,
            ),
            artifact_refs=[artifact.artifact_id], media_type="text/plain",
            content_digest=artifact.content_digest, size_bytes=artifact.size_bytes, created_at=_utc_now(),
        )

    insert_evidence_record(conn, make_evidence(run_a.run_id, artifact_a))
    insert_evidence_record(conn, make_evidence(run_b.run_id, artifact_b))
    advance(db, run_a.run_id, (LifecycleState.CANCELLED, None))
    conn.close()

    blob_path = blob_path_for_digest(data_root, artifact_a.content_digest)
    assert blob_path.exists()

    result = runner.invoke(
        app,
        [
            "cleanup", run_a.run_id, "--db", str(db), "--data-root", str(data_root),
            "--source-repo", str(tmp_path), "--purge", "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped 1 shared" in result.output
    # Run B's evidence blob must still be there — Run A's purge never touched it.
    assert blob_path.exists()


def test_cleanup_purge_requires_yes(tmp_path, db):
    repo = make_repo(tmp_path, "source-repo")
    data_root = tmp_path / "data-root"
    run = seed_run(db)
    advance(db, run.run_id, (LifecycleState.CANCELLED, None))

    result = runner.invoke(
        app,
        [
            "cleanup", run.run_id, "--db", str(db), "--data-root", str(data_root),
            "--source-repo", str(repo), "--purge",
        ],
    )
    assert result.exit_code == 1
    assert "requires --yes" in result.output
