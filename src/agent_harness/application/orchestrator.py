"""Dual-Agent Pipeline (Phase 9) + Rework Lifecycle (Phase 10): one Task,
end to end, including bounded rework loops.

``run_task_pipeline`` drives a Run through Phase 1.2's state machine,
persisted via Phase 2.1's atomic ``apply_transition``, calling into every
phase built so far in order:

    plan (7) -> accept contract (1.1) -> policy (4) -> approval (4)
    -> workspace (3.1) -> [worker (6) -> freeze/host-validate (3.3, 3.2)
    -> verify (8)] -> final approval

The bracketed stage repeats: a Verifier REWORK decision builds a
``ReworkContract`` (Phase 10, ``application/rework.py``) and loops back
through CONTRACT_VALIDATING rather than stopping, reusing the same
worktree so each rework attempt builds on the previous one's changes
rather than starting over.

Providers are looked up by role from a Phase 5 ``ProviderRegistry``-shaped
callable — this module never imports ``providers.claude``/``providers.codex``
directly, so the exact same pipeline runs against real adapters or
against ``FakeAgentProvider`` (Phase 1.1) unchanged. That's what makes a
"Fake E2E" test meaningful: it exercises this orchestrator's actual
control flow, not a parallel test-only code path.

Explicitly out of scope (per roadmap rows 9/10 and the MVP note in
section 11): DAG/multi-task Runs (one ``Task`` per ``Run``), MCP,
multi-repo, and automatic rebase onto a moved base ref. A Run that needs
manual review, or whose final approval is rejected by a human (as opposed
to a Verifier REWORK verdict, which *does* loop automatically), stops in
``AWAITING_MANUAL_REVIEW``/``REWORK_CONTRACTING`` and returns — there is
no ``required_fixes`` to build a ReworkContract from a bare human
rejection, so continuing that case is left to a human/future phase.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from agent_harness.application.rework import (
    ReworkExhaustedError,
    build_rework_contract,
    check_rework_budget,
    detect_no_progress,
    failed_criteria_ids_of,
)
from agent_harness.application.transitions import PendingAction
from agent_harness.application.verification import (
    PromptRegistry,
    VerifiedVerification,
    VerificationServiceError,
    run_verification,
)
from agent_harness.domain.digests import compute_model_digest, new_id
from agent_harness.domain.enums import (
    ActorType,
    AgentRole,
    FailureCategory,
    FailureCode,
    LifecycleState,
    PendingActionKind,
    PolicyOutcome,
    ProtocolStatus,
    SubjectType,
    VerificationDecision,
    WorkspaceAccessLevel,
)
from agent_harness.application.usage import BudgetExceededError, accumulate_usage, check_budget
from agent_harness.domain.models import (
    BudgetRequest,
    BudgetUsage,
    FailureRecord,
    IntegrityRef,
    PolicyDecision,
    RequestedCapabilities,
    ReworkContract,
    Run,
    Task,
    TaskContract,
    WorkerResult,
)
from agent_harness.execution.command_broker import CommandCatalog
from agent_harness.execution.evidence import FrozenValidationResult, freeze_and_validate, freeze_manifest
from agent_harness.execution.git_client import GitClient
from agent_harness.execution.sandbox import SandboxBackend
from agent_harness.execution.workspace import create_worktree
from agent_harness.persistence.sqlite import (
    apply_transition,
    insert_policy_decision,
    insert_rework_contract,
    insert_task_contract,
    insert_verification_result,
)
from agent_harness.policy.approvals import create_approval, resolve_decision_with_approval
from agent_harness.policy.evaluator import evaluate_policy
from agent_harness.policy.models import PolicyCeiling
from agent_harness.providers.protocol import (
    AgentProvider,
    AgentRunRequest,
    CancelRequest,
    ProviderCapabilityError,
    StartSessionRequest,
)

__all__ = ["PipelineDeps", "PipelineError", "run_task_pipeline"]


class PipelineError(RuntimeError):
    """Raised for pipeline-internal errors that aren't a modeled FailureRecord path."""


class PipelineTimeoutError(PipelineError):
    """Raised when a provider invocation exceeds its budget's timeout_seconds.

    Codex review B-07: adapters' own ``await_result()`` waits
    unconditionally — the Harness enforces the actual wall-clock
    deadline itself, at the one place every role's invocation already
    passes through (``_invoke_role``/``run_verification``), rather than
    depending on every current and future adapter to implement it.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _default_deny(_: object) -> bool:
    """Fail-closed default for both approval callbacks.

    Codex review B-03: a caller-forgot-to-wire-a-callback bug must never
    silently become an implicit approval — that violates least-privilege
    and explicit-user-approval everywhere else in this codebase
    (``evaluate_policy``, ``probe_capabilities``, sandbox capability
    negotiation all fail closed the same way). A caller that actually
    wants ``run_task_pipeline`` to reach ``READY_FOR_MERGE`` must supply a
    real ``decide_policy_approval``/``decide_final_approval`` — there is
    no default that grants anything.
    """

    return False


@dataclass
class PipelineDeps:
    conn: sqlite3.Connection
    data_root: Path
    git_client: GitClient
    source_repo_path: Path
    provider_for_role: Callable[[AgentRole], AgentProvider]
    verifier_provider_factory: Callable[[Callable[[str], Awaitable[str]]], AgentProvider]
    policy_ceiling: PolicyCeiling
    command_catalog: CommandCatalog
    sandbox: SandboxBackend
    check_command_ids: list[str]
    test_path_patterns: list[str]
    available_env: dict[str, str]
    role_profile_digest: str
    decide_policy_approval: Callable[[object], Awaitable[bool]] = _default_deny
    decide_final_approval: Callable[[VerifiedVerification], Awaitable[bool]] = _default_deny
    prompt_registry: PromptRegistry | None = None


def _read_only_grants(deps: PipelineDeps):
    from agent_harness.domain.models import PolicyGrants

    return PolicyGrants(sandbox_profile="read_only", budgets=deps.policy_ceiling.budget_ceiling)


_ACCESS_RANK = {
    WorkspaceAccessLevel.NONE: 0,
    WorkspaceAccessLevel.READ: 1,
    WorkspaceAccessLevel.READ_WRITE: 2,
}


def _merge_requested_capabilities(
    base: RequestedCapabilities, additional: RequestedCapabilities
) -> RequestedCapabilities:
    """Union two capability requests — used when a rework attempt asks for
    more than the original TaskContract requested.

    Per the review: "추가 capability가 있으면 기존 승인 재사용 금지" — the
    caller must re-run ``evaluate_policy``/approval on the *merged* result
    rather than just appending the extra grant, so nothing here decides
    policy; it only widens what gets asked for.
    """

    return RequestedCapabilities(
        workspace_access=max(
            base.workspace_access, additional.workspace_access, key=lambda a: _ACCESS_RANK[a]
        ),
        command_ids=sorted(set(base.command_ids) | set(additional.command_ids)),
        raw_shell=base.raw_shell or additional.raw_shell,
        network_domains=sorted(set(base.network_domains) | set(additional.network_domains)),
        package_install=base.package_install or additional.package_install,
        external_systems=sorted(set(base.external_systems) | set(additional.external_systems)),
        mcp_tools=sorted(set(base.mcp_tools) | set(additional.mcp_tools)),
        database_targets=sorted(set(base.database_targets) | set(additional.database_targets)),
    )


_WORKSPACE_ACCESS_CLAUDE_TOOL_IDS: dict[WorkspaceAccessLevel, tuple[str, ...]] = {
    WorkspaceAccessLevel.NONE: (),
    WorkspaceAccessLevel.READ: ("Read", "Glob", "Grep"),
    WorkspaceAccessLevel.READ_WRITE: ("Read", "Write", "Edit", "Glob", "Grep"),
}


def _claude_tool_ids_for(grants) -> list[str]:
    """The Claude SDK tool *names* a Worker invocation may use.

    Codex review B-01: this used to just be ``list(grants.command_ids)``
    — Command Broker command IDs (e.g. ``"pytest"``, ``"check"``), a
    completely different ID namespace meant for the Host Test Runner
    (``deps.check_command_ids``), never for Claude's ``allowed_tools``. A
    host command_id is never a valid Claude SDK tool name, so that wiring
    meant no real Claude tool (Read/Write/Edit/...) was ever actually
    usable in practice. ``Bash`` is deliberately never included:
    ``PolicyGrants`` carries no field reflecting whether ``raw_shell`` was
    actually granted (``evaluate_policy`` treats it as an
    approval-gate/reason-code, not part of the output grants) — fail
    closed on Bash until that gap is closed with its own field.
    """

    return list(_WORKSPACE_ACCESS_CLAUDE_TOOL_IDS.get(grants.workspace_access, ()))


def _contract_envelope_summary(contract: TaskContract) -> str:
    """The parts of an accepted TaskContract the Worker must actually see
    and respect — Codex review B-01: the prompt used to carry only the
    bare objective, leaving allowed/forbidden paths, constraints, and
    acceptance criteria entirely unstated. This is prompt-level guidance
    only, not itself an enforcement boundary — ``can_use_tool``'s path
    guard (``providers.claude._check_tool_path``) is what actually
    enforces scope; this just gives the model a fair chance to stay
    inside it in the first place.
    """

    allowed = ", ".join(contract.scope.allowed_path_rules) or "(none declared)"
    forbidden = ", ".join(contract.scope.forbidden_path_rules) or "(none declared)"
    criteria = "\n".join(
        f"- [{c.id}] {c.description}" + (" (mandatory)" if c.mandatory else "")
        for c in contract.acceptance_criteria
    )
    constraints = "\n".join(f"- {c}" for c in contract.constraints.typed_constraints) or "(none declared)"
    return (
        f"ALLOWED PATHS: {allowed}\n"
        f"FORBIDDEN PATHS: {forbidden}\n"
        f"ALLOW NEW FILES: {contract.scope.allow_new_files}\n\n"
        f"CONSTRAINTS:\n{constraints}\n\n"
        f"ACCEPTANCE CRITERIA (what will actually be verified):\n{criteria}"
    )


def _build_worker_prompt(contract: TaskContract, rework_contract: ReworkContract | None) -> str:
    envelope = _contract_envelope_summary(contract)
    if rework_contract is None:
        return (
            "Implement the following task in the working directory. Stay strictly "
            "within ALLOWED PATHS and never touch FORBIDDEN PATHS below — changes "
            "outside them will be rejected regardless of what you report. Respond "
            "with a single JSON object matching the WorkerResult schema.\n\n"
            f"OBJECTIVE:\n{contract.objective}\n\n{envelope}"
        )
    fixes = "\n".join(f"- {fix.description}" for fix in rework_contract.required_fixes)
    prohibited = "\n".join(f"- {item}" for item in rework_contract.prohibited_changes) or "(none)"
    return (
        "This is a REWORK attempt: the previous implementation did not pass verification. "
        "Fix only what is listed below; do not touch anything under PROHIBITED CHANGES. "
        "Stay strictly within ALLOWED PATHS and never touch FORBIDDEN PATHS — changes "
        "outside them will be rejected regardless of what you report. Respond with a "
        "single JSON object matching the WorkerResult schema.\n\n"
        f"OBJECTIVE (unchanged):\n{contract.objective}\n\n"
        f"REQUIRED FIXES (attempt {rework_contract.attempt_number}):\n{fixes}\n\n"
        f"PROHIBITED CHANGES:\n{prohibited}\n\n{envelope}"
    )


def _compute_deadline(budget: BudgetRequest, *, now: datetime) -> datetime:
    return now + timedelta(seconds=budget.timeout_seconds)


async def _invoke_role(
    provider: AgentProvider,
    role: AgentRole,
    *,
    prompt: str,
    prompt_registry: PromptRegistry,
    task_contract_ref: str,
    task_contract_digest: str,
    output_schema_id: str,
    effective_policy_grants,
    workspace_handle: str,
    role_profile_ref: str,
    role_profile_digest: str,
    budget: BudgetRequest,
    correlation_id: str,
    allowed_tool_ids: list[str] | None = None,
    rework_contract_ref: str | None = None,
    rework_contract_digest: str | None = None,
):
    """Invoke one provider turn under a real, enforced wall-clock deadline.

    Codex review B-07: ``deadline`` used to be set to ``_utc_now()`` (a
    deadline already in the past by the time anything read it) and
    nothing enforced it — a hung Claude/Codex call could block a Run
    forever. ``budget.timeout_seconds`` now drives both the deadline
    metadata handed to the provider *and* an actual
    ``asyncio.wait_for()`` around ``await_result()`` at the Harness
    level, since no adapter currently enforces its own deadline. On
    timeout, this attempts a best-effort ``provider.cancel()`` (skipped,
    not fatal, if the provider doesn't support ``native_cancel``) before
    always closing the session and raising ``PipelineTimeoutError``.
    """

    deadline = _compute_deadline(budget, now=_utc_now())
    prompt_ref = prompt_registry.put(prompt)
    session = await provider.start_session(
        StartSessionRequest(
            role=role,
            role_profile_ref=role_profile_ref,
            role_profile_digest=role_profile_digest,
            contract_digest=task_contract_digest,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=task_contract_digest,
            deadline=deadline,
        )
    )
    try:
        request = AgentRunRequest(
            role=role,
            task_contract_ref=task_contract_ref,
            task_contract_digest=task_contract_digest,
            rework_contract_ref=rework_contract_ref,
            rework_contract_digest=rework_contract_digest,
            context_snapshot_ref="context-ref",
            context_snapshot_digest=task_contract_digest,
            role_profile_ref=role_profile_ref,
            role_profile_digest=role_profile_digest,
            output_schema_id=output_schema_id,
            output_schema_version="1.0",
            output_schema_digest=task_contract_digest,
            effective_policy_grants=effective_policy_grants,
            workspace_handle=workspace_handle,
            deadline=deadline,
            max_turns=budget.max_turns,
            allowed_tool_ids=allowed_tool_ids or [],
            prompt_payload_artifact_ref=prompt_ref,
            correlation_id=correlation_id,
            idempotency_key=f"{role.value.lower()}-{new_id()}",
        )
        invocation = await provider.start_invocation(session, request)
        try:
            return await asyncio.wait_for(provider.await_result(invocation), timeout=budget.timeout_seconds)
        except asyncio.TimeoutError:
            try:
                await provider.cancel(
                    invocation, CancelRequest(invocation_id=invocation.opaque_ref, reason="deadline exceeded", force=True)
                )
            except ProviderCapabilityError:
                pass  # best-effort — not every provider supports native_cancel
            raise PipelineTimeoutError(
                f"{role.value} invocation exceeded its {budget.timeout_seconds}s deadline"
            ) from None
    finally:
        await provider.close_session(session)


def _fail(
    deps: PipelineDeps,
    run: Run,
    *,
    stage: str,
    code: FailureCode,
    detail: str,
    category: FailureCategory = FailureCategory.INFRASTRUCTURE,
    actor: ActorType = ActorType.HARNESS,
    correlation_id: str,
) -> Run:
    failure = FailureRecord(
        run_id=run.run_id,
        stage=stage,
        code=code,
        category=category,
        retriable=False,
        sanitized_detail=detail,
        occurred_at=_utc_now(),
    )
    outcome = apply_transition(
        deps.conn,
        run.run_id,
        LifecycleState.FAILED,
        now=_utc_now(),
        expected_state_version=run.state_version,
        actor_type=actor,
        actor_id="orchestrator",
        correlation_id=correlation_id,
        failure=failure,
    )
    return outcome.run


def _advance(
    deps: PipelineDeps,
    run: Run,
    target: LifecycleState,
    *,
    correlation_id: str,
    pending_action: PendingAction | None = None,
) -> Run:
    outcome = apply_transition(
        deps.conn,
        run.run_id,
        target,
        now=_utc_now(),
        expected_state_version=run.state_version,
        actor_type=ActorType.HARNESS,
        actor_id="orchestrator",
        correlation_id=correlation_id,
        pending_action=pending_action,
    )
    return outcome.run


async def _gate_policy(
    deps: PipelineDeps,
    run: Run,
    correlation_id: str,
    *,
    subject_type: SubjectType,
    subject_id: str,
    subject_digest: str,
    requested_capabilities,
    requested_scope,
) -> tuple[Run, PolicyDecision | None]:
    """Evaluate policy and, if required, run the human-approval gate.

    On success returns ``(run, decision)`` with ``decision`` set. On a
    hard deny or a rejected approval, returns ``(failed_run, None)`` with
    ``failed_run`` already transitioned to ``FAILED`` — the caller must
    return it immediately without any further transition.
    """

    decision = evaluate_policy(
        subject_type,
        subject_id,
        subject_digest,
        requested_capabilities,
        requested_scope,
        ceiling=deps.policy_ceiling,
        evaluated_at=_utc_now(),
    )
    # Codex review B-04 (partial): persist every decision, denials
    # included — an audit trail that only kept ALLOW outcomes would miss
    # exactly the events most worth auditing.
    insert_policy_decision(deps.conn, decision, run_id=run.run_id)
    if decision.outcome is PolicyOutcome.DENY:
        failed = _fail(
            deps, run, stage="POLICY", code=FailureCode.POLICY_HARD_DENY,
            detail="; ".join(decision.reason_codes), category=FailureCategory.DOMAIN,
            correlation_id=correlation_id,
        )
        return failed, None

    if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
        run = _advance(
            deps, run, LifecycleState.AWAITING_APPROVAL, correlation_id=correlation_id,
            pending_action=PendingAction(
                kind=PendingActionKind.APPROVAL,
                description=f"capabilities require approval: {decision.approval_requirements}",
                requested_at=_utc_now(),
            ),
        )
        approved = await deps.decide_policy_approval(decision)
        approval = create_approval(
            decision, actor="pipeline", decision_value="APPROVED" if approved else "REJECTED",
            rationale="automated pipeline decision", requested_at=_utc_now(), decided_at=_utc_now(),
        )
        resolved_outcome = resolve_decision_with_approval(decision, approval, now=_utc_now())
        if resolved_outcome is not PolicyOutcome.ALLOW:
            failed = _fail(
                deps, run, stage="POLICY", code=FailureCode.POLICY_DENIED,
                detail="capability approval was rejected", category=FailureCategory.DOMAIN,
                correlation_id=correlation_id,
            )
            return failed, None

    return run, decision


async def run_task_pipeline(
    deps: PipelineDeps,
    *,
    run: Run,
    task: Task,
    user_request_text: str,
) -> Run:
    """Drive ``run`` from ``CREATED`` as far as this phase's scope allows.

    Returns the Run in whatever terminal-for-this-phase state it reached:
    ``READY_FOR_MERGE`` (success), ``FAILED`` (hard stop, including
    ``REWORK_EXHAUSTED``), or one of ``REWORK_CONTRACTING`` (final
    approval rejected by a human) / ``AWAITING_MANUAL_REVIEW`` (needs a
    human).
    """

    correlation_id = f"run-{run.run_id}"
    registry = deps.prompt_registry or PromptRegistry()
    # Codex review B-07: accumulated in-memory across this pipeline run
    # and checked before every invocation — real, live enforcement within
    # a single Run (the actual runaway-cost/turns risk), even though it
    # is not yet persisted back onto the Run row itself (apply_transition
    # has no path for updating budget_used; see B-04's broader
    # persistence gap).
    budget_used = run.budget_used

    # Codex review H-01: repository identity and base revision must be
    # fixed by the Harness itself, resolved *before* the Planner is ever
    # invoked — a Planner is only ever allowed to propose scope/
    # acceptance_criteria/requested_capabilities within this envelope,
    # never which repository or commit gets checked out.
    try:
        authoritative_base_sha = deps.git_client.rev_parse("HEAD", cwd=deps.source_repo_path)
        authoritative_fingerprint = deps.git_client.compute_repository_fingerprint(cwd=deps.source_repo_path)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            deps, run, stage="PLANNING", code=FailureCode.EXECUTION_FAILED,
            detail=f"could not resolve authoritative repository state: {exc}", correlation_id=correlation_id,
        )

    # ---- PLANNING ---------------------------------------------------
    run = _advance(deps, run, LifecycleState.PLANNING, correlation_id=correlation_id)

    try:
        check_budget(budget_used, deps.policy_ceiling.budget_ceiling)
    except BudgetExceededError as exc:
        return _fail(
            deps, run, stage="PLANNING", code=FailureCode.EXECUTION_FAILED,
            detail=f"budget exceeded before Planner invocation: {exc}",
            category=FailureCategory.DOMAIN, correlation_id=correlation_id,
        )

    planner_prompt = (
        "Propose a TaskContract for the following request. Respond with a "
        f"single JSON object matching the TaskContract schema.\n\nREQUEST:\n{user_request_text}"
    )
    try:
        planner_result = await _invoke_role(
            deps.provider_for_role(AgentRole.PLANNER),
            AgentRole.PLANNER,
            prompt=planner_prompt,
            prompt_registry=registry,
            task_contract_ref=task.task_id,
            task_contract_digest="sha256:" + "0" * 64,
            output_schema_id="task_contract",
            effective_policy_grants=_read_only_grants(deps),
            workspace_handle=".",
            role_profile_ref="planner-profile",
            role_profile_digest=deps.role_profile_digest,
            budget=deps.policy_ceiling.budget_ceiling,
            correlation_id=correlation_id,
        )
    except PipelineTimeoutError as exc:
        return _fail(
            deps, run, stage="PLANNING", code=FailureCode.TIMED_OUT,
            detail=str(exc), correlation_id=correlation_id,
        )
    if planner_result.usage is not None:
        budget_used = accumulate_usage(budget_used, planner_result.usage)
    if planner_result.protocol_status is not ProtocolStatus.SUCCEEDED or planner_result.structured_output is None:
        return _fail(
            deps, run, stage="PLANNING", code=FailureCode.EXECUTION_FAILED,
            detail=f"planner did not produce a contract: {planner_result.protocol_status}",
            correlation_id=correlation_id,
        )

    # ---- CONTRACT_VALIDATING -----------------------------------------
    run = _advance(deps, run, LifecycleState.CONTRACT_VALIDATING, correlation_id=correlation_id)
    try:
        draft_contract = TaskContract.model_validate(
            {**planner_result.structured_output, "task_id": task.task_id, "run_id": run.run_id}
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            deps, run, stage="CONTRACT_VALIDATING", code=FailureCode.SCHEMA_INVALID,
            detail=f"planner output did not validate as a TaskContract: {exc}",
            category=FailureCategory.DOMAIN, correlation_id=correlation_id,
        )
    # Harness overrides whatever repository identity/base revision the
    # Planner proposed with what it resolved itself, before the Planner
    # was even invoked (Codex review H-01) — the Planner's own
    # `repository` field in its structured output is never trusted for
    # these values, only for objective/scope/acceptance_criteria/
    # requested_capabilities.
    draft_contract = draft_contract.model_copy(
        update={
            "repository": draft_contract.repository.model_copy(
                update={
                    "repository_id": run.repository_id,
                    "base_commit_sha": authoritative_base_sha,
                    "target_ref": "HEAD",
                    "expected_repository_fingerprint": authoritative_fingerprint,
                }
            )
        }
    )
    # Harness computes the accepted digest itself — never trusts whatever
    # (if anything) the Planner put in integrity.canonical_digest.
    contract_digest = compute_model_digest(draft_contract, exclude_fields={"integrity"})
    contract = draft_contract.model_copy(update={"integrity": IntegrityRef(canonical_digest=contract_digest)})
    # Codex review B-04 (partial): persist the accepted contract so it
    # can be audited/recovered after this process exits, not just held
    # in this function's local variable for the rest of the run.
    insert_task_contract(deps.conn, contract, run_id=run.run_id, now=_utc_now())

    # ---- Policy (initial) ---------------------------------------------
    run, decision = await _gate_policy(
        deps, run, correlation_id,
        subject_type=SubjectType.TASK_CONTRACT,
        subject_id=contract.contract_id,
        subject_digest=contract_digest,
        requested_capabilities=contract.requested_capabilities,
        requested_scope=contract.scope,
    )
    if decision is None:
        return run
    run = _advance(deps, run, LifecycleState.PREPARING_WORKSPACE, correlation_id=correlation_id)

    # ---- PREPARING_WORKSPACE (once — reworks reuse this worktree) ------
    try:
        # Re-verify identity right before checkout — catches a TOCTOU
        # where source_repo_path was swapped out from under the Harness
        # between the resolution above and now (Codex review H-01's
        # concern that verify_repository_fingerprint was never actually
        # called on the real worktree-creation path).
        deps.git_client.verify_repository_fingerprint(
            cwd=deps.source_repo_path, expected_fingerprint=contract.repository.expected_repository_fingerprint,
        )
        lease = create_worktree(
            deps.git_client,
            data_root=deps.data_root,
            repository_id=contract.repository.repository_id,
            source_repo_path=deps.source_repo_path,
            base_commit_sha=contract.repository.base_commit_sha,
            run_id=run.run_id,
            now=_utc_now(),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            deps, run, stage="PREPARING_WORKSPACE", code=FailureCode.EXECUTION_FAILED,
            detail=f"worktree creation failed: {exc}", correlation_id=correlation_id,
        )
    worktree_path = Path(lease.worktree_path_handle)
    baseline_manifest, baseline_artifact = freeze_manifest(worktree_path, data_root=deps.data_root, now=_utc_now())

    active_capabilities = contract.requested_capabilities
    active_grants = decision.grants
    rework_contract: ReworkContract | None = None
    attempt_number = 1
    previous_failed_ids: frozenset[str] | None = None

    while True:
        # ---- EXECUTING ---------------------------------------------
        run = _advance(deps, run, LifecycleState.EXECUTING, correlation_id=correlation_id)
        try:
            check_budget(budget_used, active_grants.budgets)
        except BudgetExceededError as exc:
            return _fail(
                deps, run, stage="EXECUTING", code=FailureCode.EXECUTION_FAILED,
                detail=f"budget exceeded before Worker invocation: {exc}",
                category=FailureCategory.DOMAIN, correlation_id=correlation_id,
            )

        worker_prompt = _build_worker_prompt(contract, rework_contract)
        try:
            worker_raw_result = await _invoke_role(
                deps.provider_for_role(AgentRole.WORKER),
                AgentRole.WORKER,
                prompt=worker_prompt,
                prompt_registry=registry,
                task_contract_ref=contract.contract_id,
                task_contract_digest=contract_digest,
                output_schema_id="worker_result",
                effective_policy_grants=active_grants,
                workspace_handle=lease.worktree_path_handle,
                role_profile_ref="worker-profile",
                role_profile_digest=deps.role_profile_digest,
                budget=active_grants.budgets,
                correlation_id=correlation_id,
                # Codex review B-01: Claude SDK tool *names*, not Command
                # Broker command IDs — see _claude_tool_ids_for's docstring.
                allowed_tool_ids=_claude_tool_ids_for(active_grants),
                rework_contract_ref=rework_contract.rework_id if rework_contract else None,
                rework_contract_digest=rework_contract.integrity_digest if rework_contract else None,
            )
        except PipelineTimeoutError as exc:
            return _fail(
                deps, run, stage="EXECUTING", code=FailureCode.TIMED_OUT,
                detail=str(exc), correlation_id=correlation_id,
            )
        if worker_raw_result.usage is not None:
            budget_used = accumulate_usage(budget_used, worker_raw_result.usage)
        worker_result: WorkerResult | None = None
        if worker_raw_result.protocol_status is ProtocolStatus.SUCCEEDED and worker_raw_result.structured_output:
            try:
                worker_result = WorkerResult.model_validate(
                    {
                        **worker_raw_result.structured_output,
                        "task_id": task.task_id,
                        "contract_id": contract.contract_id,
                        "contract_digest": contract_digest,
                        "invocation_id": worker_raw_result.invocation_id,
                        "provider_session_ref": worker_raw_result.provider_session_ref or "unknown",
                    }
                )
            except Exception:  # noqa: BLE001
                worker_result = None  # worker's self-report is untrusted anyway; proceed to host validation

        # ---- FREEZING_RESULT / HOST_VALIDATING ----------------------
        run = _advance(deps, run, LifecycleState.FREEZING_RESULT, correlation_id=correlation_id)
        run = _advance(deps, run, LifecycleState.HOST_VALIDATING, correlation_id=correlation_id)
        frozen_result: FrozenValidationResult = freeze_and_validate(
            run.run_id,
            task.task_id,
            worktree_path,
            baseline_manifest,
            baseline_artifact,
            catalog=deps.command_catalog,
            check_command_ids=deps.check_command_ids,
            test_path_patterns=deps.test_path_patterns,
            sandbox=deps.sandbox,
            available_env=deps.available_env,
            now=_utc_now(),
            data_root=deps.data_root,
            # Codex review M-01: the *effective granted* scope, not the
            # Planner's raw requested scope — this is what actually backs
            # find_scope_violations() inside freeze_and_validate.
            scope=active_grants.path_rules,
        )

        # ---- VERIFYING ------------------------------------------------
        run = _advance(deps, run, LifecycleState.VERIFYING, correlation_id=correlation_id)
        try:
            check_budget(budget_used, active_grants.budgets)
        except BudgetExceededError as exc:
            return _fail(
                deps, run, stage="VERIFYING", code=FailureCode.EXECUTION_FAILED,
                detail=f"budget exceeded before Verifier invocation: {exc}",
                category=FailureCategory.DOMAIN, correlation_id=correlation_id,
            )
        try:
            verified = await run_verification(
                deps.verifier_provider_factory,
                contract,
                frozen_result,
                role_profile_ref="verifier-profile",
                role_profile_digest=deps.role_profile_digest,
                context_snapshot_ref="context-ref",
                context_snapshot_digest=contract_digest,
                deadline=_compute_deadline(active_grants.budgets, now=_utc_now()),
                correlation_id=correlation_id,
                untrusted_worker_claims=None,  # bias exclusion by default, per H-06
                budget=active_grants.budgets,
            )
        except VerificationServiceError as exc:
            # Covers both "no usable result" and the timeout case raised
            # from run_verification's own asyncio.wait_for (Codex review B-07).
            return _fail(
                deps, run, stage="VERIFYING", code=FailureCode.EXECUTION_FAILED,
                detail=str(exc), correlation_id=correlation_id,
            )
        if verified.usage is not None:
            budget_used = accumulate_usage(budget_used, verified.usage)
        # Codex review B-04 (partial): persist the model's own claimed
        # VerificationResult — not accepted_decision (this module's
        # re-check of it), which is derived and always reconstructable
        # from this record plus the frozen evidence.
        insert_verification_result(deps.conn, verified.verification_result, run_id=run.run_id, now=_utc_now())

        if verified.accepted_decision is VerificationDecision.REJECT:
            return _fail(
                deps, run, stage="VERIFYING", code=FailureCode.TEST_FAILED,
                detail="; ".join(verified.verification_result.remaining_risks) or "verification rejected",
                category=FailureCategory.DOMAIN, correlation_id=correlation_id,
            )

        if verified.accepted_decision is VerificationDecision.MANUAL_REVIEW:
            return _advance(
                deps, run, LifecycleState.AWAITING_MANUAL_REVIEW, correlation_id=correlation_id,
                pending_action=PendingAction(
                    kind=PendingActionKind.MANUAL_REVIEW,
                    description="verifier decision requires manual review",
                    requested_at=_utc_now(),
                ),
            )

        if verified.accepted_decision is VerificationDecision.PASS:
            run = _advance(
                deps, run, LifecycleState.AWAITING_FINAL_APPROVAL, correlation_id=correlation_id,
                pending_action=PendingAction(
                    kind=PendingActionKind.FINAL_APPROVAL,
                    description="verification passed; awaiting final user approval",
                    requested_at=_utc_now(),
                ),
            )
            approved = await deps.decide_final_approval(verified)
            if approved:
                return _advance(deps, run, LifecycleState.READY_FOR_MERGE, correlation_id=correlation_id)
            # A human rejected the final result outright — there is no
            # Verifier-authored required_fixes list to build a
            # ReworkContract from, so (unlike a Verifier REWORK verdict)
            # this does not loop automatically. Stop for a human/Phase 11+.
            return _advance(deps, run, LifecycleState.REWORK_CONTRACTING, correlation_id=correlation_id)

        # ---- accepted_decision is REWORK: bounded, evidence-driven loop ----
        current_failed_ids = failed_criteria_ids_of(verified.verification_result)
        if detect_no_progress(previous_failed_ids, current_failed_ids):
            return _fail(
                deps, run, stage="VERIFYING", code=FailureCode.REWORK_EXHAUSTED,
                detail=f"no progress: attempt {attempt_number} failed the same criteria as the previous attempt "
                       f"({sorted(current_failed_ids)!r})",
                category=FailureCategory.DOMAIN, correlation_id=correlation_id,
            )
        try:
            check_rework_budget(attempt_number, contract.budget_request)
        except ReworkExhaustedError as exc:
            return _fail(
                deps, run, stage="VERIFYING", code=FailureCode.REWORK_EXHAUSTED,
                detail=str(exc), category=FailureCategory.DOMAIN, correlation_id=correlation_id,
            )

        additional_capabilities = (
            worker_result.requested_additional_capabilities if worker_result is not None else None
        )
        rework_contract = build_rework_contract(
            contract, verified, attempt_number=attempt_number,
            requested_additional_capabilities=additional_capabilities,
        )
        # Codex review B-04 (partial).
        insert_rework_contract(deps.conn, rework_contract, run_id=run.run_id, now=_utc_now())

        run = _advance(deps, run, LifecycleState.REWORK_CONTRACTING, correlation_id=correlation_id)
        run = _advance(deps, run, LifecycleState.CONTRACT_VALIDATING, correlation_id=correlation_id)

        if rework_contract.requested_additional_capabilities is not None:
            merged_capabilities = _merge_requested_capabilities(
                active_capabilities, rework_contract.requested_additional_capabilities
            )
            run, rework_decision = await _gate_policy(
                deps, run, correlation_id,
                subject_type=SubjectType.REWORK_CONTRACT,
                subject_id=rework_contract.rework_id,
                subject_digest=rework_contract.integrity_digest,
                requested_capabilities=merged_capabilities,
                requested_scope=rework_contract.effective_scope,
            )
            if rework_decision is None:
                return run
            active_capabilities = merged_capabilities
            active_grants = rework_decision.grants

        run = _advance(deps, run, LifecycleState.PREPARING_WORKSPACE, correlation_id=correlation_id)

        # Rolling baseline: the next freeze measures what changed *in this
        # rework attempt*, not cumulatively from the original checkout —
        # the Worker is fixing the previous attempt's output in place, not
        # starting over from a fresh worktree. Uses post_test_manifest
        # (the truly final worktree state, after host checks ran) rather
        # than the pre-test result_manifest, so a check-induced side
        # effect from this attempt doesn't get silently re-classified as
        # "the next attempt's own change" (Codex review B-02).
        baseline_manifest, baseline_artifact = frozen_result.post_test_manifest, frozen_result.post_test_artifact
        previous_failed_ids = current_failed_ids
        attempt_number += 1
