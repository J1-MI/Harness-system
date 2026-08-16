"""Recovery Coordinator (Phase 13): reconcile stale active Runs at startup.

Architecture review, "## Control Plane": "Recovery Coordinator: lease
만료, orphan process, 부분 artifact를 조정한다." Section "## 재시작 및
복구" lists what a startup scan should do; "Provider invocation 자체를
자동 재실행하지 않는다" is the one hard rule this module obeys
everywhere — it only ever moves a Run's *state machine* position, never
re-invokes a Planner/Worker/Verifier call on a stale Run's behalf.

Scope actually implemented here, and why it stops where it does:

- **Stale-Run detection + reconciliation** via ``run_recovery_scan`` —
  fully implemented, using only what Phase 2.1 already persists
  (``Run.state``/``updated_at``).
- **``BASE_REVISION_STALE`` detection** via ``check_base_revision_stale``
  — a standalone, pure utility. It is *not* wired into ``run_recovery_scan``
  because the ``TaskContract`` a Run was built from (its
  ``repository.base_commit_sha``/``target_ref``) is not persisted
  anywhere in this codebase (the same gap Phase 12's docs already flag
  for CLI resume) — there is currently nothing durable to compare a
  stale Run's original base revision against. A caller that still has
  the in-memory ``TaskContract`` (e.g. mid-``run_task_pipeline``, before
  a crash) can call this function directly.
- **Orphan child-process termination** is explicitly *not* implemented.
  Doing that for real needs a durable PID + process-start-time +
  Job Object/process-group identity marker written at invocation start
  (per "PID뿐 아니라 process start time과 job/process-group identity
  확인") and a table to persist it in — neither exists yet. Claiming to
  kill orphans without that would be exactly the kind of silent-downgrade
  risk this codebase avoids elsewhere (Phase 3.2's ``UnavailableSandboxError``,
  Phase 13's own Docker-availability probe). A future phase should add an
  ``invocation_leases`` table before this gap can honestly be closed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from agent_harness.application.transitions import PendingAction
from agent_harness.domain.enums import ActorType, FailureCategory, FailureCode, LifecycleState, PendingActionKind
from agent_harness.domain.models import FailureRecord, RepositoryRef, Run
from agent_harness.execution.git_client import GitClient
from agent_harness.persistence.sqlite import apply_transition, list_runs_by_states

__all__ = [
    "RECOVERABLE_STATES",
    "STALE_NO_RECOVERY_PATH_STATES",
    "RecoveryOutcome",
    "RecoveryReport",
    "run_recovery_scan",
    "check_base_revision_stale",
]

# The four states that involve a real host-side resource (worktree,
# subprocess, provider session) — Phase 1.2's own transition table only
# allows RECOVERY_REQUIRED from these, never from a pure in-memory/API-call
# state. This module respects that table rather than trying to route
# around it.
RECOVERABLE_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.PREPARING_WORKSPACE,
        LifecycleState.EXECUTING,
        LifecycleState.FREEZING_RESULT,
        LifecycleState.HOST_VALIDATING,
    }
)

# Pure in-memory/API-call states with no host-side lease to reconcile —
# Phase 1.2's transition table has no RECOVERY_REQUIRED path from any of
# these, so a stale Run here is simply marked FAILED (retriable=True: a
# *new* Run/attempt can retry, this one just did not finish).
STALE_NO_RECOVERY_PATH_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.PLANNING,
        LifecycleState.CONTRACT_VALIDATING,
        LifecycleState.VERIFYING,
        LifecycleState.REWORK_CONTRACTING,
    }
)


@dataclass(frozen=True)
class RecoveryOutcome:
    run_id: str
    stale_state: LifecycleState
    action: str  # "RECOVERY_REQUIRED" or "FAILED"
    resulting_run: Run


@dataclass
class RecoveryReport:
    scanned_at: datetime
    outcomes: list[RecoveryOutcome] = field(default_factory=list)


def run_recovery_scan(
    conn: sqlite3.Connection, *, now: datetime, stale_after: timedelta = timedelta(minutes=30)
) -> RecoveryReport:
    """Find every active Run that has not been touched in over
    ``stale_after`` and reconcile its state machine position.

    "불명확하면 RECOVERY_REQUIRED": a stale Run in one of
    ``RECOVERABLE_STATES`` always goes to ``RECOVERY_REQUIRED`` — this
    function never guesses that a worktree/process is actually fine and
    lets the Run continue on its own. A stale Run in
    ``STALE_NO_RECOVERY_PATH_STATES`` goes to ``FAILED`` with a
    ``retriable=True`` FailureRecord, since the state machine itself has
    no other legal destination from there.
    """

    outcomes: list[RecoveryOutcome] = []
    cutoff = now - stale_after
    candidate_states = list(RECOVERABLE_STATES | STALE_NO_RECOVERY_PATH_STATES)

    for run in list_runs_by_states(conn, candidate_states):
        if run.updated_at > cutoff:
            continue  # recently touched — a process may genuinely still be working on it

        stale_state = run.state
        if stale_state in RECOVERABLE_STATES:
            outcome_run = apply_transition(
                conn, run.run_id, LifecycleState.RECOVERY_REQUIRED, now=now,
                expected_state_version=run.state_version, actor_type=ActorType.HARNESS,
                actor_id="recovery-coordinator", correlation_id=f"recovery-scan-{run.run_id}",
                pending_action=PendingAction(
                    kind=PendingActionKind.RECOVERY_DECISION,
                    description=(
                        f"stale in {stale_state.value} since {run.updated_at.isoformat()} "
                        f"(no update for over {stale_after}); needs manual reconciliation of "
                        "worktree/process state before resuming"
                    ),
                    requested_at=now,
                ),
            ).run
            outcomes.append(
                RecoveryOutcome(run_id=run.run_id, stale_state=stale_state, action="RECOVERY_REQUIRED", resulting_run=outcome_run)
            )
        else:
            outcome_run = apply_transition(
                conn, run.run_id, LifecycleState.FAILED, now=now,
                expected_state_version=run.state_version, actor_type=ActorType.HARNESS,
                actor_id="recovery-coordinator", correlation_id=f"recovery-scan-{run.run_id}",
                failure=FailureRecord(
                    run_id=run.run_id, stage=stale_state.value, code=FailureCode.RUN_TIMEOUT,
                    category=FailureCategory.INFRASTRUCTURE, retriable=True,
                    sanitized_detail=(
                        f"stale in {stale_state.value} since {run.updated_at.isoformat()} with no "
                        f"host-side lease to reconcile; no automatic retry — start a new Run"
                    ),
                    occurred_at=now,
                ),
            ).run
            outcomes.append(
                RecoveryOutcome(run_id=run.run_id, stale_state=stale_state, action="FAILED", resulting_run=outcome_run)
            )

    return RecoveryReport(scanned_at=now, outcomes=outcomes)


def check_base_revision_stale(git_client: GitClient, source_repo_path: Path, repository_ref: RepositoryRef) -> bool:
    """True if ``repository_ref.target_ref`` now resolves to a different
    commit than ``repository_ref.base_commit_sha`` — i.e. the branch moved
    since this Run's TaskContract was accepted.

    Per H-08 / attack surface #12: never auto-rebase onto the new tip.
    The caller is expected to route a ``True`` result to
    ``FailureCode.BASE_REVISION_STALE`` and manual review, not silently
    proceed against the old pin or silently adopt the new tip.
    """

    current_sha = git_client.rev_parse(repository_ref.target_ref, cwd=source_repo_path)
    return current_sha != repository_ref.base_commit_sha
