"""SQLite-backed Run/Task/AgentInvocation store with an atomic journal.

Also carries Artifact/ContextSnapshot/Evidence *metadata* (Phase 2.2) —
digests, refs, provenance — never raw bytes. Blob bytes live under
``<data-root>/blobs/`` via ``persistence/artifacts.py``; this module never
touches the filesystem outside of the ``.sqlite3`` file itself.

The only multi-statement transaction is ``apply_transition``: it re-reads
the Run, re-validates the requested transition through the *same* pure
kernel from ``application/transitions.py`` (Phase 1.2), appends a
hash-chained journal entry, and updates the Run row — all inside one
``BEGIN IMMEDIATE`` transaction so a crash midway leaves no partial state
(see ``tests/unit/test_sqlite_persistence.py``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.application.transitions import PendingAction, TransitionOutcome, transition_run
from agent_harness.domain.digests import compute_model_digest
from agent_harness.domain.enums import (
    ActorType,
    AgentRole,
    ArtifactMediaKind,
    LifecycleState,
    RedactionStatus,
    SubjectType,
)
from agent_harness.domain.models import (
    AgentInvocation,
    Artifact,
    BudgetRequest,
    BudgetUsage,
    ContextSnapshot,
    EvidenceProvenance,
    EvidenceRecord,
    FailureRecord,
    JournalEntry,
    PolicyDecision,
    ProvidedFileRef,
    ProviderError,
    ReworkContract,
    Run,
    Task,
    TaskContract,
    UsageRecord,
    VerificationResult,
)
from agent_harness.persistence.migrations import apply_migrations

_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


class RunNotFoundError(LookupError):
    pass


class ConcurrentModificationError(RuntimeError):
    """Raised when the Run's persisted state_version no longer matches the
    caller's expectation — someone else committed a transition first."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the harness SQLite database.

    Autocommit mode (``isolation_level=None``) is used deliberately so
    ``apply_transition`` can issue an explicit ``BEGIN IMMEDIATE`` and
    control the transaction boundary itself.
    """

    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    apply_migrations(conn)
    return conn


def _dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _run_params(run: Run) -> tuple:
    return (
        run.run_id,
        run.schema_version,
        run.user_request_ref,
        run.user_request_digest,
        run.repository_id,
        run.state.value,
        run.state_version,
        run.budget_limits.model_dump_json(),
        run.budget_used.model_dump_json(),
        run.current_task_id,
        run.disposition.value if run.disposition is not None else None,
        _dt_to_str(run.created_at),
        _dt_to_str(run.updated_at),
    )


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        run_id=row["run_id"],
        schema_version=row["schema_version"],
        user_request_ref=row["user_request_ref"],
        user_request_digest=row["user_request_digest"],
        repository_id=row["repository_id"],
        state=LifecycleState(row["state"]),
        state_version=row["state_version"],
        budget_limits=BudgetRequest.model_validate_json(row["budget_limits_json"]),
        budget_used=BudgetUsage.model_validate_json(row["budget_used_json"]),
        current_task_id=row["current_task_id"],
        disposition=(
            LifecycleState(row["disposition"]) if row["disposition"] is not None else None
        ),
        created_at=_str_to_dt(row["created_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
    )


def insert_run(conn: sqlite3.Connection, run: Run) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            run_id, schema_version, user_request_ref, user_request_digest,
            repository_id, state, state_version, budget_limits_json,
            budget_used_json, current_task_id, disposition, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _run_params(run),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> Run | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None


def list_runs_by_states(conn: sqlite3.Connection, states: list[LifecycleState]) -> list[Run]:
    """Every Run currently sitting in one of ``states``.

    Added for Phase 13's Recovery Coordinator, which needs to scan for
    stale active Runs at startup — nothing before Phase 13 needed to list
    Runs by anything other than their own ``run_id``.
    """

    if not states:
        return []
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT * FROM runs WHERE state IN ({placeholders})", tuple(s.value for s in states)
    ).fetchall()
    return [_row_to_run(row) for row in rows]


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, run_id, schema_version, objective, dependency_ids_json,
            active_contract_id, active_contract_revision, attempt_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.run_id,
            task.schema_version,
            task.objective,
            json.dumps(task.dependency_ids),
            task.active_contract_id,
            task.active_contract_revision,
            task.attempt_count,
        ),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return Task(
        task_id=row["task_id"],
        run_id=row["run_id"],
        schema_version=row["schema_version"],
        objective=row["objective"],
        dependency_ids=json.loads(row["dependency_ids_json"]),
        active_contract_id=row["active_contract_id"],
        active_contract_revision=row["active_contract_revision"],
        attempt_count=row["attempt_count"],
    )


# ---------------------------------------------------------------------------
# AgentInvocation
# ---------------------------------------------------------------------------


def insert_invocation(conn: sqlite3.Connection, invocation: AgentInvocation) -> None:
    conn.execute(
        """
        INSERT INTO agent_invocations (
            invocation_id, schema_version, session_id, role, attempt,
            request_digest, deadline, event_refs_json, normalized_result_ref,
            normalized_error_json, usage_json, started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invocation.invocation_id,
            invocation.schema_version,
            invocation.session_id,
            invocation.role.value,
            invocation.attempt,
            invocation.request_digest,
            _dt_to_str(invocation.deadline),
            json.dumps(invocation.event_refs),
            invocation.normalized_result_ref,
            invocation.normalized_error.model_dump_json()
            if invocation.normalized_error is not None
            else None,
            invocation.usage.model_dump_json() if invocation.usage is not None else None,
            _dt_to_str(invocation.started_at),
            _dt_to_str(invocation.completed_at)
            if invocation.completed_at is not None
            else None,
        ),
    )


def get_invocation(conn: sqlite3.Connection, invocation_id: str) -> AgentInvocation | None:
    row = conn.execute(
        "SELECT * FROM agent_invocations WHERE invocation_id = ?", (invocation_id,)
    ).fetchone()
    if row is None:
        return None
    return AgentInvocation(
        invocation_id=row["invocation_id"],
        schema_version=row["schema_version"],
        session_id=row["session_id"],
        role=AgentRole(row["role"]),
        attempt=row["attempt"],
        request_digest=row["request_digest"],
        deadline=_str_to_dt(row["deadline"]),
        event_refs=json.loads(row["event_refs_json"]),
        normalized_result_ref=row["normalized_result_ref"],
        normalized_error=(
            ProviderError.model_validate_json(row["normalized_error_json"])
            if row["normalized_error_json"] is not None
            else None
        ),
        usage=(
            UsageRecord.model_validate_json(row["usage_json"])
            if row["usage_json"] is not None
            else None
        ),
        started_at=_str_to_dt(row["started_at"]),
        completed_at=(
            _str_to_dt(row["completed_at"]) if row["completed_at"] is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# FailureRecord (write path only exercised via apply_transition)
# ---------------------------------------------------------------------------


def _insert_failure_record(conn: sqlite3.Connection, failure: FailureRecord) -> None:
    conn.execute(
        """
        INSERT INTO failure_records (
            failure_id, schema_version, run_id, task_id, stage, code,
            category, retriable, sanitized_detail, artifact_refs_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            failure.failure_id,
            failure.schema_version,
            failure.run_id,
            failure.task_id,
            failure.stage,
            failure.code.value,
            failure.category.value,
            int(failure.retriable),
            failure.sanitized_detail,
            json.dumps(failure.artifact_refs),
            _dt_to_str(failure.occurred_at),
        ),
    )


def get_failure_records(conn: sqlite3.Connection, run_id: str) -> list[FailureRecord]:
    rows = conn.execute(
        "SELECT * FROM failure_records WHERE run_id = ? ORDER BY occurred_at ASC", (run_id,)
    ).fetchall()
    return [
        FailureRecord(
            failure_id=row["failure_id"],
            schema_version=row["schema_version"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            stage=row["stage"],
            code=row["code"],
            category=row["category"],
            retriable=bool(row["retriable"]),
            sanitized_detail=row["sanitized_detail"],
            artifact_refs=json.loads(row["artifact_refs_json"]),
            occurred_at=_str_to_dt(row["occurred_at"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# JournalEntry (append-only)
# ---------------------------------------------------------------------------


def _insert_journal_entry(conn: sqlite3.Connection, entry: JournalEntry) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (
            journal_entry_id, schema_version, sequence, run_id, timestamp_utc,
            event_type, actor_type, actor_id, correlation_id, state_before,
            state_after, state_version, payload_json, artifact_refs_json,
            previous_entry_hash, entry_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.journal_entry_id,
            entry.schema_version,
            entry.sequence,
            entry.run_id,
            _dt_to_str(entry.timestamp_utc),
            entry.event_type,
            entry.actor_type.value,
            entry.actor_id,
            entry.correlation_id,
            entry.state_before.value if entry.state_before is not None else None,
            entry.state_after.value if entry.state_after is not None else None,
            entry.state_version,
            json.dumps(entry.payload_json, sort_keys=True),
            json.dumps(entry.artifact_refs),
            entry.previous_entry_hash,
            entry.entry_hash,
        ),
    )


def _row_to_journal_entry(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        journal_entry_id=row["journal_entry_id"],
        schema_version=row["schema_version"],
        sequence=row["sequence"],
        run_id=row["run_id"],
        timestamp_utc=_str_to_dt(row["timestamp_utc"]),
        event_type=row["event_type"],
        actor_type=ActorType(row["actor_type"]),
        actor_id=row["actor_id"],
        correlation_id=row["correlation_id"],
        state_before=LifecycleState(row["state_before"]) if row["state_before"] else None,
        state_after=LifecycleState(row["state_after"]) if row["state_after"] else None,
        state_version=row["state_version"],
        payload_json=json.loads(row["payload_json"]),
        artifact_refs=json.loads(row["artifact_refs_json"]),
        previous_entry_hash=row["previous_entry_hash"],
        entry_hash=row["entry_hash"],
    )


def list_journal_entries(conn: sqlite3.Connection, run_id: str) -> list[JournalEntry]:
    rows = conn.execute(
        "SELECT * FROM journal_entries WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
    ).fetchall()
    return [_row_to_journal_entry(row) for row in rows]


# ---------------------------------------------------------------------------
# The atomic operation: validate + journal + persist, in one transaction
# ---------------------------------------------------------------------------


def apply_transition(
    conn: sqlite3.Connection,
    run_id: str,
    target: LifecycleState,
    *,
    now: datetime,
    expected_state_version: int,
    actor_type: ActorType,
    actor_id: str,
    correlation_id: str,
    event_type: str | None = None,
    failure: FailureRecord | None = None,
    pending_action: PendingAction | None = None,
) -> TransitionOutcome:
    """Validate and persist one LifecycleState transition atomically.

    Sequence (matches architecture review section 10's pseudocode):
    ``BEGIN IMMEDIATE`` -> read current state_version -> re-validate via the
    pure kernel -> append journal entry -> update the run row -> ``COMMIT``.
    Any exception rolls the whole transaction back, so a crash mid-sequence
    never leaves a journal entry without a matching run update or vice versa.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        current_run = _row_to_run(row)

        if current_run.state_version != expected_state_version:
            raise ConcurrentModificationError(
                f"run {run_id}: expected state_version="
                f"{expected_state_version} but persisted state_version="
                f"{current_run.state_version}"
            )

        outcome = transition_run(
            current_run, target, now=now, failure=failure, pending_action=pending_action
        )

        prev_row = conn.execute(
            "SELECT entry_hash FROM journal_entries WHERE run_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        previous_entry_hash = prev_row["entry_hash"] if prev_row is not None else None

        seq_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_seq "
            "FROM journal_entries WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        sequence = seq_row["next_seq"]

        payload: dict = {}
        if pending_action is not None:
            payload["pending_action"] = pending_action.model_dump(mode="json")
        if failure is not None:
            payload["failure_id"] = failure.failure_id

        draft_entry = JournalEntry(
            sequence=sequence,
            run_id=run_id,
            timestamp_utc=now,
            event_type=event_type or f"TRANSITION_{target.value}",
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            state_before=current_run.state,
            state_after=target,
            state_version=outcome.run.state_version,
            payload_json=payload,
            previous_entry_hash=previous_entry_hash,
            entry_hash=_PLACEHOLDER_DIGEST,
        )
        entry_hash = compute_model_digest(draft_entry, exclude_fields={"entry_hash"})
        entry = draft_entry.model_copy(update={"entry_hash": entry_hash})

        _insert_journal_entry(conn, entry)

        cursor = conn.execute(
            "UPDATE runs SET state = ?, state_version = ?, disposition = ?, "
            "updated_at = ? WHERE run_id = ? AND state_version = ?",
            (
                outcome.run.state.value,
                outcome.run.state_version,
                outcome.run.disposition.value if outcome.run.disposition else None,
                _dt_to_str(outcome.run.updated_at),
                run_id,
                expected_state_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentModificationError(
                f"run {run_id} changed underneath this transaction "
                "(expected exactly one row updated)"
            )

        if failure is not None:
            _insert_failure_record(conn, failure)

        conn.execute("COMMIT")
        return outcome
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Artifact metadata (bytes live in persistence/artifacts.py, not here)
# ---------------------------------------------------------------------------


def insert_artifact(conn: sqlite3.Connection, artifact: Artifact) -> None:
    conn.execute(
        """
        INSERT INTO artifacts (
            artifact_id, schema_version, media_type, media_kind, size_bytes,
            content_digest, storage_uri, compression, redaction_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact.artifact_id,
            artifact.schema_version,
            artifact.media_type,
            artifact.media_kind.value,
            artifact.size_bytes,
            artifact.content_digest,
            artifact.storage_uri,
            artifact.compression,
            artifact.redaction_status.value,
            _dt_to_str(artifact.created_at),
        ),
    )


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    row = conn.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        return None
    return Artifact(
        artifact_id=row["artifact_id"],
        schema_version=row["schema_version"],
        media_type=row["media_type"],
        media_kind=ArtifactMediaKind(row["media_kind"]),
        size_bytes=row["size_bytes"],
        content_digest=row["content_digest"],
        storage_uri=row["storage_uri"],
        compression=row["compression"],
        redaction_status=RedactionStatus(row["redaction_status"]),
        created_at=_str_to_dt(row["created_at"]),
    )


def count_artifacts_with_content_digest(conn: sqlite3.Connection, content_digest: str) -> int:
    """How many ``Artifact`` rows (any Run, any Task) share this digest.

    Added for Phase 12 CLI's ``cleanup --purge`` (Codex review M-04):
    ``persistence.artifacts.write_blob`` deduplicates identical bytes to
    one blob file on disk, but each call still inserts its own new
    ``Artifact`` row/``artifact_id`` — so two unrelated Runs producing
    byte-identical output (e.g. the same "ok" stdout from a trivial
    check) can end up with different Artifacts pointing at the exact
    same blob. A purge must never delete a blob some *other* Artifact
    row still references.
    """

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM artifacts WHERE content_digest = ?", (content_digest,)
    ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# ContextSnapshot (refs/digests only, never raw file bytes)
# ---------------------------------------------------------------------------


def insert_context_snapshot(conn: sqlite3.Connection, snapshot: ContextSnapshot) -> None:
    conn.execute(
        """
        INSERT INTO context_snapshots (
            context_snapshot_id, schema_version, run_id, task_id,
            prompt_template_digest, contract_ref, contract_digest,
            policy_decision_ref, policy_decision_digest, provided_files_json,
            tool_result_digests_json, mcp_result_digests_json, schema_digest,
            provider_id, provider_version, role_profile_digest,
            environment_capability_manifest_json, environment_variable_names_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.context_snapshot_id,
            snapshot.schema_version,
            snapshot.run_id,
            snapshot.task_id,
            snapshot.prompt_template_digest,
            snapshot.contract_ref,
            snapshot.contract_digest,
            snapshot.policy_decision_ref,
            snapshot.policy_decision_digest,
            json.dumps([f.model_dump(mode="json") for f in snapshot.provided_files]),
            json.dumps(snapshot.tool_result_digests),
            json.dumps(snapshot.mcp_result_digests),
            snapshot.schema_digest,
            snapshot.provider_id,
            snapshot.provider_version,
            snapshot.role_profile_digest,
            json.dumps(snapshot.environment_capability_manifest, sort_keys=True),
            json.dumps(snapshot.environment_variable_names),
            _dt_to_str(snapshot.created_at),
        ),
    )


def get_context_snapshot(
    conn: sqlite3.Connection, context_snapshot_id: str
) -> ContextSnapshot | None:
    row = conn.execute(
        "SELECT * FROM context_snapshots WHERE context_snapshot_id = ?",
        (context_snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    return ContextSnapshot(
        context_snapshot_id=row["context_snapshot_id"],
        schema_version=row["schema_version"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        prompt_template_digest=row["prompt_template_digest"],
        contract_ref=row["contract_ref"],
        contract_digest=row["contract_digest"],
        policy_decision_ref=row["policy_decision_ref"],
        policy_decision_digest=row["policy_decision_digest"],
        provided_files=[
            ProvidedFileRef(**f) for f in json.loads(row["provided_files_json"])
        ],
        tool_result_digests=json.loads(row["tool_result_digests_json"]),
        mcp_result_digests=json.loads(row["mcp_result_digests_json"]),
        schema_digest=row["schema_digest"],
        provider_id=row["provider_id"],
        provider_version=row["provider_version"],
        role_profile_digest=row["role_profile_digest"],
        environment_capability_manifest=json.loads(
            row["environment_capability_manifest_json"]
        ),
        environment_variable_names=json.loads(row["environment_variable_names_json"]),
        created_at=_str_to_dt(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# EvidenceRecord
# ---------------------------------------------------------------------------


def insert_evidence_record(conn: sqlite3.Connection, evidence: EvidenceRecord) -> None:
    conn.execute(
        """
        INSERT INTO evidence (
            evidence_id, schema_version, run_id, task_id, subject_type,
            subject_id, subject_digest, kind, provenance_json, artifact_refs_json,
            media_type, content_digest, size_bytes, created_at, redaction_status,
            truncated, verification_relevance_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence.evidence_id,
            evidence.schema_version,
            evidence.run_id,
            evidence.task_id,
            evidence.subject_type.value,
            evidence.subject_id,
            evidence.subject_digest,
            evidence.kind,
            evidence.provenance.model_dump_json(),
            json.dumps(evidence.artifact_refs),
            evidence.media_type,
            evidence.content_digest,
            evidence.size_bytes,
            _dt_to_str(evidence.created_at),
            evidence.redaction_status.value,
            int(evidence.truncated),
            json.dumps(evidence.verification_relevance),
        ),
    )


def get_evidence_record(conn: sqlite3.Connection, evidence_id: str) -> EvidenceRecord | None:
    row = conn.execute(
        "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
    ).fetchone()
    if row is None:
        return None
    return EvidenceRecord(
        evidence_id=row["evidence_id"],
        schema_version=row["schema_version"],
        run_id=row["run_id"],
        task_id=row["task_id"],
        subject_type=SubjectType(row["subject_type"]),
        subject_id=row["subject_id"],
        subject_digest=row["subject_digest"],
        kind=row["kind"],
        provenance=EvidenceProvenance.model_validate_json(row["provenance_json"]),
        artifact_refs=json.loads(row["artifact_refs_json"]),
        media_type=row["media_type"],
        content_digest=row["content_digest"],
        size_bytes=row["size_bytes"],
        created_at=_str_to_dt(row["created_at"]),
        redaction_status=RedactionStatus(row["redaction_status"]),
        truncated=bool(row["truncated"]),
        verification_relevance=json.loads(row["verification_relevance_json"]),
    )


def list_evidence_for_run(conn: sqlite3.Connection, run_id: str) -> list[EvidenceRecord]:
    rows = conn.execute(
        "SELECT evidence_id FROM evidence WHERE run_id = ? ORDER BY created_at ASC",
        (run_id,),
    ).fetchall()
    return [get_evidence_record(conn, row["evidence_id"]) for row in rows]


# ---------------------------------------------------------------------------
# TaskContract / PolicyDecision / VerificationResult / ReworkContract
# (Codex review B-04, partial persistence — see migrations.py's module
# docstring for exactly which objects are still not covered)
# ---------------------------------------------------------------------------


def insert_task_contract(conn: sqlite3.Connection, contract: TaskContract, *, run_id: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO task_contracts (
            contract_id, run_id, task_id, contract_revision, canonical_digest, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract.contract_id, run_id, contract.task_id, contract.contract_revision,
            contract.integrity.canonical_digest, contract.model_dump_json(), _dt_to_str(now),
        ),
    )


def get_task_contract(conn: sqlite3.Connection, contract_id: str) -> TaskContract | None:
    row = conn.execute(
        "SELECT data_json FROM task_contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()
    return TaskContract.model_validate_json(row["data_json"]) if row is not None else None


def list_task_contracts_for_run(conn: sqlite3.Connection, run_id: str) -> list[TaskContract]:
    rows = conn.execute(
        "SELECT data_json FROM task_contracts WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
    ).fetchall()
    return [TaskContract.model_validate_json(row["data_json"]) for row in rows]


def insert_policy_decision(conn: sqlite3.Connection, decision: PolicyDecision, *, run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO policy_decisions (
            decision_id, run_id, subject_type, subject_id, outcome, integrity_digest, data_json, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision.decision_id, run_id, decision.subject_type.value, decision.subject_id,
            decision.outcome.value, decision.integrity_digest, decision.model_dump_json(),
            _dt_to_str(decision.evaluated_at),
        ),
    )


def get_policy_decision(conn: sqlite3.Connection, decision_id: str) -> PolicyDecision | None:
    row = conn.execute(
        "SELECT data_json FROM policy_decisions WHERE decision_id = ?", (decision_id,)
    ).fetchone()
    return PolicyDecision.model_validate_json(row["data_json"]) if row is not None else None


def list_policy_decisions_for_run(conn: sqlite3.Connection, run_id: str) -> list[PolicyDecision]:
    rows = conn.execute(
        "SELECT data_json FROM policy_decisions WHERE run_id = ? ORDER BY evaluated_at ASC", (run_id,)
    ).fetchall()
    return [PolicyDecision.model_validate_json(row["data_json"]) for row in rows]


def insert_verification_result(
    conn: sqlite3.Connection, result: VerificationResult, *, run_id: str, now: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO verification_results (
            verification_id, run_id, task_id, contract_digest, decision, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.verification_id, run_id, result.task_id, result.contract_digest,
            result.decision.value, result.model_dump_json(), _dt_to_str(now),
        ),
    )


def get_verification_result(conn: sqlite3.Connection, verification_id: str) -> VerificationResult | None:
    row = conn.execute(
        "SELECT data_json FROM verification_results WHERE verification_id = ?", (verification_id,)
    ).fetchone()
    return VerificationResult.model_validate_json(row["data_json"]) if row is not None else None


def list_verification_results_for_run(conn: sqlite3.Connection, run_id: str) -> list[VerificationResult]:
    rows = conn.execute(
        "SELECT data_json FROM verification_results WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
    ).fetchall()
    return [VerificationResult.model_validate_json(row["data_json"]) for row in rows]


def insert_rework_contract(
    conn: sqlite3.Connection, rework: ReworkContract, *, run_id: str, now: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO rework_contracts (
            rework_id, run_id, task_id, attempt_number, parent_contract_digest, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rework.rework_id, run_id, rework.task_id, rework.attempt_number,
            rework.parent_contract_digest, rework.model_dump_json(), _dt_to_str(now),
        ),
    )


def get_rework_contract(conn: sqlite3.Connection, rework_id: str) -> ReworkContract | None:
    row = conn.execute(
        "SELECT data_json FROM rework_contracts WHERE rework_id = ?", (rework_id,)
    ).fetchone()
    return ReworkContract.model_validate_json(row["data_json"]) if row is not None else None


def list_rework_contracts_for_run(conn: sqlite3.Connection, run_id: str) -> list[ReworkContract]:
    rows = conn.execute(
        "SELECT data_json FROM rework_contracts WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
    ).fetchall()
    return [ReworkContract.model_validate_json(row["data_json"]) for row in rows]
