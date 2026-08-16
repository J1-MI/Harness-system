"""Ordered, idempotent SQLite schema migrations.

Phase 2.1 shipped ``runs``, ``tasks``, ``agent_invocations``,
``failure_records``, ``journal_entries``; Phase 2.2 added ``artifacts``,
``context_snapshots``, ``evidence``. Codex implementation review (B-04)
picks up part of the remaining table list architecture review section 10
named: ``task_contracts``, ``policy_decisions``, ``verification_results``,
``rework_contracts`` — the four objects most load-bearing for "can the
next legal state and the reasoning behind it be reconstructed from the
DB alone." ``agent_sessions``, ``approvals``, ``workspace_leases``, and
``command_runs`` are still not persisted — see
``docs/architecture/hardening-and-recovery.md``/the Codex review response
notes for why those remain open follow-ups rather than being rushed in
alongside this batch.

Each of the four new tables stores its full model as ``data_json``
(``model_dump_json()``, matching the existing ``payload_json``/
``provenance_json`` convention already used by ``journal_entries``/
``evidence``) plus a handful of indexed columns for lookup — these are
audit records, not objects this layer needs to run structured queries
against beyond "give me everything for this Run."
"""

from __future__ import annotations

import sqlite3

# Each migration is a single idempotent DDL script. Order matters — this
# list *is* the migration history, append-only.
MIGRATIONS: tuple[str, ...] = (
    # 1: schema_migrations bookkeeping table itself.
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    );
    """,
    # 2: runs
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        user_request_ref TEXT NOT NULL,
        user_request_digest TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        state TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        budget_limits_json TEXT NOT NULL,
        budget_used_json TEXT NOT NULL,
        current_task_id TEXT,
        disposition TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 3: tasks
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        schema_version TEXT NOT NULL,
        objective TEXT NOT NULL,
        dependency_ids_json TEXT NOT NULL,
        active_contract_id TEXT,
        active_contract_revision INTEGER,
        attempt_count INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);
    """,
    # 4: agent_invocations
    """
    CREATE TABLE IF NOT EXISTS agent_invocations (
        invocation_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        request_digest TEXT NOT NULL,
        deadline TEXT NOT NULL,
        event_refs_json TEXT NOT NULL,
        normalized_result_ref TEXT,
        normalized_error_json TEXT,
        usage_json TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_invocations_session_id
        ON agent_invocations(session_id);
    """,
    # 5: failure_records
    """
    CREATE TABLE IF NOT EXISTS failure_records (
        failure_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT,
        stage TEXT NOT NULL,
        code TEXT NOT NULL,
        category TEXT NOT NULL,
        retriable INTEGER NOT NULL,
        sanitized_detail TEXT NOT NULL,
        artifact_refs_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_failure_records_run_id
        ON failure_records(run_id);
    """,
    # 6: journal_entries (append-only)
    """
    CREATE TABLE IF NOT EXISTS journal_entries (
        journal_entry_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        timestamp_utc TEXT NOT NULL,
        event_type TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        state_before TEXT,
        state_after TEXT,
        state_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        artifact_refs_json TEXT NOT NULL,
        previous_entry_hash TEXT,
        entry_hash TEXT NOT NULL,
        UNIQUE(run_id, sequence)
    );
    """,
    # 7: artifacts (metadata only — bytes live under <data-root>/blobs/,
    # written by persistence/artifacts.py)
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        media_type TEXT NOT NULL,
        media_kind TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        content_digest TEXT NOT NULL,
        storage_uri TEXT NOT NULL,
        compression TEXT,
        redaction_status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_artifacts_content_digest
        ON artifacts(content_digest);
    """,
    # 8: context_snapshots (refs/digests only — never raw file bytes)
    """
    CREATE TABLE IF NOT EXISTS context_snapshots (
        context_snapshot_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        prompt_template_digest TEXT NOT NULL,
        contract_ref TEXT NOT NULL,
        contract_digest TEXT NOT NULL,
        policy_decision_ref TEXT,
        policy_decision_digest TEXT,
        provided_files_json TEXT NOT NULL,
        tool_result_digests_json TEXT NOT NULL,
        mcp_result_digests_json TEXT NOT NULL,
        schema_digest TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        provider_version TEXT NOT NULL,
        role_profile_digest TEXT NOT NULL,
        environment_capability_manifest_json TEXT NOT NULL,
        environment_variable_names_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_context_snapshots_run_id
        ON context_snapshots(run_id);
    """,
    # 9: evidence
    """
    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        subject_digest TEXT NOT NULL,
        kind TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        artifact_refs_json TEXT NOT NULL,
        media_type TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        redaction_status TEXT NOT NULL,
        truncated INTEGER NOT NULL,
        verification_relevance_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence(run_id);
    """,
    # 10: task_contracts, policy_decisions, verification_results,
    # rework_contracts (Codex review B-04, partial — see module docstring)
    """
    CREATE TABLE IF NOT EXISTS task_contracts (
        contract_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        contract_revision INTEGER NOT NULL,
        canonical_digest TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_task_contracts_run_id ON task_contracts(run_id);

    CREATE TABLE IF NOT EXISTS policy_decisions (
        decision_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        integrity_digest TEXT NOT NULL,
        data_json TEXT NOT NULL,
        evaluated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_policy_decisions_run_id ON policy_decisions(run_id);

    CREATE TABLE IF NOT EXISTS verification_results (
        verification_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        contract_digest TEXT NOT NULL,
        decision TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_verification_results_run_id ON verification_results(run_id);

    CREATE TABLE IF NOT EXISTS rework_contracts (
        rework_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        parent_contract_digest TEXT NOT NULL,
        data_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_rework_contracts_run_id ON rework_contracts(run_id);
    """,
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply every migration in ``MIGRATIONS`` not yet recorded as applied.

    Safe to call on every process start: each script is idempotent
    (``CREATE TABLE IF NOT EXISTS``) and already-applied versions are
    skipped via the ``schema_migrations`` bookkeeping table.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, script in enumerate(MIGRATIONS, start=1):
        if version in applied:
            continue
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
            (version,),
        )
    conn.commit()
