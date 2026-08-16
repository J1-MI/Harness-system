"""Tests for the content-addressed blob store and artifact/evidence metadata."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import ArtifactMediaKind, RedactionStatus, SubjectType
from agent_harness.domain.models import EvidenceProvenance
from agent_harness.domain.validation import InvariantViolation, assert_evidence_matches_artifact
from agent_harness.persistence.artifacts import (
    ArtifactQuotaExceededError,
    CorruptedArtifactError,
    blob_path_for_digest,
    read_blob,
    write_blob,
)
from agent_harness.persistence.sqlite import (
    connect,
    get_artifact,
    get_context_snapshot,
    get_evidence_record,
    insert_artifact,
    insert_context_snapshot,
    insert_evidence_record,
    insert_run,
    list_evidence_for_run,
)
from tests.factories import VALID_DIGEST, make_run


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def _blob_root(tmp_path):
    return tmp_path / "data-root"


def _count_blob_files(data_root) -> int:
    blobs_dir = data_root / "blobs"
    if not blobs_dir.exists():
        return 0
    return sum(1 for p in blobs_dir.rglob("*") if p.is_file())


def _staging_files(data_root) -> list:
    staging_dir = data_root / "staging"
    if not staging_dir.exists():
        return []
    return list(staging_dir.iterdir())


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_write_blob_creates_content_addressed_file(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root,
        b"hello world",
        media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT,
        redact=False,
    )
    path = blob_path_for_digest(data_root, artifact.content_digest)
    assert path.exists()
    assert path.read_bytes() == b"hello world"
    assert artifact.size_bytes == len(b"hello world")


def test_write_blob_leaves_no_staging_files_behind(tmp_path):
    data_root = _blob_root(tmp_path)
    write_blob(
        data_root, b"content", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    assert _staging_files(data_root) == []


def test_read_blob_round_trips_exact_bytes(tmp_path):
    data_root = _blob_root(tmp_path)
    original = b"\x00\x01binary-ish content\xff"
    artifact = write_blob(
        data_root,
        original,
        media_type="application/octet-stream",
        media_kind=ArtifactMediaKind.BINARY,
    )
    assert read_blob(data_root, artifact) == original


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_write_blob_dedups_identical_content(tmp_path):
    data_root = _blob_root(tmp_path)
    first = write_blob(
        data_root, b"same bytes", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    second = write_blob(
        data_root, b"same bytes", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    assert first.content_digest == second.content_digest
    assert _count_blob_files(data_root) == 1


def test_write_blob_different_content_creates_separate_blobs(tmp_path):
    data_root = _blob_root(tmp_path)
    write_blob(data_root, b"one", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT)
    write_blob(data_root, b"two", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT)
    assert _count_blob_files(data_root) == 2


# ---------------------------------------------------------------------------
# Corrupted digest detection
# ---------------------------------------------------------------------------


def test_read_blob_detects_tampering(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"original", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    path = blob_path_for_digest(data_root, artifact.content_digest)
    path.write_bytes(b"tampered!")

    with pytest.raises(CorruptedArtifactError):
        read_blob(data_root, artifact)


def test_read_blob_raises_for_missing_blob(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"will be deleted", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    blob_path_for_digest(data_root, artifact.content_digest).unlink()

    with pytest.raises(FileNotFoundError):
        read_blob(data_root, artifact)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def test_write_blob_rejects_content_over_quota(tmp_path):
    data_root = _blob_root(tmp_path)
    with pytest.raises(ArtifactQuotaExceededError):
        write_blob(
            data_root,
            b"x" * 100,
            media_type="text/plain",
            media_kind=ArtifactMediaKind.TEXT,
            max_size_bytes=10,
        )
    # A rejected write must not leave any bytes on disk anywhere.
    assert _count_blob_files(data_root) == 0
    assert _staging_files(data_root) == []


def test_write_blob_accepts_content_at_exactly_the_quota(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root,
        b"x" * 10,
        media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT,
        max_size_bytes=10,
    )
    assert artifact.size_bytes == 10


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_write_blob_redacts_known_secret_patterns(tmp_path):
    data_root = _blob_root(tmp_path)
    content = b"api_key = sk-abcdefghijklmnopqrstuvwxyz123456"
    artifact = write_blob(
        data_root, content, media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    assert artifact.redaction_status is RedactionStatus.REDACTED
    stored = read_blob(data_root, artifact)
    assert b"sk-abcdefghijklmnopqrstuvwxyz123456" not in stored
    assert b"[REDACTED]" in stored


def test_write_blob_does_not_redact_binary_content(tmp_path):
    data_root = _blob_root(tmp_path)
    secret_like = b"sk-abcdefghijklmnopqrstuvwxyz123456"
    artifact = write_blob(
        data_root,
        secret_like,
        media_type="application/octet-stream",
        media_kind=ArtifactMediaKind.BINARY,
    )
    assert artifact.redaction_status is RedactionStatus.NONE
    assert read_blob(data_root, artifact) == secret_like


def test_write_blob_leaves_clean_content_unredacted(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"nothing sensitive here", media_type="text/plain",
        media_kind=ArtifactMediaKind.TEXT,
    )
    assert artifact.redaction_status is RedactionStatus.NONE


# ---------------------------------------------------------------------------
# SQLite metadata round trips
# ---------------------------------------------------------------------------


def test_artifact_metadata_round_trips_through_sqlite(conn, tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"metadata test", media_type="text/plain", media_kind=ArtifactMediaKind.TEXT
    )
    insert_artifact(conn, artifact)
    assert get_artifact(conn, artifact.artifact_id) == artifact


def test_context_snapshot_round_trips_through_sqlite(conn):
    from agent_harness.domain.models import ContextSnapshot, ProvidedFileRef

    run = make_run()
    insert_run(conn, run)
    snapshot = ContextSnapshot(
        run_id=run.run_id,
        task_id=new_id(),
        prompt_template_digest=VALID_DIGEST,
        contract_ref="contract-ref-1",
        contract_digest=VALID_DIGEST,
        provided_files=[ProvidedFileRef(path="src/main.py", content_digest=VALID_DIGEST)],
        schema_digest=VALID_DIGEST,
        provider_id="fake-provider",
        provider_version="0.1.0",
        role_profile_digest=VALID_DIGEST,
        environment_variable_names=["PATH", "HOME"],
        created_at=_utc_now(),
    )
    insert_context_snapshot(conn, snapshot)
    assert get_context_snapshot(conn, snapshot.context_snapshot_id) == snapshot


def test_evidence_record_round_trips_through_sqlite(conn):
    run = make_run()
    insert_run(conn, run)
    evidence = EvidenceProvenance(
        producer_type="HOST_TEST_RUNNER",
        producer_id="host-runner-1",
        collection_method="direct_process_observation",
        trust_tier="HOST_OBSERVED",
    )
    from agent_harness.domain.models import EvidenceRecord

    record = EvidenceRecord(
        run_id=run.run_id,
        task_id=new_id(),
        subject_type=SubjectType.COMMAND_RUN,
        subject_id=new_id(),
        subject_digest=VALID_DIGEST,
        kind="command_exit_code",
        provenance=evidence,
        artifact_refs=["artifact-1"],
        media_type="application/json",
        content_digest=VALID_DIGEST,
        size_bytes=42,
        created_at=_utc_now(),
    )
    insert_evidence_record(conn, record)
    assert get_evidence_record(conn, record.evidence_id) == record
    assert [e.evidence_id for e in list_evidence_for_run(conn, run.run_id)] == [
        record.evidence_id
    ]


# ---------------------------------------------------------------------------
# Evidence <-> Artifact integrity validator
# ---------------------------------------------------------------------------


def _make_evidence(run_id: str, *, artifact_refs: list[str], content_digest: str):
    from agent_harness.domain.models import EvidenceRecord

    return EvidenceRecord(
        run_id=run_id,
        task_id=new_id(),
        subject_type=SubjectType.COMMAND_RUN,
        subject_id=new_id(),
        subject_digest=VALID_DIGEST,
        kind="command_exit_code",
        provenance=EvidenceProvenance(
            producer_type="HOST_TEST_RUNNER",
            producer_id="host-runner-1",
            collection_method="direct_process_observation",
            trust_tier="HOST_OBSERVED",
        ),
        artifact_refs=artifact_refs,
        media_type="application/json",
        content_digest=content_digest,
        size_bytes=10,
        created_at=_utc_now(),
    )


def test_evidence_matches_artifact_accepts_consistent_pair(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"evidence bytes", media_type="application/json", media_kind=ArtifactMediaKind.JSON
    )
    evidence = _make_evidence(
        run_id=new_id(),
        artifact_refs=[artifact.artifact_id],
        content_digest=artifact.content_digest,
    )
    assert_evidence_matches_artifact(evidence, artifact)  # must not raise


def test_evidence_matches_artifact_rejects_digest_mismatch(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"evidence bytes", media_type="application/json", media_kind=ArtifactMediaKind.JSON
    )
    evidence = _make_evidence(
        run_id=new_id(), artifact_refs=[artifact.artifact_id], content_digest=VALID_DIGEST
    )
    with pytest.raises(InvariantViolation):
        assert_evidence_matches_artifact(evidence, artifact)


def test_evidence_matches_artifact_rejects_missing_artifact_ref(tmp_path):
    data_root = _blob_root(tmp_path)
    artifact = write_blob(
        data_root, b"evidence bytes", media_type="application/json", media_kind=ArtifactMediaKind.JSON
    )
    evidence = _make_evidence(
        run_id=new_id(), artifact_refs=["some-other-artifact"], content_digest=artifact.content_digest
    )
    with pytest.raises(InvariantViolation):
        assert_evidence_matches_artifact(evidence, artifact)
