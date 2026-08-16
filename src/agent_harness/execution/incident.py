"""Secret Incident Response (Phase 13).

Attack surface #7 in the review's table ("로그에 비밀값 기록") lists a
recovery control distinct from prevention: "blob 삭제·tombstone, key
회전, affected report 표시." Phase 2.2's ``write_blob(redact=True)`` is
the *prevention* side (regex redaction at write time, streaming, before
anything hits disk). This module is the *response* side, for when a
secret is discovered in an artifact that already exists — a pattern the
prevention regex missed, an artifact written before a pattern list
update, or an external/manual report.

``quarantine_and_delete_artifact`` never tries to redact an existing
Artifact in place: an ``Artifact.content_digest`` is supposed to be an
immutable proof of exactly which bytes were ever stored (every
``EvidenceRecord`` that cites this artifact's digest depends on that not
changing), and ``Artifact`` is explicitly modeled as immutable after
creation. So the only honest response is what Phase 12's ``cleanup
--purge`` already does for routine retention — delete the blob, leave a
tombstone — reused here for the security-incident case specifically:
immediate, not retention-scheduled, and marked ``"incident": true`` in
the tombstone so it is distinguishable from a routine purge later.

Key rotation ("key 회전") and "affected report 표시" are operator actions
outside this codebase's control (rotating a real credential happens at
the provider, not in the harness) — this module's job stops at getting
the contaminated bytes off disk and leaving an auditable record that it
happened.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_harness.domain.models import Artifact
from agent_harness.persistence.artifacts import blob_path_for_digest, contains_secret_pattern, read_blob

__all__ = ["scan_artifact_for_secrets", "quarantine_and_delete_artifact"]


def scan_artifact_for_secrets(data_root: Path, artifact: Artifact) -> bool:
    """Re-scan an already-stored artifact's blob for a known secret pattern.

    Uses the exact same pattern set ``write_blob`` applies at write time
    (``persistence.artifacts.contains_secret_pattern``) — a positive here
    means either the pattern list changed since this blob was written, or
    the original redaction pass had a bug, either way worth a human
    incident review, not an automatic silent fix.
    """

    content = read_blob(data_root, artifact)
    return contains_secret_pattern(content)


def quarantine_and_delete_artifact(
    data_root: Path, artifact: Artifact, *, now: datetime, reason: str
) -> Path:
    """Immediately delete a secret-contaminated artifact's blob, leaving
    an incident tombstone. Returns the tombstone path.

    Ignores retention policy entirely — a confirmed secret leak is acted
    on now, not on the artifact's normal 30-day retention clock.
    """

    blob_path = blob_path_for_digest(data_root, artifact.content_digest)
    tombstone_path = blob_path.with_suffix(blob_path.suffix + ".tombstone")
    tombstone_path.write_text(
        json.dumps(
            {
                "incident": True,
                "reason": reason,
                "quarantined_at": now.isoformat(),
                "content_digest": artifact.content_digest,
                "artifact_id": artifact.artifact_id,
            }
        )
    )
    if blob_path.exists():
        blob_path.unlink()
    return tombstone_path
