"""Freeze the worktree, run approved host checks, produce HOST_OBSERVED evidence.

Ties together:

- ``execution/validation.py`` (manifest freeze + diff + test-mutation detection)
- ``execution/command_broker.py`` (Phase 3.2 — only registered commands run)
- ``persistence/artifacts.py`` (Phase 2.2 — manifests and command output
  become content-addressed Artifacts)
- ``domain/validation.py::assert_evidence_matches_artifact`` (Phase 2.2 —
  every ``EvidenceRecord`` this module builds is self-checked against its
  backing ``Artifact`` before being returned)

No Codex verifier here — this module only assembles evidence for
something else (Phase 8) to judge PASS/REWORK/REJECT against. Everything
this module produces carries ``EvidenceTrustTier.HOST_OBSERVED``: the
Harness ran the check and hashed the files itself, not the Worker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_harness.domain.enums import ActorType, ArtifactMediaKind, EvidenceTrustTier, SubjectType
from agent_harness.domain.models import Artifact, EvidenceProvenance, EvidenceRecord, ScopeRules
from agent_harness.domain.validation import assert_evidence_matches_artifact
from agent_harness.execution.command_broker import CommandCatalog, CommandExecution, execute_command
from agent_harness.execution.sandbox import SandboxBackend
from agent_harness.execution.scope_guard import find_scope_violations
from agent_harness.execution.validation import (
    ManifestDiff,
    ManifestEntry,
    build_file_manifest,
    diff_manifests,
    find_test_mutations,
)
from agent_harness.persistence.artifacts import write_blob

__all__ = [
    "FrozenValidationResult",
    "freeze_manifest",
    "run_approved_checks",
    "build_command_evidence",
    "freeze_and_validate",
]


def _canonical_manifest_json(entries: list[ManifestEntry]) -> bytes:
    payload = [
        {
            "relative_path": e.relative_path,
            "file_type": e.file_type,
            "mode": e.mode,
            "size": e.size,
            "sha256": e.sha256,
            "symlink_target": e.symlink_target,
        }
        for e in entries
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def freeze_manifest(
    worktree_path: Path, *, data_root: Path, now: datetime
) -> tuple[list[ManifestEntry], Artifact]:
    """Snapshot the current worktree state as a manifest + a stored Artifact."""

    entries = build_file_manifest(worktree_path)
    artifact = write_blob(
        data_root,
        _canonical_manifest_json(entries),
        media_type="application/json",
        media_kind=ArtifactMediaKind.JSON,
        redact=False,  # a manifest is paths/hashes, never file content
        now=now,
    )
    return entries, artifact


def run_approved_checks(
    catalog: CommandCatalog,
    check_command_ids: list[str],
    *,
    sandbox: SandboxBackend,
    cwd: Path,
    available_env: dict[str, str],
    now: datetime,
    data_root: Path,
    parameters_by_command_id: dict[str, dict[str, str]] | None = None,
) -> list[CommandExecution]:
    """Run every command_id in ``check_command_ids`` through the Command Broker.

    "Approved" means: already present in ``catalog`` — nothing here can run
    a command that was not registered ahead of time (Phase 3.2's
    ``UnknownCommandError`` guarantee).
    """

    parameters_by_command_id = parameters_by_command_id or {}
    return [
        execute_command(
            catalog,
            command_id,
            parameters_by_command_id.get(command_id, {}),
            sandbox=sandbox,
            cwd=cwd,
            available_env=available_env,
            now=now,
            artifact_data_root=data_root,
        )
        for command_id in check_command_ids
    ]


def build_command_evidence(
    run_id: str,
    task_id: str,
    execution: CommandExecution,
    *,
    now: datetime,
) -> list[EvidenceRecord]:
    """Wrap one command's stdout/stderr artifacts as HOST_OBSERVED evidence.

    Each returned record is checked against its own ``Artifact`` via
    ``assert_evidence_matches_artifact`` before being returned — a bug here
    fails loudly instead of shipping a self-inconsistent evidence record.
    """

    provenance = EvidenceProvenance(
        producer_type=ActorType.HOST_TEST_RUNNER,
        producer_id="command-broker",
        collection_method="direct_process_observation",
        trust_tier=EvidenceTrustTier.HOST_OBSERVED,
    )

    records: list[EvidenceRecord] = []
    for stream_name, artifact, truncated in (
        ("stdout", execution.stdout_artifact, execution.process_result.stdout_truncated),
        ("stderr", execution.stderr_artifact, execution.process_result.stderr_truncated),
    ):
        if artifact is None:
            continue
        record = EvidenceRecord(
            run_id=run_id,
            task_id=task_id,
            subject_type=SubjectType.COMMAND_RUN,
            subject_id=execution.command_run.command_spec_id,
            subject_digest=artifact.content_digest,
            kind=f"command_{stream_name}",
            provenance=provenance,
            artifact_refs=[artifact.artifact_id],
            media_type=artifact.media_type,
            content_digest=artifact.content_digest,
            size_bytes=artifact.size_bytes,
            created_at=now,
            truncated=truncated,
            verification_relevance=[execution.command_run.command_spec_id],
        )
        assert_evidence_matches_artifact(record, artifact)
        records.append(record)
    return records


@dataclass(frozen=True)
class FrozenValidationResult:
    baseline_manifest: list[ManifestEntry]
    result_manifest: list[ManifestEntry]
    baseline_artifact: Artifact
    result_artifact: Artifact
    manifest_diff: ManifestDiff
    test_mutations: list[str]
    check_executions: list[CommandExecution] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    post_test_manifest: list[ManifestEntry] = field(default_factory=list)
    post_test_artifact: Artifact | None = None
    test_side_effects: list[str] = field(default_factory=list)


def freeze_and_validate(
    run_id: str,
    task_id: str,
    worktree_path: Path,
    baseline_manifest: list[ManifestEntry],
    baseline_artifact: Artifact,
    *,
    catalog: CommandCatalog,
    check_command_ids: list[str],
    test_path_patterns: list[str],
    sandbox: SandboxBackend,
    available_env: dict[str, str],
    now: datetime,
    data_root: Path,
    parameters_by_command_id: dict[str, dict[str, str]] | None = None,
    scope: ScopeRules | None = None,
) -> FrozenValidationResult:
    """The Phase 3.3 pipeline: freeze -> diff -> detect test mutation ->
    host checks -> freeze again -> evidence.

    Order matters: ``result_manifest`` is frozen (and test-mutation/scope
    detection run against it) *before* any host check executes, so an
    approved check command cannot itself alter what that freeze already
    observed — ``manifest_diff``/``test_mutations``/``scope_violations``
    describe only the Worker's own changes. A **second** freeze
    (``post_test_manifest``) runs after the host checks, and the diff
    between the two becomes ``test_side_effects`` (Codex review B-02): a
    malicious or buggy test/build script that mutates the worktree as a
    side effect of running is no longer invisible just because it ran
    after the first snapshot.

    ``scope`` should be the *effective granted* scope
    (``PolicyDecision.grants.path_rules``), not the Planner's raw
    requested scope — per Codex review M-01, this is what actually
    decides ``scope_violations`` deterministically via
    ``execution.scope_guard.find_scope_violations``, rather than leaving
    it entirely to the Verifier's own free-text judgment. Left as
    ``None`` (skipping the check, ``scope_violations`` stays empty) only
    for callers that do not yet have a resolved grant to check against.
    """

    result_manifest, result_artifact = freeze_manifest(worktree_path, data_root=data_root, now=now)
    manifest_diff = diff_manifests(baseline_manifest, result_manifest)
    test_mutations = find_test_mutations(manifest_diff, test_path_patterns)
    scope_violations = (
        find_scope_violations(
            manifest_diff, scope, baseline_manifest=baseline_manifest, result_manifest=result_manifest
        )
        if scope is not None
        else []
    )

    check_executions = run_approved_checks(
        catalog,
        check_command_ids,
        sandbox=sandbox,
        cwd=worktree_path,
        available_env=available_env,
        now=now,
        data_root=data_root,
        parameters_by_command_id=parameters_by_command_id,
    )

    post_test_manifest, post_test_artifact = freeze_manifest(worktree_path, data_root=data_root, now=now)
    test_side_effects_diff = diff_manifests(result_manifest, post_test_manifest)
    test_side_effects = sorted(
        [*test_side_effects_diff.added, *test_side_effects_diff.modified, *test_side_effects_diff.deleted]
    )

    evidence: list[EvidenceRecord] = []
    for execution in check_executions:
        evidence.extend(build_command_evidence(run_id, task_id, execution, now=now))

    return FrozenValidationResult(
        baseline_manifest=baseline_manifest,
        result_manifest=result_manifest,
        baseline_artifact=baseline_artifact,
        result_artifact=result_artifact,
        manifest_diff=manifest_diff,
        test_mutations=test_mutations,
        check_executions=check_executions,
        evidence=evidence,
        scope_violations=scope_violations,
        post_test_manifest=post_test_manifest,
        post_test_artifact=post_test_artifact,
        test_side_effects=test_side_effects,
    )
