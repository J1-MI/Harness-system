"""Reporting (Phase 12): build the final manifest for a Run.

Roadmap row 12 names "report builder" as its own target, separate from
the Typer CLI itself — ``interfaces/cli.py``'s ``report`` command is a
thin wrapper around ``build_final_manifest`` below, which does the actual
work and is independently testable without going through the CLI at all.

Pulls exclusively from what Phase 2.1/2.2 already persist (Run, Task,
journal entries, evidence records, failure record) — no new storage. See
``FinalManifest``'s own docstring (domain/models.py) for why
``manifest_digest`` is a canonical digest, not a cryptographic signature.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from agent_harness.domain.digests import compute_model_digest, new_id
from agent_harness.domain.models import FinalManifest
from agent_harness.persistence.sqlite import (
    get_failure_records,
    get_run,
    get_task,
    list_evidence_for_run,
    list_journal_entries,
)

__all__ = ["RunNotFoundForReportError", "build_final_manifest"]


class RunNotFoundForReportError(RuntimeError):
    pass


def build_final_manifest(conn: sqlite3.Connection, run_id: str, *, generated_at: datetime) -> FinalManifest:
    run = get_run(conn, run_id)
    if run is None:
        raise RunNotFoundForReportError(f"no Run found with run_id={run_id!r}")

    journal_entries = list_journal_entries(conn, run_id)
    evidence_records = list_evidence_for_run(conn, run_id)
    failures = get_failure_records(conn, run_id)

    task_ids: list[str] = []
    if run.current_task_id is not None:
        task = get_task(conn, run.current_task_id)
        if task is not None:
            task_ids.append(task.task_id)

    draft = FinalManifest(
        manifest_id=new_id(),
        run_id=run.run_id,
        generated_at=generated_at,
        run_state=run.state,
        run_disposition=run.disposition,
        budget_used=run.budget_used,
        task_ids=task_ids,
        journal_entry_count=len(journal_entries),
        journal_head_hash=journal_entries[-1].entry_hash if journal_entries else None,
        evidence_ids=[record.evidence_id for record in evidence_records],
        failure=failures[-1] if failures else None,
        manifest_digest="sha256:" + "0" * 64,
    )
    real_digest = compute_model_digest(draft, exclude_fields={"manifest_digest"})
    return draft.model_copy(update={"manifest_digest": real_digest})
