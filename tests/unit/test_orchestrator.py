"""Fake E2E + opt-in live E2E for the Dual-Agent Pipeline (Phase 9).

The Fake E2E tests drive ``run_task_pipeline`` through
``FakeAgentProvider`` for all three roles — same orchestrator code path
a real Claude/Codex run would take, just with scripted provider turns
instead of network calls. Each test asserts on the *persisted* state:
the final ``Run.state``/``disposition`` and the full ``journal_entries``
sequence via ``persistence.sqlite``, not just the returned ``Run``.

Reuses ``make_repo``/``git_client`` from ``test_workspace.py`` (Phase
3.1) for a real local git repo, and the Codex/Claude fakes are not
needed here — Phase 9 only needs ``providers.fake.FakeAgentProvider``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_harness.application.orchestrator import (
    PipelineDeps,
    PipelineTimeoutError,
    _build_worker_prompt,
    _claude_tool_ids_for,
    _invoke_role,
    run_task_pipeline,
)
from agent_harness.application.verification import PromptRegistry
from agent_harness.domain.digests import new_id
from agent_harness.domain.enums import (
    AgentRole,
    DriverKind,
    LifecycleState,
    McpControlSupport,
    PolicyOutcome,
    ProtocolStatus,
    SessionResumeSupport,
    StreamingSupport,
    StructuredOutputSupport,
    UsageReportingSupport,
    VerificationDecision,
)
from agent_harness.domain.models import BudgetRequest, CommandSpec
from agent_harness.execution.command_broker import CommandCatalog
from agent_harness.execution.git_client import GitClient
from agent_harness.execution.sandbox import TrustedLocalSandbox
from agent_harness.persistence.migrations import apply_migrations
from agent_harness.persistence.sqlite import connect, insert_run, insert_task, list_journal_entries
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import AgentRunResult, ProviderCapabilities
from tests.factories import (
    make_acceptance_criterion,
    make_policy_ceiling,
    make_policy_grants,
    make_run,
    make_task_contract,
    make_verification_spec,
)
from tests.unit.test_workspace import make_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not available"
)

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_capabilities(role: AgentRole) -> ProviderCapabilities:
    return ProviderCapabilities(
        supported_roles=[role],
        structured_output=StructuredOutputSupport.JSON_SCHEMA,
        streaming=StreamingSupport.NONE,
        session_resume=SessionResumeSupport.NONE,
        session_fork=False,
        native_cancel=False,
        tool_approval_callbacks=False,
        tool_visibility_control=False,
        mcp_control=McpControlSupport.NONE,
        usage_reporting=UsageReportingSupport.NONE,
        driver_kind=DriverKind.SDK,
        driver_version="0.0.0-fake",
        capability_probe_timestamp=_utc_now(),
    )


def make_result(structured_output: dict) -> AgentRunResult:
    return AgentRunResult(
        invocation_id=new_id(),
        protocol_status=ProtocolStatus.SUCCEEDED,
        structured_output=structured_output,
        provider_session_ref="fake-session",
        started_at=_utc_now(),
        completed_at=_utc_now(),
    )


class FileWritingWorkerProvider(FakeAgentProvider):
    """A FakeAgentProvider that also mutates the workspace it is given.

    ``FakeAgentProvider`` never touches the filesystem (Phase 1.1 is a
    conformance double, not a simulator), but this pipeline's freeze/
    host-validate step (Phase 3.3) needs *something* to have changed on
    disk to exercise the manifest diff. A real Worker adapter changes
    files by actually running tools; this stand-in does the same thing
    directly, using ``request.workspace_handle`` — the same field a real
    adapter would resolve tool calls against.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._call_count = 0

    async def start_invocation(self, session, request):
        self._call_count += 1
        workspace = Path(request.workspace_handle)
        # A distinct filename per call so each rework attempt's manifest
        # diff shows a real, fresh change instead of re-writing identical
        # bytes over an unchanged rolling baseline. Written under src/ so
        # it actually falls inside the default contract scope
        # (allowed_path_rules=["src/**", "tests/**"]) now that
        # freeze_and_validate/run_verification enforce that for real
        # (Codex review M-01) — writing at the worktree root would be a
        # genuine, correctly-flagged scope violation.
        src_dir = workspace / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / f"attempt_{self._call_count}.txt").write_bytes(b"written by the fake worker\n")
        return await super().start_invocation(session, request)


class EvidenceCitingVerifierFactory:
    """Builds a fresh Verifier ``FakeAgentProvider`` per call.

    Mirrors ``run_verification``'s "fresh provider + fresh session"
    contract (Phase 8, H-06): every call gets a brand-new provider
    instance. The scripted ``VerificationResult`` cites whatever real
    evidence IDs ended up in the prompt (parsed back out of the prompt
    text via ``resolve_prompt`` — the actual evidence IDs are only known
    once ``freeze_and_validate`` has run, which is *after* this
    factory's construction but *before* it's called), so
    ``find_missing_evidence_violations`` sees genuine, resolvable refs.
    """

    def __init__(self, *, decision: str, criterion_id: str = "crit-1") -> None:
        self.decision = decision
        self.criterion_id = criterion_id

    def __call__(self, resolve_prompt):
        decision = self.decision
        criterion_id = self.criterion_id

        class _Provider(FakeAgentProvider):
            async def start_invocation(self, session, request):
                prompt_text = await resolve_prompt(request.prompt_payload_artifact_ref)
                evidence_ids = _extract_evidence_ids(prompt_text)
                verification_json = {
                    "schema_version": "1.0",
                    "verification_id": new_id(),
                    "task_id": new_id(),
                    "contract_digest": request.task_contract_digest,
                    "result_snapshot_digest": VALID_DIGEST,
                    "evidence_set_digest": VALID_DIGEST,
                    "invocation_id": new_id(),
                    "decision": decision,
                    "criteria": [
                        {
                            "id": criterion_id,
                            "result": "PASS" if decision == "PASS" else "FAIL",
                            "evidence_refs": evidence_ids if decision == "PASS" else [],
                        }
                    ],
                    "scope_violations": [],
                    "security_findings": [],
                    "quality_findings": [],
                    "required_fixes": [] if decision != "REWORK" else ["fix it"],
                    "prohibited_changes": [],
                    "remaining_risks": [] if decision != "REJECT" else ["too risky"],
                }
                self.queue_invocation(
                    AgentRole.VERIFIER, ScriptedInvocation(events=[], result=make_result(verification_json))
                )
                return await super().start_invocation(session, request)

        return _Provider(capabilities=make_capabilities(AgentRole.VERIFIER))


class NoEvidenceVerifierFactory:
    """A Verifier that claims PASS but cites no evidence at all — should
    be downgraded to MANUAL_REVIEW by Phase 8's deterministic gate."""

    def __call__(self, resolve_prompt):
        verification_json = {
            "schema_version": "1.0",
            "verification_id": new_id(),
            "task_id": new_id(),
            "contract_digest": VALID_DIGEST,
            "result_snapshot_digest": VALID_DIGEST,
            "evidence_set_digest": VALID_DIGEST,
            "invocation_id": new_id(),
            "decision": "PASS",
            "criteria": [{"id": "crit-1", "result": "PASS", "evidence_refs": []}],
            "scope_violations": [],
            "security_findings": [],
            "quality_findings": [],
            "required_fixes": [],
            "prohibited_changes": [],
            "remaining_risks": [],
        }
        provider = FakeAgentProvider(capabilities=make_capabilities(AgentRole.VERIFIER))
        provider.queue_invocation(
            AgentRole.VERIFIER, ScriptedInvocation(events=[], result=make_result(verification_json))
        )
        return provider


class SequencedVerifierFactory:
    """Builds a fresh Verifier ``FakeAgentProvider`` per call (Phase 8's
    fresh-provider contract), returning the next scripted verdict from a
    fixed program list — one entry per expected verify attempt across a
    rework loop.

    Each program is ``{"decision": "REWORK"|"PASS", "criterion_id": str,
    "required_fixes": list[str], "requested_additional_capabilities":
    dict | None}``. A ``"PASS"`` program cites whatever real evidence IDs
    ended up in its own prompt (same trick as ``EvidenceCitingVerifierFactory``);
    a ``"REWORK"`` program just needs a non-empty ``required_fixes``.
    """

    def __init__(self, programs: list[dict]) -> None:
        self._programs = list(programs)
        self.calls = 0

    def __call__(self, resolve_prompt):
        program = self._programs[self.calls]
        self.calls += 1
        decision = program["decision"]
        criterion_id = program.get("criterion_id", "crit-1")

        class _Provider(FakeAgentProvider):
            async def start_invocation(self, session, request):
                if decision == "PASS":
                    prompt_text = await resolve_prompt(request.prompt_payload_artifact_ref)
                    evidence_ids = _extract_evidence_ids(prompt_text)
                    criteria = [{"id": criterion_id, "result": "PASS", "evidence_refs": evidence_ids}]
                    required_fixes: list[str] = []
                else:
                    criteria = [{"id": criterion_id, "result": "FAIL", "evidence_refs": []}]
                    required_fixes = program.get("required_fixes", ["fix it"])
                verification_json = {
                    "schema_version": "1.0",
                    "verification_id": new_id(),
                    "task_id": new_id(),
                    "contract_digest": request.task_contract_digest,
                    "result_snapshot_digest": VALID_DIGEST,
                    "evidence_set_digest": VALID_DIGEST,
                    "invocation_id": new_id(),
                    "decision": decision,
                    "criteria": criteria,
                    "scope_violations": [],
                    "security_findings": [],
                    "quality_findings": [],
                    "required_fixes": required_fixes,
                    "prohibited_changes": program.get("prohibited_changes", []),
                    "remaining_risks": [],
                }
                self.queue_invocation(
                    AgentRole.VERIFIER, ScriptedInvocation(events=[], result=make_result(verification_json))
                )
                return await super().start_invocation(session, request)

        return _Provider(capabilities=make_capabilities(AgentRole.VERIFIER))


def _extract_evidence_ids(prompt_text: str) -> list[str]:
    marker = "EVIDENCE RECORDS (host-observed):\n"
    start = prompt_text.index(marker) + len(marker)
    end = prompt_text.index("\n\n", start)
    records = json.loads(prompt_text[start:end])
    return [record["evidence_id"] for record in records]


def make_check_spec(command_id: str = "check") -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        executable_identity=sys.executable,
        argv_template=[sys.executable, "-c", "print('ok')"],
        cwd_policy="WORKSPACE_ROOT",
        env_allowlist=["PATH"],
        timeout_seconds=10,
        policy_version="v1",
    )


def make_deps(
    tmp_path: Path,
    conn,
    source_repo: Path,
    *,
    verifier_provider_factory,
    worker_provider_cls=FileWritingWorkerProvider,
    worker_result_dicts: list[dict] | None = None,
    ceiling_overrides: dict | None = None,
    decide_policy_approval=None,
    decide_final_approval=None,
) -> PipelineDeps:
    data_root = tmp_path / "data-root"
    git_client = GitClient(data_root / "empty-hooks")
    catalog = CommandCatalog()
    catalog.register(make_check_spec())

    planner_provider = FakeAgentProvider(capabilities=make_capabilities(AgentRole.PLANNER))
    worker_provider = worker_provider_cls(capabilities=make_capabilities(AgentRole.WORKER))
    for structured_output in worker_result_dicts or [
        {"status": "COMPLETED", "implementation_summary": "Added new_file.txt"}
    ]:
        worker_provider.queue_invocation(
            AgentRole.WORKER,
            ScriptedInvocation(events=[], result=make_result(structured_output)),
        )

    providers = {AgentRole.PLANNER: planner_provider, AgentRole.WORKER: worker_provider}

    ceiling_kwargs = dict(allowed_command_ids=frozenset({"check"}))
    if ceiling_overrides:
        ceiling_kwargs.update(ceiling_overrides)

    kwargs = dict(
        conn=conn,
        data_root=data_root,
        git_client=git_client,
        source_repo_path=source_repo,
        provider_for_role=lambda role: providers[role],
        verifier_provider_factory=verifier_provider_factory,
        policy_ceiling=make_policy_ceiling(**ceiling_kwargs),
        command_catalog=catalog,
        sandbox=TrustedLocalSandbox(),
        check_command_ids=["check"],
        test_path_patterns=["tests/**"],
        available_env=dict(os.environ),
        role_profile_digest=VALID_DIGEST,
        prompt_registry=PromptRegistry(),
    )
    async def _auto_approve(_: object) -> bool:
        return True

    # PipelineDeps itself defaults both callbacks to fail-closed deny
    # (Codex review B-03) — these Fake E2E tests are about pipeline
    # plumbing, not about that safety property, so this test helper
    # supplies an explicit approve-everything default unless a test
    # overrides it. The deny-by-default behavior itself is covered by
    # test_pipeline_deps_defaults_to_denying_every_approval below.
    kwargs["decide_policy_approval"] = decide_policy_approval or _auto_approve
    kwargs["decide_final_approval"] = decide_final_approval or _auto_approve

    return PipelineDeps(**kwargs), planner_provider


def queue_planner_contract(planner_provider: FakeAgentProvider, contract) -> None:
    planner_provider.queue_invocation(
        AgentRole.PLANNER,
        ScriptedInvocation(events=[], result=make_result(contract.model_dump(mode="json"))),
    )


def seed_run_task_contract(tmp_path: Path, db_conn, source_repo: Path, **contract_overrides):
    """Shared setup: a real HEAD-pinned repo + inserted Run/Task + a
    matching TaskContract (not yet inserted anywhere — the Planner "proposes"
    it via ``queue_planner_contract``)."""

    from agent_harness.domain.models import Task

    git_client = GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    data = dict(
        run_id=run.run_id,
        task_id=task.task_id,
        repository={
            "repository_id": "repo-1",
            "base_commit_sha": head_sha,
            "target_ref": "refs/heads/main",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
        # command_id must match what make_deps() actually registers/runs
        # ("check") — the factory default ("pytest") is never actually
        # executed by these Fake E2E tests, and the deterministic
        # check_execution gate (Codex review B-02) now correctly rejects
        # a PASS whose criterion's command was never run.
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ],
        requested_capabilities={
            "workspace_access": "READ_WRITE",
            "command_ids": ["check"],
            "raw_shell": False,
            "network_domains": [],
            "package_install": False,
            "external_systems": [],
            "mcp_tools": [],
            "database_targets": [],
        },
    )
    data.update(contract_overrides)
    contract = make_task_contract(**data)
    return run, task, contract


@pytest.fixture()
def source_repo(tmp_path) -> Path:
    return make_repo(tmp_path, "source-repo")


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "harness.db")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B-03 (Codex review): approval callbacks must fail closed by default
# ---------------------------------------------------------------------------


def test_pipeline_deps_defaults_to_denying_every_approval(tmp_path, db_conn, source_repo):
    """A caller that forgets to wire a real approval callback must never
    get an implicit approval — PipelineDeps' own defaults must deny."""

    import asyncio

    deps = PipelineDeps(
        conn=db_conn, data_root=tmp_path / "data-root", git_client=GitClient(tmp_path / "hooks"),
        source_repo_path=source_repo, provider_for_role=lambda role: None,
        verifier_provider_factory=lambda resolve_prompt: None,
        policy_ceiling=make_policy_ceiling(), command_catalog=CommandCatalog(),
        sandbox=TrustedLocalSandbox(), check_command_ids=[], test_path_patterns=[],
        available_env={}, role_profile_digest=VALID_DIGEST,
    )
    assert asyncio.run(deps.decide_policy_approval(object())) is False
    assert asyncio.run(deps.decide_final_approval(object())) is False


# ---------------------------------------------------------------------------
# B-07 (Codex review): a hung provider call must not block a Run forever
# ---------------------------------------------------------------------------


class HangingProvider:
    """A minimal AgentProvider whose await_result() never returns on its
    own — proves _invoke_role's asyncio.wait_for actually enforces the
    budget's timeout_seconds, rather than the deadline being decorative
    metadata nobody reads (Codex review B-07)."""

    def __init__(self) -> None:
        self.cancel_called = False

    @property
    def provider_id(self) -> str:
        return "hanging-provider"

    @property
    def provider_version(self) -> str:
        return "0.0.0"

    async def health_check(self):
        raise NotImplementedError

    async def capabilities(self):
        raise NotImplementedError

    async def start_session(self, request):
        from agent_harness.providers.protocol import ProviderSessionRef

        return ProviderSessionRef(opaque_ref="hang-session", provider_id=self.provider_id, role=request.role)

    async def resume_session(self, request):
        raise NotImplementedError

    async def start_invocation(self, session, request):
        from agent_harness.providers.protocol import ProviderInvocationRef

        return ProviderInvocationRef(opaque_ref="hang-invocation", provider_id=self.provider_id)

    def stream_events(self, invocation, *, after_cursor=None):
        raise NotImplementedError

    async def await_result(self, invocation):
        await asyncio.sleep(3600)  # would hang forever without an enforced timeout
        raise AssertionError("unreachable — the timeout should have cancelled this first")

    async def cancel(self, invocation, request):
        from agent_harness.providers.protocol import CancelResult

        self.cancel_called = True
        return CancelResult(invocation_id=request.invocation_id, protocol_status=ProtocolStatus.CANCELLED, cancelled_at=_utc_now())

    async def close_session(self, session) -> None:
        return None


def test_invoke_role_enforces_budget_timeout_and_cancels():
    provider = HangingProvider()
    budget = BudgetRequest(timeout_seconds=1, max_turns=5, max_rework_iterations=0)

    async def scenario():
        return await _invoke_role(
            provider, AgentRole.WORKER, prompt="do something", prompt_registry=PromptRegistry(),
            task_contract_ref="contract-1", task_contract_digest=VALID_DIGEST,
            output_schema_id="worker_result", effective_policy_grants=make_policy_grants(),
            workspace_handle=".", role_profile_ref="worker-profile", role_profile_digest=VALID_DIGEST,
            budget=budget, correlation_id="corr-1",
        )

    with pytest.raises(PipelineTimeoutError):
        asyncio.run(scenario())
    assert provider.cancel_called is True


def test_pipeline_fails_cleanly_when_budget_already_exhausted_before_planning(tmp_path, db_conn, source_repo):
    from agent_harness.domain.models import BudgetUsage, Task

    run = make_run(repository_id="repo-1", budget_used=BudgetUsage(turns_used=999))
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    deps, _ = make_deps(
        tmp_path, db_conn, source_repo,
        verifier_provider_factory=EvidenceCitingVerifierFactory(decision="PASS"),
        ceiling_overrides=dict(budget_ceiling=BudgetRequest(timeout_seconds=600, max_turns=1, max_rework_iterations=1)),
    )

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.FAILED
    from agent_harness.persistence.sqlite import get_failure_records

    failures = get_failure_records(db_conn, run.run_id)
    assert len(failures) == 1
    assert "budget exceeded" in failures[0].sanitized_detail


# ---------------------------------------------------------------------------
# B-01 (Codex review): Worker gets real Claude tool names, not command_ids,
# and its prompt actually states the scope it must respect
# ---------------------------------------------------------------------------


def test_claude_tool_ids_are_real_sdk_tool_names_not_command_ids():
    from agent_harness.domain.enums import WorkspaceAccessLevel

    read_write_grants = make_policy_grants(workspace_access=WorkspaceAccessLevel.READ_WRITE)
    tool_ids = _claude_tool_ids_for(read_write_grants)

    assert "pytest" not in tool_ids  # a command_id, never a Claude tool name
    assert "Write" in tool_ids and "Edit" in tool_ids and "Read" in tool_ids
    assert "Bash" not in tool_ids  # fail-closed: no PolicyGrants field tracks raw_shell yet

    assert _claude_tool_ids_for(make_policy_grants(workspace_access=WorkspaceAccessLevel.NONE)) == []
    read_only = _claude_tool_ids_for(make_policy_grants(workspace_access=WorkspaceAccessLevel.READ))
    assert "Write" not in read_only and "Read" in read_only


def test_worker_prompt_states_allowed_and_forbidden_paths():
    contract = make_task_contract(
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ]
    )
    prompt = _build_worker_prompt(contract, None)

    assert "ALLOWED PATHS" in prompt
    assert "FORBIDDEN PATHS" in prompt
    for pattern in contract.scope.allowed_path_rules:
        assert pattern in prompt
    assert "ACCEPTANCE CRITERIA" in prompt
    assert "crit-1" in prompt


# ---------------------------------------------------------------------------
# H-01 (Codex review): Planner cannot override authoritative repository info
# ---------------------------------------------------------------------------


def test_planner_proposed_repository_info_is_overridden_by_the_harness(tmp_path, db_conn, source_repo):
    """A malicious/buggy Planner claims a bogus base_commit_sha and a
    different repository_id in its TaskContract proposal. The pipeline
    must still succeed by pinning to the Harness's own resolved HEAD —
    if the Planner's claim were used, worktree creation would fail
    outright (the bogus SHA does not exist in this repo)."""

    run, task, contract = seed_run_task_contract(
        tmp_path, db_conn, source_repo,
        repository={
            "repository_id": "attacker-controlled-repo-id",
            "base_commit_sha": "a" * 40,  # well-formed but does not exist in source_repo
            "target_ref": "refs/heads/malicious-branch",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
    )

    verifier_factory = EvidenceCitingVerifierFactory(decision="PASS")
    deps, planner_provider = make_deps(tmp_path, db_conn, source_repo, verifier_provider_factory=verifier_factory)
    queue_planner_contract(planner_provider, contract)

    import asyncio

    from agent_harness.execution.git_client import GitClient as _GitClient
    from agent_harness.execution.workspace import worktree_path_for

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.READY_FOR_MERGE
    # The worktree was created under the Run's real repository_id, not
    # the Planner's claimed one.
    real_head_sha = _GitClient(tmp_path / "hooks").rev_parse("HEAD", cwd=source_repo)
    worktree_path = worktree_path_for(tmp_path / "data-root", repository_id=run.repository_id, run_id=run.run_id)
    assert worktree_path.exists()
    checked_out_sha = _GitClient(tmp_path / "hooks").rev_parse("HEAD", cwd=worktree_path)
    assert checked_out_sha == real_head_sha


# ---------------------------------------------------------------------------
# Happy path: CREATED -> ... -> READY_FOR_MERGE
# ---------------------------------------------------------------------------


def test_fake_e2e_pipeline_reaches_ready_for_merge(tmp_path, db_conn, source_repo):
    from agent_harness.execution.git_client import GitClient as _GitClient
    from agent_harness.domain.models import Task

    git_client = _GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    contract = make_task_contract(
        run_id=run.run_id,
        task_id=task.task_id,
        repository={
            "repository_id": "repo-1",
            "base_commit_sha": head_sha,
            "target_ref": "refs/heads/main",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ],
        requested_capabilities={
            "workspace_access": "READ_WRITE",
            "command_ids": ["check"],
            "raw_shell": False,
            "network_domains": [],
            "package_install": False,
            "external_systems": [],
            "mcp_tools": [],
            "database_targets": [],
        },
    )

    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=EvidenceCitingVerifierFactory(decision="PASS"),
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.READY_FOR_MERGE
    assert final_run.disposition is LifecycleState.READY_FOR_MERGE

    entries = list_journal_entries(db_conn, run.run_id)
    observed_states = [e.state_after for e in entries]
    assert observed_states == [
        LifecycleState.PLANNING,
        LifecycleState.CONTRACT_VALIDATING,
        LifecycleState.PREPARING_WORKSPACE,
        LifecycleState.EXECUTING,
        LifecycleState.FREEZING_RESULT,
        LifecycleState.HOST_VALIDATING,
        LifecycleState.VERIFYING,
        LifecycleState.AWAITING_FINAL_APPROVAL,
        LifecycleState.READY_FOR_MERGE,
    ]
    # Journal is hash-chained: every entry after the first links to the
    # previous one's hash (Phase 2.1) — a cheap end-to-end integrity check.
    for previous, current in zip(entries, entries[1:]):
        assert current.previous_entry_hash == previous.entry_hash


def test_fake_e2e_pipeline_persists_contract_decision_and_verification(tmp_path, db_conn, source_repo):
    """Codex review B-04 (partial): the accepted TaskContract, every
    PolicyDecision, and the Verifier's VerificationResult must all be
    recoverable from the DB after this process exits, not just held in
    the orchestrator's local variables for the run's lifetime."""

    from agent_harness.execution.git_client import GitClient as _GitClient
    from agent_harness.domain.models import Task
    from agent_harness.persistence.sqlite import (
        list_policy_decisions_for_run,
        list_task_contracts_for_run,
        list_verification_results_for_run,
    )

    git_client = _GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    contract = make_task_contract(
        run_id=run.run_id,
        task_id=task.task_id,
        repository={
            "repository_id": "repo-1",
            "base_commit_sha": head_sha,
            "target_ref": "refs/heads/main",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ],
        requested_capabilities={
            "workspace_access": "READ_WRITE", "command_ids": ["check"], "raw_shell": False,
            "network_domains": [], "package_install": False, "external_systems": [],
            "mcp_tools": [], "database_targets": [],
        },
    )
    deps, planner_provider = make_deps(
        tmp_path, db_conn, source_repo, verifier_provider_factory=EvidenceCitingVerifierFactory(decision="PASS"),
    )
    queue_planner_contract(planner_provider, contract)

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )
    assert final_run.state is LifecycleState.READY_FOR_MERGE

    persisted_contracts = list_task_contracts_for_run(db_conn, run.run_id)
    assert len(persisted_contracts) == 1
    assert persisted_contracts[0].objective == contract.objective

    persisted_decisions = list_policy_decisions_for_run(db_conn, run.run_id)
    assert len(persisted_decisions) == 1
    assert persisted_decisions[0].outcome is PolicyOutcome.ALLOW

    persisted_verifications = list_verification_results_for_run(db_conn, run.run_id)
    assert len(persisted_verifications) == 1
    assert persisted_verifications[0].decision is VerificationDecision.PASS


# ---------------------------------------------------------------------------
# Policy REQUIRE_APPROVAL branch
# ---------------------------------------------------------------------------


def test_fake_e2e_pipeline_goes_through_awaiting_approval(tmp_path, db_conn, source_repo):
    from agent_harness.execution.git_client import GitClient as _GitClient
    from agent_harness.domain.models import Task

    git_client = _GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    contract = make_task_contract(
        run_id=run.run_id,
        task_id=task.task_id,
        repository={
            "repository_id": "repo-1",
            "base_commit_sha": head_sha,
            "target_ref": "refs/heads/main",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ],
        requested_capabilities={
            "workspace_access": "READ_WRITE",
            "command_ids": ["check"],
            "raw_shell": True,  # forces PolicyOutcome.REQUIRE_APPROVAL
            "network_domains": [],
            "package_install": False,
            "external_systems": [],
            "mcp_tools": [],
            "database_targets": [],
        },
    )

    approval_calls: list[object] = []

    async def approve(decision) -> bool:
        approval_calls.append(decision)
        return True

    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=EvidenceCitingVerifierFactory(decision="PASS"),
        ceiling_overrides=dict(raw_shell_allowed=True),
        decide_policy_approval=approve,
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert len(approval_calls) == 1
    assert final_run.state is LifecycleState.READY_FOR_MERGE

    entries = list_journal_entries(db_conn, run.run_id)
    observed_states = [e.state_after for e in entries]
    assert LifecycleState.AWAITING_APPROVAL in observed_states
    assert observed_states.index(LifecycleState.AWAITING_APPROVAL) == observed_states.index(
        LifecycleState.CONTRACT_VALIDATING
    ) + 1


# ---------------------------------------------------------------------------
# Verifier PASS-with-no-evidence -> MANUAL_REVIEW
# ---------------------------------------------------------------------------


def test_fake_e2e_pipeline_stops_at_manual_review_on_unsupported_pass(tmp_path, db_conn, source_repo):
    from agent_harness.execution.git_client import GitClient as _GitClient
    from agent_harness.domain.models import Task

    git_client = _GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a new file")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    contract = make_task_contract(
        run_id=run.run_id,
        task_id=task.task_id,
        repository={
            "repository_id": "repo-1",
            "base_commit_sha": head_sha,
            "target_ref": "refs/heads/main",
            "expected_repository_fingerprint": VALID_DIGEST,
        },
        acceptance_criteria=[
            make_acceptance_criterion("crit-1", mandatory=True, verification=make_verification_spec(command_id="check"))
        ],
        requested_capabilities={
            "workspace_access": "READ_WRITE",
            "command_ids": ["check"],
            "raw_shell": False,
            "network_domains": [],
            "package_install": False,
            "external_systems": [],
            "mcp_tools": [],
            "database_targets": [],
        },
    )

    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=NoEvidenceVerifierFactory(),
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.AWAITING_MANUAL_REVIEW
    assert final_run.disposition is None  # not a terminal state

    entries = list_journal_entries(db_conn, run.run_id)
    assert entries[-1].state_after is LifecycleState.AWAITING_MANUAL_REVIEW


# ---------------------------------------------------------------------------
# Phase 10: rework loop
# ---------------------------------------------------------------------------


def test_fake_e2e_pipeline_recovers_from_rework_and_reaches_ready_for_merge(tmp_path, db_conn, source_repo):
    run, task, contract = seed_run_task_contract(tmp_path, db_conn, source_repo)

    verifier_factory = SequencedVerifierFactory(
        [
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["fix the assertion"]},
            {"decision": "PASS", "criterion_id": "crit-1"},
        ]
    )
    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=verifier_factory,
        worker_result_dicts=[
            {"status": "COMPLETED", "implementation_summary": "first attempt"},
            {"status": "COMPLETED", "implementation_summary": "fixed per rework"},
        ],
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.READY_FOR_MERGE
    assert verifier_factory.calls == 2

    entries = list_journal_entries(db_conn, run.run_id)
    observed_states = [e.state_after for e in entries]
    assert observed_states == [
        LifecycleState.PLANNING,
        LifecycleState.CONTRACT_VALIDATING,
        LifecycleState.PREPARING_WORKSPACE,
        LifecycleState.EXECUTING,
        LifecycleState.FREEZING_RESULT,
        LifecycleState.HOST_VALIDATING,
        LifecycleState.VERIFYING,
        LifecycleState.REWORK_CONTRACTING,
        LifecycleState.CONTRACT_VALIDATING,
        LifecycleState.PREPARING_WORKSPACE,
        LifecycleState.EXECUTING,
        LifecycleState.FREEZING_RESULT,
        LifecycleState.HOST_VALIDATING,
        LifecycleState.VERIFYING,
        LifecycleState.AWAITING_FINAL_APPROVAL,
        LifecycleState.READY_FOR_MERGE,
    ]


def test_fake_e2e_pipeline_stops_with_rework_exhausted_at_max_iterations(tmp_path, db_conn, source_repo):
    run, task, contract = seed_run_task_contract(
        tmp_path,
        db_conn,
        source_repo,
        budget_request={"timeout_seconds": 600, "max_turns": 10, "max_rework_iterations": 1},
    )

    verifier_factory = SequencedVerifierFactory(
        [
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["fix A"]},
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["fix B"]},
        ]
    )
    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=verifier_factory,
        worker_result_dicts=[
            {"status": "COMPLETED", "implementation_summary": "attempt 1"},
            {"status": "COMPLETED", "implementation_summary": "attempt 2, different change"},
        ],
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.FAILED
    assert final_run.disposition is LifecycleState.FAILED
    assert verifier_factory.calls == 2

    entries = list_journal_entries(db_conn, run.run_id)
    assert entries[-1].state_after is LifecycleState.FAILED
    assert entries[-1].payload_json["failure_id"]


def test_fake_e2e_pipeline_stops_with_rework_exhausted_on_no_progress(tmp_path, db_conn, source_repo):
    run, task, contract = seed_run_task_contract(
        tmp_path,
        db_conn,
        source_repo,
        budget_request={"timeout_seconds": 600, "max_turns": 10, "max_rework_iterations": 5},
    )

    # Same criterion fails, verbatim, on two consecutive attempts — no
    # progress, even though the iteration budget (5) has plenty of room left.
    verifier_factory = SequencedVerifierFactory(
        [
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["fix it"]},
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["fix it again"]},
        ]
    )
    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=verifier_factory,
        worker_result_dicts=[
            {"status": "COMPLETED", "implementation_summary": "attempt 1"},
            {"status": "COMPLETED", "implementation_summary": "attempt 2, no real progress"},
        ],
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.FAILED
    assert verifier_factory.calls == 2


def test_fake_e2e_pipeline_rework_capability_expansion_requires_approval(tmp_path, db_conn, source_repo):
    run, task, contract = seed_run_task_contract(tmp_path, db_conn, source_repo)

    verifier_factory = SequencedVerifierFactory(
        [
            {"decision": "REWORK", "criterion_id": "crit-1", "required_fixes": ["need raw shell to fix it"]},
            {"decision": "PASS", "criterion_id": "crit-1"},
        ]
    )

    policy_decisions_seen: list = []

    async def approve(decision) -> bool:
        policy_decisions_seen.append(decision)
        return True

    deps, planner_provider = make_deps(
        tmp_path,
        db_conn,
        source_repo,
        verifier_provider_factory=verifier_factory,
        worker_result_dicts=[
            {
                "status": "COMPLETED",
                "implementation_summary": "attempt 1, need more access",
                "requested_additional_capabilities": {
                    "workspace_access": "READ_WRITE",
                    "command_ids": ["check"],
                    "raw_shell": True,
                    "network_domains": [],
                    "package_install": False,
                    "external_systems": [],
                    "mcp_tools": [],
                    "database_targets": [],
                },
            },
            {"status": "COMPLETED", "implementation_summary": "attempt 2, fixed with shell access"},
        ],
        ceiling_overrides=dict(raw_shell_allowed=True),
        decide_policy_approval=approve,
    )
    queue_planner_contract(planner_provider, contract)

    import asyncio

    final_run = asyncio.run(
        run_task_pipeline(deps, run=run, task=task, user_request_text="Add a new file to the repo")
    )

    assert final_run.state is LifecycleState.READY_FOR_MERGE
    # Only the rework's capability expansion needed approval — the initial
    # TaskContract requested no gated capability, so it went straight to ALLOW.
    assert len(policy_decisions_seen) == 1
    from agent_harness.domain.enums import SubjectType as _SubjectType

    assert policy_decisions_seen[0].subject_type is _SubjectType.REWORK_CONTRACT

    entries = list_journal_entries(db_conn, run.run_id)
    observed_states = [e.state_after for e in entries]
    assert observed_states.count(LifecycleState.AWAITING_APPROVAL) == 1
    # The approval sits between the rework's CONTRACT_VALIDATING and its PREPARING_WORKSPACE.
    approval_index = observed_states.index(LifecycleState.AWAITING_APPROVAL)
    assert observed_states[approval_index - 1] is LifecycleState.CONTRACT_VALIDATING
    assert observed_states[approval_index + 1] is LifecycleState.PREPARING_WORKSPACE


# ---------------------------------------------------------------------------
# Opt-in live E2E (real Claude Worker + real Codex Planner/Verifier)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_DUAL_AGENT_E2E") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY")
    or not os.environ.get("OPENAI_API_KEY"),
    reason="opt-in: set RUN_LIVE_DUAL_AGENT_E2E=1 and export ANTHROPIC_API_KEY/OPENAI_API_KEY to run",
)
def test_live_dual_agent_pipeline_smoke(tmp_path, db_conn, source_repo):
    """A minimal live run through real Claude (Worker) + real Codex
    (Planner/Verifier) adapters, wired through the same orchestrator.

    Skipped unless the runner already exported real
    ``ANTHROPIC_API_KEY``/``OPENAI_API_KEY`` (see the ``skipif`` above) —
    this test never reads a credential file itself, matching the
    Phase 6/7 live-smoke pattern: the *caller* injects the key via shell
    env, this test only ever checks that the env var is present. No
    user-specific path is hardcoded here (Codex review H-04).
    """

    import asyncio

    from agent_harness.providers.claude import ClaudeAgentAdapter
    from agent_harness.providers.codex import CodexPlannerAdapter
    from agent_harness.domain.models import Task

    git_client = GitClient(tmp_path / "data-root" / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    run = make_run(repository_id="repo-1")
    task = Task(run_id=run.run_id, objective="Add a README note")
    insert_run(db_conn, run)
    insert_task(db_conn, task)

    registry = PromptRegistry()
    planner_provider = CodexPlannerAdapter(resolve_prompt=registry.resolve)
    worker_provider = ClaudeAgentAdapter(
        resolve_prompt=registry.resolve, resolve_workspace_handle=lambda handle: handle
    )

    async def auto_approve(_: object) -> bool:
        return True

    deps = PipelineDeps(
        conn=db_conn,
        data_root=tmp_path / "data-root",
        git_client=git_client,
        source_repo_path=source_repo,
        provider_for_role=lambda role: {
            AgentRole.PLANNER: planner_provider,
            AgentRole.WORKER: worker_provider,
        }[role],
        verifier_provider_factory=lambda resolve_prompt: CodexPlannerAdapter(
            resolve_prompt=resolve_prompt
        ),
        policy_ceiling=make_policy_ceiling(allowed_command_ids=frozenset({"check"})),
        command_catalog=CommandCatalog(),
        sandbox=TrustedLocalSandbox(),
        check_command_ids=[],
        test_path_patterns=["tests/**"],
        available_env=dict(os.environ),
        role_profile_digest=VALID_DIGEST,
        prompt_registry=registry,
        decide_policy_approval=auto_approve,
        decide_final_approval=auto_approve,
    )

    final_run = asyncio.run(
        run_task_pipeline(
            deps, run=run, task=task, user_request_text="Add a one-line note to README.md"
        )
    )

    # A real success condition, not just "the pipeline returned something"
    # (Codex review H-04: the old `is not None` assertion passed even on
    # FAILED). If this fails on a real infra/billing issue, that is the
    # correct, honest signal for an opt-in live test — not something to
    # paper over with a weaker assertion.
    assert final_run.state is LifecycleState.READY_FOR_MERGE, (
        f"expected READY_FOR_MERGE, got {final_run.state.value} "
        f"(disposition={final_run.disposition})"
    )

    from agent_harness.persistence.sqlite import list_evidence_for_run, list_journal_entries

    journal_entries = list_journal_entries(db_conn, run.run_id)
    assert journal_entries[-1].state_after is LifecycleState.READY_FOR_MERGE
    assert len(list_evidence_for_run(db_conn, run.run_id)) > 0
