"""Tests for the retention policy (Phase 13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_harness.domain.enums import ArtifactMediaKind, LifecycleState, RedactionStatus
from agent_harness.domain.models import Artifact
from agent_harness.execution.retention import RetentionPolicy, is_artifact_purge_eligible, is_workspace_cleanup_eligible
from tests.factories import make_run

VALID_DIGEST = "sha256:" + "0" * 64
POLICY = RetentionPolicy(
    active_or_failed_workspace_retention=timedelta(days=7),
    completed_workspace_retention=timedelta(hours=24),
    artifact_blob_retention=timedelta(days=30),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def test_active_run_is_never_cleanup_eligible():
    run = make_run(state=LifecycleState.EXECUTING, updated_at=_utc_now() - timedelta(days=365))
    assert is_workspace_cleanup_eligible(run, now=_utc_now(), policy=POLICY) is False


def test_awaiting_run_is_never_cleanup_eligible():
    run = make_run(state=LifecycleState.AWAITING_APPROVAL, updated_at=_utc_now() - timedelta(days=365))
    assert is_workspace_cleanup_eligible(run, now=_utc_now(), policy=POLICY) is False


def test_failed_run_eligible_only_after_quarantine_window():
    fresh = make_run(state=LifecycleState.FAILED, updated_at=_utc_now() - timedelta(days=1))
    old = make_run(state=LifecycleState.FAILED, updated_at=_utc_now() - timedelta(days=8))
    assert is_workspace_cleanup_eligible(fresh, now=_utc_now(), policy=POLICY) is False
    assert is_workspace_cleanup_eligible(old, now=_utc_now(), policy=POLICY) is True


def test_ready_for_merge_uses_the_shorter_completed_window():
    fresh = make_run(state=LifecycleState.READY_FOR_MERGE, updated_at=_utc_now() - timedelta(hours=1))
    old = make_run(state=LifecycleState.READY_FOR_MERGE, updated_at=_utc_now() - timedelta(hours=25))
    assert is_workspace_cleanup_eligible(fresh, now=_utc_now(), policy=POLICY) is False
    assert is_workspace_cleanup_eligible(old, now=_utc_now(), policy=POLICY) is True


def test_artifact_purge_eligibility_uses_created_at():
    fresh_artifact = Artifact(
        media_type="text/plain", media_kind=ArtifactMediaKind.TEXT, size_bytes=1,
        content_digest=VALID_DIGEST, storage_uri="blob:x", redaction_status=RedactionStatus.NONE,
        created_at=_utc_now() - timedelta(days=1),
    )
    old_artifact = Artifact(
        media_type="text/plain", media_kind=ArtifactMediaKind.TEXT, size_bytes=1,
        content_digest=VALID_DIGEST, storage_uri="blob:x", redaction_status=RedactionStatus.NONE,
        created_at=_utc_now() - timedelta(days=31),
    )
    assert is_artifact_purge_eligible(fresh_artifact, now=_utc_now(), policy=POLICY) is False
    assert is_artifact_purge_eligible(old_artifact, now=_utc_now(), policy=POLICY) is True
