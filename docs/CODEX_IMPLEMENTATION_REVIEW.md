# Codex Implementation Review

**검토일**: 2026-08-15  
**검토 대상**: `docs/IMPLEMENTATION_SUMMARY.md` 및 현재 작업 트리  
**판정**: `REWORK`

## Executive Verdict

`로드맵 13개 Phase 전부 완료` 주장은 승인할 수 없다.

현재 결과물은 테스트가 잘 갖춰진 기반 구현이지만, 실제 Codex–Claude 하네스로 실행·중단·재개·감사할 수 있는 상태는 아니다. 구현 요약도 완료를 선언하면서 핵심 실행 경로가 연결되지 않았다고 스스로 인정하므로 완료 선언과 기술적 사실이 모순된다.

현재 상태는 다음과 같이 정정하는 것이 적절하다.

> 기반 컴포넌트 구현 완료. 운영 통합, 권한 강제, 결정적 검증, 재시작 및 보안 경계 미완료.

## Independent Verification

검토 시점에 다음을 독립적으로 실행했다.

```text
.venv\Scripts\python.exe -m pytest -q
```

결과:

```text
632 passed, 4 skipped, 3 warnings in 24.56s
```

이는 `IMPLEMENTATION_SUMMARY.md`의 `634 passed, 2 skipped` 주장과 일치하지 않는다.

Skip 항목:

1. Claude 실제 API smoke test 1건
2. 실제 Dual-Agent E2E 1건
3. Windows symlink 검사 2건

추가 확인:

```text
Schema 생성/드리프트 검사: 5 passed
git diff --cached --check: 통과
python -m agent_harness.interfaces.cli --help: 통과
where harness: 실행 파일을 찾지 못함
```

모든 프로젝트 파일은 staged 상태이며 커밋은 없다.

## BLOCKER Findings

### B-01 — Claude Worker 권한이 실제 실행 경계에서 강제되지 않는다

문제:

- `ClaudeAgentAdapter._make_can_use_tool()`은 도구 이름만 검사한다.
- `tool_input`의 대상 경로, 명령 인자, 네트워크 목적지 등을 검사하지 않는다.
- `PolicyGrants.path_rules`, `network_rules`, `package_rules`가 Claude SDK 도구 실행에 적용되지 않는다.
- orchestrator는 Host Test Runner용 `command_ids`를 Claude SDK의 `allowed_tool_ids`로 그대로 전달한다. 두 ID namespace는 의미가 다르다.
- Worker prompt는 objective만 전달하고 허용 경로, 금지 경로, 제약, acceptance criteria, evidence 요구사항을 전달하지 않는다.
- `trusted_local` 실행에는 OS 수준 파일 시스템 또는 네트워크 격리가 없다.

영향:

- 일반적인 command ID를 사용하면 Claude가 `Read`, `Edit`, `Write`, `Bash` 같은 실제 구현 도구를 사용할 수 없다.
- 해당 도구를 허용하면 절대 경로나 상위 경로를 통해 worktree 밖 호스트 파일에 접근할 수 있다.
- TaskContract와 PolicyDecision이 선언적 객체에 머물고 실제 권한 경계가 되지 못한다.

근거:

- `src/agent_harness/providers/claude.py:459-488`
- `src/agent_harness/application/orchestrator.py:167-181`
- `src/agent_harness/application/orchestrator.py:469-485`
- `src/agent_harness/execution/sandbox.py:9-14`

수정 방향:

1. `AgentToolGrant`와 `HostCommandGrant`를 별도 타입과 ID namespace로 분리한다.
2. 모든 file tool 요청에서 canonicalized target path가 worktree의 실제 경로 아래인지 Harness가 검사한다.
3. forbidden path, symlink, junction, reparse point, case-folding까지 검사한다.
4. Bash 또는 임의 프로세스가 필요한 Worker는 실제 격리 backend 내부에서 실행한다.
5. network 및 package-install grant를 실제 runtime control과 연결한다.
6. Worker prompt에 accepted TaskContract 또는 검증된 최소 투영본을 포함한다.

첫 구현 전 해결 필요: **예**

### B-02 — 실패한 Host Test가 있어도 공식 PASS가 가능하다

문제:

- `scope_guard`는 standalone 함수와 테스트만 있고 orchestrator 또는 verification 경로에서 호출되지 않는다.
- manifest는 Host Test 실행 전에 한 번 동결된다.
- Host Test가 파일을 변경한 뒤 post-test manifest를 생성하지 않는다.
- deterministic PASS gate는 실제 command exit code, timeout, output cap, expected exit code를 검사하지 않는다.
- Verifier가 존재하는 Evidence ID만 인용하면, 해당 evidence가 실패한 command의 결과여도 PASS가 받아들여질 수 있다.
- 테스트 파일 변경 정보도 모델에게 전달될 뿐 Harness의 hard gate가 아니다.

영향:

- 악성 테스트나 빌드 스크립트가 실행 후 worktree를 변경해도 최종 manifest와 scope 판정에 반영되지 않는다.
- `exit_code != 0`, timeout 또는 output cap 발생에도 Verifier의 잘못된 PASS가 `READY_FOR_MERGE`로 이어질 수 있다.

근거:

- `src/agent_harness/execution/scope_guard.py:14-21`
- `src/agent_harness/execution/evidence.py:195-229`
- `src/agent_harness/application/verification.py:176-201`
- `src/agent_harness/domain/validation.py:66-119`

수정 방향:

1. Worker 실행 직후 `worker_result_manifest`를 동결한다.
2. Host Test 후 `post_test_manifest`를 다시 생성한다.
3. 두 manifest 차이를 `test_side_effects`로 기록한다.
4. `PolicyDecision.grants.path_rules`에 대해 `find_scope_violations()`를 호출한다.
5. 필수 command의 timeout, output cap, 비허용 exit code를 deterministic failure로 처리한다.
6. scope 위반, 금지된 테스트 변경, 증거 누락은 모델 판단과 무관하게 PASS를 금지한다.

첫 구현 전 해결 필요: **예**

### B-03 — 승인 기본값이 자동 승인이다

문제:

`PipelineDeps`에서 policy approval과 final approval callback의 기본값이 모두 항상 `True`를 반환한다.

```python
async def _default_approve(_: object) -> bool:
    return True
```

영향:

- 호출자가 callback 설정을 누락하면 승인 필요 작업과 최종 병합 가능 판정이 묵시적으로 승인된다.
- 최소 권한과 명시적 사용자 승인 원칙을 위반한다.

근거:

- `src/agent_harness/application/orchestrator.py:104-125`

수정 방향:

- 기본값을 자동 승인 함수가 아니라 `None` 또는 명시적인 pause/deny 구현으로 바꾼다.
- 승인이 필요하면 `AWAITING_*` 상태와 durable `ApprovalRequest`를 기록한 후 pipeline을 종료한다.
- 별도 승인 명령이 durable Approval을 생성한 뒤 scheduler가 다음 step을 재개해야 한다.

첫 구현 전 해결 필요: **예**

### B-04 — 감사에 필요한 객체가 실행 경로에서 영속화되지 않는다

문제:

다음 객체의 durable table 또는 실행 경로 저장이 없다.

- TaskContract
- PolicyDecision
- Approval
- AgentSession
- WorkspaceLease
- CommandRun
- VerificationResult
- ReworkContract

Artifact, ContextSnapshot, Evidence, AgentInvocation 저장 함수는 일부 존재하지만 orchestrator가 호출하지 않는다. `PromptRegistry`도 process-local 메모리 저장소다.

영향:

- 재시작 후 동일한 Contract와 PolicyDecision을 복원할 수 없다.
- 승인 binding과 재사용 여부를 감사할 수 없다.
- 최종 report가 실제 invocation, diff, command output, verification을 포함하지 못한다.
- blob만 남고 metadata가 없는 orphan artifact가 발생한다.
- Agent의 설명보다 실제 증거를 신뢰한다는 핵심 목표를 만족하지 못한다.

근거:

- `src/agent_harness/persistence/migrations.py:3-9`
- `src/agent_harness/application/orchestrator.py`
- `src/agent_harness/application/reporting.py:36-66`

수정 방향:

- 누락된 aggregate table과 repository를 추가한다.
- 각 side effect 전 `AgentInvocation STARTED`와 lease를 먼저 기록한다.
- provider event/result, Artifact, Evidence, Verification을 완료 transition과 원자적으로 연결한다.
- ContextSnapshot의 placeholder ref와 digest를 제거하고 실제 immutable snapshot을 사용한다.

첫 구현 전 해결 필요: **예**

### B-05 — CLI 승인 및 재개가 실제 pipeline을 재개하지 않는다

문제:

- `approve`와 `resume`은 Run 상태만 다음 상태로 전이한다.
- 중단된 pipeline step을 다시 dispatch하지 않는다.
- TaskContract와 WorkspaceLease가 영속화되지 않아 재개에 필요한 입력도 없다.
- `cancel`은 DB 상태를 `CANCELLED`로 변경하지만 실행 중 Provider invocation이나 subprocess를 취소하지 않는다.

영향:

- `approve` 후 Run이 `PREPARING_WORKSPACE`에 정지한다.
- `resume` 후 자동 복구가 시작되지 않는다.
- 취소된 Agent/API 호출이 계속 실행되어 비용과 side effect가 발생할 수 있다.

근거:

- `src/agent_harness/interfaces/cli.py:117-167`
- `src/agent_harness/interfaces/cli.py:197-230`
- `src/agent_harness/interfaces/cli.py:236-260`

수정 방향:

- CLI는 승인·취소 의도만 durable record로 남긴다.
- Run Manager 또는 step executor가 상태를 claim하고 해당 상태의 작업을 수행한다.
- invocation cancellation token과 provider `cancel()`을 연결한다.
- process identity를 저장하고 실제 process tree 종료까지 확인한다.

첫 구현 전 해결 필요: **예**

### B-06 — 실제 운영 CLI가 아니라 Fake Provider 데모다

문제:

- `harness run` 구현은 `_demo_pipeline.run_demo_pipeline()`만 호출한다.
- 실제 Codex/Claude provider registry, credentials, policy, command catalog config loader가 없다.
- `pyproject.toml`에 `[project.scripts]`가 없어 설치 후 `harness` 명령이 생성되지 않는다.

영향:

- 현재 CLI로는 실제 코드를 구현하는 Dual-Agent pipeline을 실행할 수 없다.
- `harness run --repo ...`가 성공해도 Worker는 Fake Provider이므로 코드 변경이 없다.

근거:

- `src/agent_harness/interfaces/cli.py:355-387`
- `src/agent_harness/interfaces/_demo_pipeline.py:1-9`
- `pyproject.toml`

수정 방향:

- 데모 명령을 `harness demo`로 분리한다.
- 실제 `run`에는 검증된 config loader와 Provider Registry를 연결한다.
- `[project.scripts] harness = "agent_harness.interfaces.cli:main"` 형태의 entry point를 추가한다.
- 외부 키 없이 Fake/Replay E2E를 유지하되 실제 provider smoke test는 별도 opt-in profile로 둔다.

첫 구현 전 해결 필요: **예**

### B-07 — timeout, usage, budget, cancellation이 연결되지 않는다

문제:

- orchestrator가 Provider request의 deadline에 `_utc_now()`를 넣는다.
- Claude와 Codex adapter의 `await_result()`는 deadline을 집행하지 않고 무기한 condition wait를 수행한다.
- `accumulate_usage()`와 `check_budget()`는 단위 함수로만 존재하고 orchestrator에서 호출되지 않는다.
- Provider request의 `max_turns`, `max_tokens`, `max_cost_usd`도 pipeline에서 설정하지 않는다.

영향:

- Provider가 응답하지 않으면 Run이 무기한 정지할 수 있다.
- turn/token/cost 제한이 실제 비용 통제로 작동하지 않는다.
- 자동 rework와 함께 비용 폭주 가능성이 있다.

근거:

- `src/agent_harness/application/orchestrator.py:388-401`
- `src/agent_harness/application/orchestrator.py:469-485`
- `src/agent_harness/providers/claude.py:577-585`
- `src/agent_harness/providers/codex.py:583-591`
- `src/agent_harness/application/usage.py`

수정 방향:

- accepted policy budget로 절대 deadline과 provider 제한을 계산한다.
- Harness 레벨에서 `asyncio.timeout()` 또는 동등한 timeout을 집행한다.
- 모든 invocation 완료 시 usage를 누적하고 다음 invocation 전 budget을 검사한다.
- timeout과 cancellation 시 provider cancel, session close, child cleanup을 순서대로 수행한다.

첫 구현 전 해결 필요: **예**

## HIGH Findings

### H-01 — Planner가 authoritative repository 정보를 바꿀 수 있다

Harness는 Planner 출력에서 `task_id`와 `run_id`만 덮어쓴다. 다음 값은 Planner가 제시한 값을 그대로 수용한다.

- repository ID
- base commit SHA
- target ref
- repository fingerprint
- scope
- acceptance criteria
- requested capabilities

`GitClient.verify_repository_fingerprint()`도 실제 worktree 생성 경로에서 호출되지 않는다.

근거:

- `src/agent_harness/application/orchestrator.py:410-425`
- `src/agent_harness/application/orchestrator.py:441-450`
- `src/agent_harness/execution/git_client.py:137-143`

수정 방향:

- repository identity, source path, base SHA, request snapshot은 Run 생성 시 Harness가 고정한다.
- Planner는 변경 불가능한 값이 아니라 제안 가능한 scope와 criteria만 반환하게 한다.
- Contract acceptance 시 authoritative input과 cross-object invariant를 검증한다.

첫 구현 전 해결 필요: **예**

### H-02 — Worktree 및 lock 경로 containment가 없다

`repository_id`와 `run_id`가 path segment로 직접 사용되지만 identifier validator와 resolved containment 검사가 없다. 기존 검사 fixture는 `ScopeRules` path만 다루며 repository ID를 다루지 않는다.

근거:

- `src/agent_harness/domain/models.py:113-117`
- `src/agent_harness/execution/workspace.py:48-50`
- `src/agent_harness/execution/workspace.py:81-87`

수정 방향:

- repository/run ID를 제한된 slug 또는 Harness 생성 opaque ID로 한정한다.
- 생성·삭제 전에 resolved absolute target이 정확한 data root 아래인지 검사한다.
- symlink, junction, reparse point가 경로 구성 요소에 있으면 거부한다.

첫 구현 전 해결 필요: **예**

### H-03 — 파일 manifest가 directory symlink/junction을 놓칠 수 있다

`os.walk(..., followlinks=False)` 후 `filenames`만 기록한다. directory symlink는 일반적으로 `dirnames`에 나타나므로 canonical manifest에서 누락될 수 있다. Windows junction/reparse point도 별도 검사가 없다.

근거:

- `src/agent_harness/execution/validation.py:52-96`

수정 방향:

- 각 `dirnames` entry를 `lstat()`하고 symlink/reparse point를 manifest entry로 기록한다.
- 외부 대상 링크는 실행 전 차단하거나 명시적 정책 위반으로 기록한다.
- Windows junction fixture를 별도로 추가한다.

첫 구현 전 해결 필요: **예**

### H-04 — 실제 E2E 검사가 성공을 증명하지 않는다

문제:

- 실제 Dual-Agent E2E는 사용자별 credential 파일 경로를 소스에 하드코딩한다.
- API 키를 테스트 내부에서 읽어 전역 환경변수에 설정한다.
- 마지막 assertion이 `final_run.state is not None`뿐이라 `FAILED`도 테스트를 통과한다.

근거:

- `tests/unit/test_orchestrator.py:810-836`
- `tests/unit/test_orchestrator.py:874-879`

수정 방향:

- credential은 테스트 runner가 주입한 환경변수 존재 여부만 확인한다.
- 사용자 이름이나 로컬 secret 경로를 저장소에 넣지 않는다.
- 성공 smoke test는 `READY_FOR_MERGE`와 예상 evidence, invocation, journal을 검증한다.
- 의도된 실패 smoke test는 정확한 FailureCode를 검증한다.

첫 구현 전 해결 필요: **예**

### H-05 — ContainerSandbox 실제 실행이 검증되지 않았다

현재 테스트는 Docker daemon 가용성을 확인하거나 `run_process`를 mock한다. 실제 컨테이너에서 파일 격리, network deny, timeout, process cleanup이 동작하는 테스트는 없다. 테스트 설명의 “Docker가 켜지면 실제 실행” 주장과 구현이 일치하지 않는다.

또한 task env를 `-e KEY=value` argv로 넘겨 host process list에 값이 노출될 수 있다.

근거:

- `tests/unit/test_sandbox.py:54-68`
- `tests/unit/test_sandbox.py:78-108`
- `src/agent_harness/execution/sandbox.py:178-200`

수정 방향:

- Docker opt-in integration marker를 추가하고 실제 컨테이너를 실행한다.
- image를 immutable digest로 고정한다.
- `--cap-drop=ALL`, `no-new-privileges`, PID/memory/CPU 제한을 검토한다.
- 값이 포함된 `-e KEY=value`를 피하고 안전한 secret 전달 방식을 사용한다.

첫 구현 전 해결 필요: 격리 backend를 완료 범위로 선언한다면 **예**

### H-06 — 복구 경로가 완료 조건을 충족하지 않는다

미구현 또는 미연결 항목:

- stale RepoLock 회수
- durable WorkspaceLease reconciliation
- orphan child process 종료
- base revision stale 검사의 recovery 연결
- provider invocation reconciliation
- WSL2 backend

현재 `resume`은 상태만 전이하므로 실제 복구가 아니다.

근거:

- `src/agent_harness/application/recovery.py:10-32`
- `src/agent_harness/execution/workspace.py:39-74`
- `docs/IMPLEMENTATION_SUMMARY.md:143-157`

첫 구현 전 해결 필요: Phase 13 완료를 주장하려면 **예**

## MEDIUM Findings

### M-01 — MCP Gateway는 pipeline과 Provider에 연결되지 않았다

Gateway 자체는 정책, 승인, rate limit, result cap을 모델링하지만 orchestrator 또는 Provider adapter의 도구 호출 경로에서 사용되지 않는다. 따라서 Phase 11은 standalone component 구현이지 end-to-end governance 완료가 아니다.

### M-02 — Scope 제한 일부가 집행되지 않는다

`find_scope_violations()`는 `max_changed_files`를 검사하지만 `max_changed_bytes`와 `declared_generated_paths`를 집행하지 않는다.

### M-03 — 새 테스트 파일 추가가 test mutation으로 분류되지 않는다

`find_test_mutations()`는 modified/deleted만 검사하고 added test files는 제외한다. Worker가 새 테스트를 추가해 결과를 왜곡하는 경우를 별도 정책으로 다뤄야 한다.

### M-04 — Evidence blob purge가 다른 Run의 공유 blob을 손상할 수 있다

Artifact store는 content digest로 blob을 deduplicate하지만 CLI purge는 다른 Artifact 또는 Run이 같은 digest를 참조하는지 확인하지 않고 blob을 삭제한다.

근거:

- `src/agent_harness/persistence/artifacts.py:100-154`
- `src/agent_harness/interfaces/cli.py:313-327`

### M-05 — 현재 테스트 실행에 Pydantic 직렬화 경고가 남는다

Codex adapter 관련 테스트 3건에서 `items_view`가 enum 대신 문자열로 직렬화된다는 경고가 발생한다. SDK 모델 변환 경계가 version 변화에 안전한지 확인해야 한다.

## Confirmed Strengths

다음 기반은 유지하고 확장할 가치가 있다.

- Pydantic 계약과 deterministic JSON Schema 생성
- immutable contract 모델과 digest helper
- 상태 전이 검증 및 terminal immutability
- SQLite의 atomic state transition과 hash-chain journal
- argv-only Command Broker
- shell injection 방지 구조
- 파일 기반 manifest와 content-addressed Artifact Store
- Fake/Replay Provider와 공통 conformance suite
- Codex/Claude SDK client seam
- Policy ceiling 및 Approval digest binding
- 별도 Verifier provider/session 생성

이 기반 때문에 판정은 `REJECT`가 아니라 `REWORK`다.

## Required Rework Order

새로운 standalone 모듈을 추가하기 전에 다음 순서로 실제 실행 경로를 완성해야 한다.

### Priority 0.1 — Durable Execution Records

1. TaskContract, PolicyDecision, Approval, AgentSession, WorkspaceLease, CommandRun, Verification, ReworkContract persistence 추가
2. Artifact/Evidence/Invocation 저장을 orchestrator에 연결
3. 실제 ContextSnapshot 생성 및 저장
4. Run과 Task의 `current_task_id`, `active_contract_id`, revision, attempt 갱신

완료 기준:

- 프로세스 종료 후 DB와 blob만으로 다음 합법 상태와 이전 실행 근거를 복원할 수 있다.

### Priority 0.2 — Re-entrant Step Executor

1. monolithic `run_task_pipeline()`을 상태별 idempotent step으로 분리
2. run claim/lease 도입
3. `approve`, `resume`, `cancel`을 scheduler/step executor와 연결
4. 자동 승인 기본값 제거

완료 기준:

- approval 전 프로세스를 종료하고 다른 프로세스에서 승인한 후 pipeline을 정상 재개할 수 있다.

### Priority 0.3 — Runtime Authorization Boundary

1. Agent tool과 Host command 권한 분리
2. Claude file tool path gate 구현
3. network/package/MCP grant의 runtime 집행
4. Worker prompt에 accepted contract projection 포함
5. worktree root containment 및 symlink/junction 방어

완료 기준:

- Worker가 허용 경로를 수정할 수 있고, 같은 invocation에서 worktree 밖 쓰기는 실행 전에 거부된다.

### Priority 0.4 — Deterministic Host Verification

1. worker-result manifest, post-test manifest 분리
2. scope guard 연결
3. expected exit code, timeout, output cap hard gate
4. test mutation과 test side effect hard gate
5. max changed bytes 및 generated path 집행

완료 기준:

- Verifier가 PASS를 주장해도 실패한 테스트나 scope 위반이 있으면 `READY_FOR_MERGE`에 도달할 수 없다.

### Priority 0.5 — Budget, Timeout, Cancellation

1. Run deadline 계산
2. Provider await timeout 집행
3. usage 누적과 budget 검사
4. cancellation token 및 Provider cancel 연결
5. durable process identity와 process-tree cleanup

완료 기준:

- hang, timeout, cancel, budget exhaustion 각각에 대해 정해진 FailureCode와 증거가 남는다.

### Priority 0.6 — Real Provider CLI

1. provider/policy/command config loader
2. 실제 Provider Registry wiring
3. console script entry point
4. Fake demo 명령 분리
5. 실제 성공/실패 smoke test 강화

완료 기준:

- `harness run --repo ... --request ...`가 실제 Planner, Worker, Verifier를 거쳐 승인 지점에서 정지·재개되고 최종 report를 생성한다.

### Priority 1 — Hardening Completion

1. container 실제 실행 검증
2. WSL2 지원 여부 최종 결정
3. stale lock 및 orphan process recovery
4. base revision stale 연결
5. junction/reparse point 공격 fixture
6. 공유 blob reference-aware retention
7. credential path 제거 및 redaction 확장

## Acceptance Decision

현재 작업 트리는 다음 용도로는 승인할 수 있다.

- 계약 및 상태 머신 기반 코드의 중간 checkpoint
- Fake/Replay 기반 후속 통합 개발의 출발점
- Provider adapter API 탐색 결과

다음 용도로는 승인할 수 없다.

- 실제 저장소에 대한 안전한 Claude Worker 실행
- 사용자 승인 후 process 간 재개
- 감사 가능한 Dual-Agent pipeline
- 악성 또는 비신뢰 저장소 실행
- 자동화된 `READY_FOR_MERGE` 판정
- Phase 13 또는 전체 로드맵 완료 선언

최종 판정은 **REWORK**다.
