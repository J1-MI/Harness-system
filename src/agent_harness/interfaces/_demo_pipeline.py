"""Backing implementation for ``harness run`` (Phase 12).

Demo/fake-provider path only: wires ``application.orchestrator.run_task_pipeline``
end to end against a real local git repo, using ``providers.fake.FakeAgentProvider``
for all three roles with a trivial, always-succeeding scripted contract —
enough to prove the CLI is wired to the real orchestrator (not a parallel
mock path), not a substitute for real Claude/Codex automation. See
``docs/architecture/cli-and-reporting.md`` for why real-provider credential
wiring is out of scope for this phase.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from agent_harness.application.orchestrator import PipelineDeps, run_task_pipeline
from agent_harness.application.verification import PromptRegistry
from agent_harness.domain.digests import compute_digest, new_id
from agent_harness.domain.enums import (
    AgentRole,
    DriverKind,
    McpControlSupport,
    SessionResumeSupport,
    StreamingSupport,
    StructuredOutputSupport,
    UsageReportingSupport,
)
from agent_harness.domain.models import (
    AcceptanceCriterion,
    BudgetRequest,
    BudgetUsage,
    CommandSpec,
    ConstraintSet,
    IntegrityRef,
    RepositoryRef,
    RequestedCapabilities,
    Run,
    ScopeRules,
    Task,
    TaskContract,
    VerificationSpec,
)
from agent_harness.execution.command_broker import CommandCatalog
from agent_harness.execution.git_client import GitClient
from agent_harness.execution.sandbox import TrustedLocalSandbox
from agent_harness.persistence.sqlite import insert_run, insert_task
from agent_harness.policy.models import PolicyCeiling
from agent_harness.providers.fake import FakeAgentProvider, ScriptedInvocation
from agent_harness.providers.protocol import AgentRunResult, ProtocolStatus, ProviderCapabilities

__all__ = ["run_demo_pipeline"]

VALID_DIGEST = "sha256:" + "0" * 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_capabilities(role: AgentRole) -> ProviderCapabilities:
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
        driver_version="0.0.0-cli-demo",
        capability_probe_timestamp=_utc_now(),
    )


def _make_result(structured_output: dict) -> AgentRunResult:
    return AgentRunResult(
        invocation_id=new_id(), protocol_status=ProtocolStatus.SUCCEEDED,
        structured_output=structured_output, provider_session_ref="cli-demo-session",
        started_at=_utc_now(), completed_at=_utc_now(),
    )


def _make_demo_command_catalog() -> CommandCatalog:
    catalog = CommandCatalog()
    catalog.register(
        CommandSpec(
            command_id="demo-check", executable_identity=sys.executable,
            argv_template=[sys.executable, "-c", "print('demo check ok')"],
            cwd_policy="WORKSPACE_ROOT", env_allowlist=["PATH"], timeout_seconds=10, policy_version="v1",
        )
    )
    return catalog


def _make_demo_contract(run_id: str, task_id: str, base_commit_sha: str, request_text: str) -> TaskContract:
    return TaskContract(
        contract_revision=1, run_id=run_id, task_id=task_id,
        request_snapshot_ref="cli-demo-request", objective=request_text,
        repository=RepositoryRef(
            repository_id="cli-demo-repo", base_commit_sha=base_commit_sha,
            target_ref="refs/heads/main", expected_repository_fingerprint=VALID_DIGEST,
        ),
        scope=ScopeRules(allowed_path_rules=["**"], forbidden_path_rules=[".git/**"]),
        constraints=ConstraintSet(),
        acceptance_criteria=[
            AcceptanceCriterion(
                id="demo-crit-1", description="demo check command exits 0", mandatory=True,
                verification=VerificationSpec(
                    method="COMMAND", command_id="demo-check", expected_exit_codes=[0],
                    required_evidence_kinds=["command_exit_code"],
                ),
            )
        ],
        requested_capabilities=RequestedCapabilities(
            workspace_access="READ_WRITE", command_ids=["demo-check"], raw_shell=False,
            network_domains=[], package_install=False, external_systems=[], mcp_tools=[], database_targets=[],
        ),
        budget_request=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        integrity=IntegrityRef(canonical_digest=VALID_DIGEST),
    )


def _make_demo_policy_ceiling() -> PolicyCeiling:
    return PolicyCeiling(
        policy_bundle_id="cli-demo-bundle", policy_bundle_digest=VALID_DIGEST, engine_version="0.1.0",
        workspace_scope=ScopeRules(allowed_path_rules=["**"], forbidden_path_rules=[".git/**"]),
        allowed_command_ids=frozenset({"demo-check"}), raw_shell_allowed=False,
        allowed_network_domains=frozenset(), package_install_allowed=False,
        allowed_mcp_tools=frozenset(), allowed_external_systems=frozenset(), allowed_database_targets=frozenset(),
        sandbox_profile="trusted_local", budget_ceiling=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
    )


def _extract_evidence_ids(prompt_text: str) -> list[str]:
    marker = "EVIDENCE RECORDS (host-observed):\n"
    start = prompt_text.index(marker) + len(marker)
    end = prompt_text.index("\n\n", start)
    return [record["evidence_id"] for record in json.loads(prompt_text[start:end])]


def _demo_verifier_factory(resolve_prompt: Callable[[str], Awaitable[str]]):
    class _Provider(FakeAgentProvider):
        async def start_invocation(self, session, request):
            prompt_text = await resolve_prompt(request.prompt_payload_artifact_ref)
            evidence_ids = _extract_evidence_ids(prompt_text)
            verification_json = {
                "schema_version": "1.0", "verification_id": new_id(), "task_id": new_id(),
                "contract_digest": request.task_contract_digest, "result_snapshot_digest": VALID_DIGEST,
                "evidence_set_digest": VALID_DIGEST, "invocation_id": new_id(), "decision": "PASS",
                "criteria": [{"id": "demo-crit-1", "result": "PASS", "evidence_refs": evidence_ids}],
                "scope_violations": [], "security_findings": [], "quality_findings": [],
                "required_fixes": [], "prohibited_changes": [], "remaining_risks": [],
            }
            self.queue_invocation(
                AgentRole.VERIFIER, ScriptedInvocation(events=[], result=_make_result(verification_json))
            )
            return await super().start_invocation(session, request)

    return _Provider(capabilities=_make_capabilities(AgentRole.VERIFIER))


async def run_demo_pipeline(
    conn: sqlite3.Connection,
    *,
    data_root: Path,
    source_repo: Path,
    request_text: str,
    auto_approve: bool,
    confirm: Callable[[str], bool],
):
    git_client = GitClient(data_root / "empty-hooks")
    head_sha = git_client.rev_parse("HEAD", cwd=source_repo)

    now = _utc_now()
    run = Run(
        user_request_ref="cli-demo-request",
        user_request_digest=compute_digest(request_text.encode("utf-8")),
        repository_id="cli-demo-repo",
        budget_limits=BudgetRequest(timeout_seconds=600, max_turns=10, max_rework_iterations=1),
        budget_used=BudgetUsage(),
        created_at=now,
        updated_at=now,
    )
    task = Task(run_id=run.run_id, objective=request_text)
    insert_run(conn, run)
    insert_task(conn, task)

    contract = _make_demo_contract(run.run_id, task.task_id, head_sha, request_text)
    planner_provider = FakeAgentProvider(capabilities=_make_capabilities(AgentRole.PLANNER))
    planner_provider.queue_invocation(
        AgentRole.PLANNER, ScriptedInvocation(events=[], result=_make_result(contract.model_dump(mode="json")))
    )
    worker_provider = FakeAgentProvider(capabilities=_make_capabilities(AgentRole.WORKER))
    worker_provider.queue_invocation(
        AgentRole.WORKER,
        ScriptedInvocation(
            events=[], result=_make_result({"status": "COMPLETED", "implementation_summary": request_text})
        ),
    )
    providers = {AgentRole.PLANNER: planner_provider, AgentRole.WORKER: worker_provider}

    async def decide(decision) -> bool:
        if auto_approve:
            return True
        return confirm(f"Approve? ({decision})")

    deps = PipelineDeps(
        conn=conn, data_root=data_root, git_client=git_client, source_repo_path=source_repo,
        provider_for_role=lambda role: providers[role], verifier_provider_factory=_demo_verifier_factory,
        policy_ceiling=_make_demo_policy_ceiling(), command_catalog=_make_demo_command_catalog(),
        sandbox=TrustedLocalSandbox(), check_command_ids=["demo-check"], test_path_patterns=["tests/**"],
        available_env={}, role_profile_digest=VALID_DIGEST,
        decide_policy_approval=decide, decide_final_approval=decide, prompt_registry=PromptRegistry(),
    )
    return await run_task_pipeline(deps, run=run, task=task, user_request_text=request_text)
