"""Harness CLI (Phase 12): the operational interface, per roadmap row 12.

Scope: ``run/status/approve/reject/resume/cancel/report/cleanup`` + final
manifest. Excludes: a Web UI. Built with Typer (per the review's own tech
conclusion: "CLI: Typer 적합, 내부에서 `asyncio.run`").

Every command below opens its own short-lived SQLite connection, performs
exactly one bounded, already-atomic operation (``apply_transition``,
``build_final_manifest``, or a single filesystem cleanup step), and exits.
There is no long-lived in-process state a crash could lose - that is what
"crash-safe commands" (roadmap row 12's test criterion) means here: if the
process dies mid-command, either the one atomic transaction committed or
it didn't (Phase 2.1's ``BEGIN IMMEDIATE`` semantics), never something
in between.

"ambiguous approval 방지" (prevent ambiguous approval): ``approve``/
``reject`` refuse to act unless the Run is *actually* sitting in one of
the three ``AWAITING_*`` states - never inferred, never silently assumed
- and print exactly what pending action is being resolved before doing
anything, sourced from the last journal entry's own recorded
``PendingAction`` (Phase 1.2/2.1), not re-derived or guessed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import typer

from agent_harness.application.reporting import RunNotFoundForReportError, build_final_manifest
from agent_harness.application.transitions import (
    PENDING_ACTION_REQUIRED_STATES,
    RECOVERY_RESTART_TARGETS,
    PendingAction,
)
from agent_harness.domain.enums import (
    ActorType,
    FailureCategory,
    FailureCode,
    LifecycleState,
    PendingActionKind,
    TERMINAL_LIFECYCLE_STATES,
)
from agent_harness.domain.models import FailureRecord
from agent_harness.execution.git_client import GitClient, GitCommandError
from agent_harness.execution.workspace import branch_ref_for, cleanup_worktree, worktree_path_for
from agent_harness.persistence.artifacts import blob_path_for_digest
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import (
    apply_transition,
    connect,
    count_artifacts_with_content_digest,
    get_artifact,
    get_run,
    list_evidence_for_run,
)

app = typer.Typer(add_completion=False, help="Agent Harness operational CLI.")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _open(db: Path) -> sqlite3.Connection:
    conn = connect(db)
    apply_migrations(conn)
    return conn


def _fail_cli(message: str) -> "typer.Exit":
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=1)


def _require_run(conn: sqlite3.Connection, run_id: str):
    run = get_run(conn, run_id)
    if run is None:
        raise _fail_cli(f"no Run found with run_id={run_id!r}")
    return run


DB_OPTION = typer.Option(Path("harness.db"), "--db", help="Path to the harness SQLite database.")
DATA_ROOT_OPTION = typer.Option(Path("harness-data"), "--data-root", help="Path to the harness data root.")


@app.command()
def status(run_id: str, db: Path = DB_OPTION) -> None:
    """Show a Run's current state, budget usage, and any pending action."""

    conn = _open(db)
    try:
        run = _require_run(conn, run_id)
        typer.echo(f"run_id:        {run.run_id}")
        typer.echo(f"state:         {run.state.value}")
        typer.echo(f"disposition:   {run.disposition.value if run.disposition else '(none)'}")
        typer.echo(f"state_version: {run.state_version}")
        typer.echo(
            "budget_used:   turns={t} tokens={tok} cost_usd={c} rework={r}".format(
                t=run.budget_used.turns_used, tok=run.budget_used.tokens_used,
                c=run.budget_used.cost_used_usd, r=run.budget_used.rework_used,
            )
        )
        if run.state in PENDING_ACTION_REQUIRED_STATES:
            from agent_harness.persistence.sqlite import list_journal_entries

            entries = list_journal_entries(conn, run_id)
            pending = entries[-1].payload_json.get("pending_action") if entries else None
            if pending:
                typer.echo(f"pending:       {pending['kind']} - {pending['description']}")
    finally:
        conn.close()


_AWAITING_APPROVE_TARGET = {
    LifecycleState.AWAITING_APPROVAL: LifecycleState.PREPARING_WORKSPACE,
    LifecycleState.AWAITING_FINAL_APPROVAL: LifecycleState.READY_FOR_MERGE,
    LifecycleState.AWAITING_MANUAL_REVIEW: LifecycleState.AWAITING_FINAL_APPROVAL,
}
_AWAITING_REJECT_TARGET = {
    LifecycleState.AWAITING_APPROVAL: LifecycleState.FAILED,
    LifecycleState.AWAITING_FINAL_APPROVAL: LifecycleState.REWORK_CONTRACTING,
    LifecycleState.AWAITING_MANUAL_REVIEW: LifecycleState.REWORK_CONTRACTING,
}


def _resolve_awaiting(run_id: str, db: Path, *, approved: bool, rationale: str) -> None:
    conn = _open(db)
    try:
        run = _require_run(conn, run_id)
        if run.state not in _AWAITING_APPROVE_TARGET:
            raise _fail_cli(
                f"Run {run_id} is not awaiting approval (state={run.state.value}) - "
                "refusing to guess what you meant to approve/reject"
            )

        from agent_harness.persistence.sqlite import list_journal_entries

        entries = list_journal_entries(conn, run_id)
        pending = entries[-1].payload_json.get("pending_action") if entries else None
        typer.echo(
            f"resolving pending action for Run {run_id} (state={run.state.value}): "
            f"{pending['description'] if pending else '(no recorded description)'}"
        )

        target = (_AWAITING_APPROVE_TARGET if approved else _AWAITING_REJECT_TARGET)[run.state]
        pending_action = None
        failure = None
        if target in PENDING_ACTION_REQUIRED_STATES:
            pending_action = PendingAction(
                kind=PendingActionKind.FINAL_APPROVAL, description=rationale, requested_at=_utc_now(),
            )
        if target is LifecycleState.FAILED:
            failure = FailureRecord(
                run_id=run_id, stage="APPROVAL", code=FailureCode.POLICY_DENIED,
                category=FailureCategory.DOMAIN, retriable=False, sanitized_detail=rationale,
                occurred_at=_utc_now(),
            )

        outcome = apply_transition(
            conn, run_id, target, now=_utc_now(), expected_state_version=run.state_version,
            actor_type=ActorType.USER, actor_id="cli", correlation_id=f"cli-approve-{run_id}",
            pending_action=pending_action, failure=failure,
        )
        typer.echo(f"-> {outcome.run.state.value}")
    finally:
        conn.close()


@app.command()
def approve(
    run_id: str,
    db: Path = DB_OPTION,
    rationale: str = typer.Option("approved via CLI", "--rationale"),
) -> None:
    """Approve whatever a Run is currently awaiting. Refuses if the Run
    isn't actually in an AWAITING_* state (ambiguous-approval prevention)."""

    _resolve_awaiting(run_id, db, approved=True, rationale=rationale)


@app.command()
def reject(
    run_id: str,
    db: Path = DB_OPTION,
    rationale: str = typer.Option("rejected via CLI", "--rationale"),
) -> None:
    """Reject whatever a Run is currently awaiting. Same refusal rule as
    ``approve``."""

    _resolve_awaiting(run_id, db, approved=False, rationale=rationale)


@app.command()
def resume(
    run_id: str,
    to: str = typer.Option(..., "--to", help="Target LifecycleState to resume into."),
    db: Path = DB_OPTION,
) -> None:
    """Resume a Run stuck in RECOVERY_REQUIRED into one of the states the
    state machine recognizes as safe to restart from."""

    conn = _open(db)
    try:
        run = _require_run(conn, run_id)
        if run.state is not LifecycleState.RECOVERY_REQUIRED:
            raise _fail_cli(f"Run {run_id} is not in RECOVERY_REQUIRED (state={run.state.value})")
        try:
            target = LifecycleState(to)
        except ValueError:
            raise _fail_cli(f"{to!r} is not a valid LifecycleState")
        if target not in RECOVERY_RESTART_TARGETS:
            valid = ", ".join(sorted(s.value for s in RECOVERY_RESTART_TARGETS))
            raise _fail_cli(f"{to!r} is not a safe recovery target - valid targets: {valid}")

        pending_action = None
        if target in PENDING_ACTION_REQUIRED_STATES:
            pending_action = PendingAction(
                kind=PendingActionKind.RECOVERY_DECISION,
                description=f"resumed from RECOVERY_REQUIRED into {target.value} via CLI",
                requested_at=_utc_now(),
            )
        outcome = apply_transition(
            conn, run_id, target, now=_utc_now(), expected_state_version=run.state_version,
            actor_type=ActorType.USER, actor_id="cli", correlation_id=f"cli-resume-{run_id}",
            pending_action=pending_action,
        )
        typer.echo(f"-> {outcome.run.state.value}")
    finally:
        conn.close()


@app.command()
def cancel(
    run_id: str,
    db: Path = DB_OPTION,
    data_root: Path = DATA_ROOT_OPTION,
    source_repo: Path | None = typer.Option(None, "--source-repo", help="Enables best-effort worktree cleanup."),
) -> None:
    """Cancel a non-terminal Run. Best-effort worktree cleanup if
    --source-repo is given; a missing worktree (e.g. cancelled before
    PREPARING_WORKSPACE) is not an error."""

    conn = _open(db)
    try:
        run = _require_run(conn, run_id)
        if run.state in TERMINAL_LIFECYCLE_STATES:
            raise _fail_cli(f"Run {run_id} is already terminal (state={run.state.value})")

        outcome = apply_transition(
            conn, run_id, LifecycleState.CANCELLED, now=_utc_now(),
            expected_state_version=run.state_version, actor_type=ActorType.USER,
            actor_id="cli", correlation_id=f"cli-cancel-{run_id}",
        )
        typer.echo(f"-> {outcome.run.state.value}")

        if source_repo is not None:
            _best_effort_worktree_cleanup(run.run_id, run.repository_id, data_root, source_repo)
    finally:
        conn.close()


def _best_effort_worktree_cleanup(run_id: str, repository_id: str, data_root: Path, source_repo: Path) -> None:
    from agent_harness.domain.models import WorkspaceLease

    worktree_path = worktree_path_for(data_root, repository_id=repository_id, run_id=run_id)
    if not worktree_path.exists():
        typer.echo("(no worktree to clean up)")
        return
    lease = WorkspaceLease(
        repository_id=repository_id, worktree_path_handle=str(worktree_path),
        base_commit_sha="0" * 40, branch=branch_ref_for(repository_id=repository_id, run_id=run_id),
        owner=run_id, heartbeat_at=_utc_now(), sandbox_profile="trusted_local", created_at=_utc_now(),
    )
    try:
        cleanup_worktree(GitClient(data_root / "empty-hooks"), source_repo_path=source_repo, lease=lease, force=True)
        typer.echo("worktree removed")
    except GitCommandError as exc:
        typer.echo(f"(worktree cleanup failed, left in place: {exc})", err=True)


@app.command()
def cleanup(
    run_id: str,
    db: Path = DB_OPTION,
    data_root: Path = DATA_ROOT_OPTION,
    source_repo: Path = typer.Option(..., "--source-repo"),
    purge: bool = typer.Option(False, "--purge", help="Also delete evidence blobs (irreversible)."),
    yes: bool = typer.Option(False, "--yes", help="Required with --purge; skips the confirmation prompt."),
) -> None:
    """Remove a terminal Run's worktree/branch. ``--purge`` additionally
    deletes its evidence blobs, leaving a tombstone - never done without
    ``--yes``, matching the review's cleanup/purge distinction."""

    conn = _open(db)
    try:
        run = _require_run(conn, run_id)
        if run.state not in TERMINAL_LIFECYCLE_STATES:
            raise _fail_cli(f"Run {run_id} is not terminal (state={run.state.value}) - cancel it first")

        _best_effort_worktree_cleanup(run.run_id, run.repository_id, data_root, source_repo)

        if purge:
            if not yes:
                raise _fail_cli("--purge requires --yes (irreversible: deletes evidence blobs)")
            _purge_evidence(conn, data_root, run_id)
    finally:
        conn.close()


def _purge_evidence(conn: sqlite3.Connection, data_root: Path, run_id: str) -> None:
    purged = 0
    skipped_shared = 0
    for record in list_evidence_for_run(conn, run_id):
        for artifact_id in record.artifact_refs:
            artifact = get_artifact(conn, artifact_id)
            if artifact is None:
                continue
            # write_blob dedups identical bytes to one file on disk, but a
            # new Artifact row is inserted every time — so this content
            # digest may still be referenced by another Artifact (this
            # Run's own, or a completely different Run's) even after this
            # one row is gone. Only unlink the blob when this is the only
            # row pointing at it (Codex review M-04) — conservative by
            # design, so a same-run duplicate-digest pair may occasionally
            # leave the blob behind rather than risk deleting bytes a
            # sibling reference still needs.
            if count_artifacts_with_content_digest(conn, artifact.content_digest) > 1:
                skipped_shared += 1
                continue
            blob_path = blob_path_for_digest(data_root, artifact.content_digest)
            if blob_path.exists():
                tombstone = blob_path.with_suffix(blob_path.suffix + ".tombstone")
                tombstone.write_text(
                    json.dumps({"purged_at": _utc_now().isoformat(), "content_digest": artifact.content_digest})
                )
                blob_path.unlink()
                purged += 1
    typer.echo(
        f"purged {purged} evidence blob(s), skipped {skipped_shared} shared with another Artifact "
        "reference; metadata/digests retained in the journal"
    )


@app.command()
def report(
    run_id: str,
    db: Path = DB_OPTION,
    out: Path | None = typer.Option(None, "--out", help="Write the manifest JSON here instead of stdout."),
) -> None:
    """Build and print (or write) the final manifest for a Run."""

    conn = _open(db)
    try:
        try:
            manifest = build_final_manifest(conn, run_id, generated_at=_utc_now())
        except RunNotFoundForReportError as exc:
            raise _fail_cli(str(exc))
        payload = json.dumps(manifest.model_dump(mode="json"), indent=2)
        if out is not None:
            out.write_text(payload)
            typer.echo(f"wrote {out}")
        else:
            typer.echo(payload)
    finally:
        conn.close()


@app.command(name="demo")
def demo_command(
    repo: Path,
    request: str,
    db: Path = DB_OPTION,
    data_root: Path = DATA_ROOT_OPTION,
    auto_approve: bool = typer.Option(
        False, "--auto-approve", help="Skip interactive approval prompts (used by tests/demos)."
    ),
) -> None:
    """Run the dual-agent pipeline once against a real local git repo,
    using FakeAgentProvider for all three roles (Codex review B-06).

    This is a demo/plumbing-proof command, not real automation: no code
    is actually written by an LLM — the Planner/Worker/Verifier
    structured outputs are deterministic canned responses, just routed
    through the real orchestrator so this proves the CLI is wired to the
    real pipeline, not a parallel mock path. There is no ``harness run``
    command in this phase: a real one needs a config loader for
    Claude/Codex credentials and policy/command-catalog config that does
    not exist anywhere in this codebase yet (see
    docs/architecture/cli-and-reporting.md) — shipping a command literally
    named ``run`` that silently only ever uses fakes would be exactly the
    kind of overclaim this review flagged. Approvals are interactive
    terminal prompts unless --auto-approve is given.
    """

    import asyncio

    from agent_harness.interfaces._demo_pipeline import run_demo_pipeline

    conn = _open(db)
    try:
        final_run = asyncio.run(
            run_demo_pipeline(
                conn, data_root=data_root, source_repo=repo, request_text=request,
                auto_approve=auto_approve, confirm=typer.confirm,
            )
        )
        typer.echo(f"-> {final_run.state.value}")
    finally:
        conn.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
