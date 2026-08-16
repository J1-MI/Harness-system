"""Tests for secret incident response (Phase 13)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.domain.enums import ArtifactMediaKind
from agent_harness.execution.incident import quarantine_and_delete_artifact, scan_artifact_for_secrets
from agent_harness.persistence.artifacts import blob_path_for_digest, write_blob


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def test_scan_artifact_for_secrets_detects_a_missed_pattern(tmp_path):
    data_root = tmp_path / "data-root"
    # write_blob(redact=False) simulates a blob that bypassed prevention-side redaction.
    artifact = write_blob(
        data_root, b"token: ghp_" + b"a" * 36, media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT, redact=False, now=_utc_now(),
    )
    assert scan_artifact_for_secrets(data_root, artifact) is True


def test_scan_artifact_for_secrets_false_when_clean(tmp_path):
    data_root = tmp_path / "data-root"
    artifact = write_blob(
        data_root, b"nothing sensitive here", media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT, redact=False, now=_utc_now(),
    )
    assert scan_artifact_for_secrets(data_root, artifact) is False


def test_quarantine_deletes_blob_and_writes_tombstone(tmp_path):
    data_root = tmp_path / "data-root"
    artifact = write_blob(
        data_root, b"token: ghp_" + b"a" * 36, media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT, redact=False, now=_utc_now(),
    )
    blob_path = blob_path_for_digest(data_root, artifact.content_digest)
    assert blob_path.exists()

    tombstone_path = quarantine_and_delete_artifact(
        data_root, artifact, now=_utc_now(), reason="manual report: leaked github token"
    )

    assert not blob_path.exists()
    assert tombstone_path.exists()
    payload = json.loads(tombstone_path.read_text())
    assert payload["incident"] is True
    assert payload["content_digest"] == artifact.content_digest
    assert "leaked github token" in payload["reason"]
