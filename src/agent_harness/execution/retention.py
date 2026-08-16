"""Retention policy (Phase 13): when a Run's workspace/artifacts become
eligible for ``cleanup``/``purge`` (Phase 12's CLI commands).

Architecture review "## 데이터 보존·삭제": distinguishes ``cleanup``
(process/worktree/temp/branch) from ``purge`` (evidence/artifact/journal
data), and gives recommended defaults this module encodes directly:

    active/failed quarantine workspace: 7 days
    completed workspace: immediately, or 24 hours after final approval
    artifact blob: 30 days
    journal metadata and digests: kept until an explicit purge
    secret-contaminated artifact: immediate quarantine, then deletion
      (see ``execution.incident`` — that path ignores this policy
      entirely and always acts immediately)

Pure policy functions only — this module makes no filesystem or database
calls itself. ``interfaces/cli.py``'s ``cleanup``/``report`` commands (or
a future scheduled sweep) are the callers that act on these decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent_harness.domain.enums import LifecycleState
from agent_harness.domain.models import Artifact, Run

__all__ = ["RetentionPolicy", "DEFAULT_RETENTION_POLICY", "is_workspace_cleanup_eligible", "is_artifact_purge_eligible"]


@dataclass(frozen=True)
class RetentionPolicy:
    active_or_failed_workspace_retention: timedelta = timedelta(days=7)
    completed_workspace_retention: timedelta = timedelta(hours=24)
    artifact_blob_retention: timedelta = timedelta(days=30)


DEFAULT_RETENTION_POLICY = RetentionPolicy()

# Runs whose workspace is eligible for cleanup once old enough — matches
# "active/failed quarantine workspace" (FAILED/CANCELLED/RECOVERY_REQUIRED
# all land a Run in a quarantined-but-not-cleaned-up state) plus
# READY_FOR_MERGE (the "completed" case, on its own shorter clock).
_QUARANTINE_STATES = frozenset(
    {LifecycleState.FAILED, LifecycleState.CANCELLED, LifecycleState.RECOVERY_REQUIRED}
)


def is_workspace_cleanup_eligible(run: Run, *, now: datetime, policy: RetentionPolicy = DEFAULT_RETENTION_POLICY) -> bool:
    """Whether ``run``'s workspace has aged past its retention window.

    A Run still active or awaiting a human is never eligible, regardless
    of age — only a Run that has actually reached one of the states this
    function recognizes can be cleaned up at all.
    """

    age = now - run.updated_at
    if run.state is LifecycleState.READY_FOR_MERGE:
        return age >= policy.completed_workspace_retention
    if run.state in _QUARANTINE_STATES:
        return age >= policy.active_or_failed_workspace_retention
    return False


def is_artifact_purge_eligible(artifact: Artifact, *, now: datetime, policy: RetentionPolicy = DEFAULT_RETENTION_POLICY) -> bool:
    return (now - artifact.created_at) >= policy.artifact_blob_retention
