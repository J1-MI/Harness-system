"""Content-addressed blob store (Phase 2.2).

Layout under ``data_root`` (matches architecture review section 10):

```
<data-root>/
  blobs/sha256/<first-2-hex>/<64-hex>
  staging/<random-id>.tmp
```

Write order, matching section 10 exactly: write to staging -> redact ->
enforce size cap -> flush/fsync -> compute digest -> atomic rename into
``blobs/`` on the same volume -> return an ``Artifact`` for the caller to
persist via ``persistence.sqlite.insert_artifact``. Nothing here touches
the SQLite metadata tables — this module only knows about bytes on disk.

Only ``persistence.sqlite`` (Run/Task/journal/artifact *metadata*) and this
module (blob *bytes*) exist in Phase 2.2. Actual Git/test execution that
would produce real evidence is still out of scope (Phase 3.x).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.domain.digests import compute_digest, new_id
from agent_harness.domain.enums import ArtifactMediaKind, RedactionStatus
from agent_harness.domain.models import Artifact

__all__ = [
    "ArtifactQuotaExceededError",
    "CorruptedArtifactError",
    "write_blob",
    "read_blob",
    "blob_path_for_digest",
    "contains_secret_pattern",
]


class ArtifactQuotaExceededError(ValueError):
    """Raised when content exceeds ``max_size_bytes``.

    Deliberately fails closed instead of silently truncating — section 10:
    "조용히 잘라내고 PASS해서는 안 된다" (never silently truncate and pass).
    """


class CorruptedArtifactError(ValueError):
    """Raised when a blob's on-disk bytes no longer match its recorded digest."""


# Coarse patterns for common secret shapes. This is a best-effort redactor,
# not a substitute for never putting secrets in the workspace in the first
# place — see section 8's "자격증명에 대한 특별 원칙".
_SECRET_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{36}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----", re.DOTALL),
)

_REDACTABLE_KINDS = frozenset(
    {ArtifactMediaKind.TEXT, ArtifactMediaKind.JSON, ArtifactMediaKind.LOG, ArtifactMediaKind.DIFF}
)


def _redact(content: bytes) -> tuple[bytes, bool]:
    redacted = content
    changed = False
    for pattern in _SECRET_PATTERNS:
        new_content, count = pattern.subn(b"[REDACTED]", redacted)
        if count:
            redacted = new_content
            changed = True
    return redacted, changed


def contains_secret_pattern(content: bytes) -> bool:
    """Re-usable for Phase 13's incident-response re-scan
    (``execution.incident``) — the same coarse pattern set ``write_blob``
    already applies at write time, exposed so a later re-scan of an
    already-stored blob (e.g. after a pattern list update, or a manual
    report) can use the exact same detector rather than a second,
    possibly-drifted copy of it.
    """

    return any(pattern.search(content) is not None for pattern in _SECRET_PATTERNS)


def blob_path_for_digest(data_root: Path, content_digest: str) -> Path:
    hex_digest = content_digest.split(":", 1)[1]
    return data_root / "blobs" / "sha256" / hex_digest[:2] / hex_digest


def write_blob(
    data_root: Path,
    content: bytes,
    *,
    media_type: str,
    media_kind: ArtifactMediaKind,
    max_size_bytes: int | None = None,
    redact: bool = True,
    now: datetime | None = None,
) -> Artifact:
    """Store ``content`` as a content-addressed blob and return its ``Artifact``.

    Deduplicates automatically: if a blob with the same digest already
    exists on disk, the staging copy is discarded instead of overwriting
    it (bytes are immutable once addressed by their own hash).
    """

    if max_size_bytes is not None and len(content) > max_size_bytes:
        raise ArtifactQuotaExceededError(
            f"content is {len(content)} bytes, exceeds max_size_bytes={max_size_bytes}"
        )

    was_redacted = False
    if redact and media_kind in _REDACTABLE_KINDS:
        content, was_redacted = _redact(content)

    digest = compute_digest(content)
    final_path = blob_path_for_digest(data_root, digest)

    if not final_path.exists():
        staging_dir = data_root / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f"{uuid.uuid4().hex}.tmp"
        try:
            with open(staging_path, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, final_path)
        finally:
            # os.replace already moved it away on success; this only fires
            # if we raised before the rename (e.g. disk full mid-write).
            if staging_path.exists():
                staging_path.unlink(missing_ok=True)

    return Artifact(
        artifact_id=new_id(),
        media_type=media_type,
        media_kind=media_kind,
        size_bytes=len(content),
        content_digest=digest,
        storage_uri=f"blob:sha256:{digest.split(':', 1)[1]}",
        redaction_status=RedactionStatus.REDACTED if was_redacted else RedactionStatus.NONE,
        created_at=now or datetime.now(timezone.utc),
    )


def read_blob(data_root: Path, artifact: Artifact) -> bytes:
    """Read a blob back and verify its bytes still match ``content_digest``."""

    path = blob_path_for_digest(data_root, artifact.content_digest)
    if not path.exists():
        raise FileNotFoundError(f"no blob for digest {artifact.content_digest!r} at {path}")
    content = path.read_bytes()
    actual_digest = compute_digest(content)
    if actual_digest != artifact.content_digest:
        raise CorruptedArtifactError(
            f"blob at {path} has digest {actual_digest!r}, expected "
            f"{artifact.content_digest!r} — on-disk bytes were modified "
            "after write_blob()"
        )
    return content
