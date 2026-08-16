# Contract Kernel — Phase 1.1

이 문서는 `AGENT_HARNESS_ARCHITECTURE_REVIEW.md` 12절의 핸드오프 프롬프트에 따라 구현된
"Executable Contract Kernel"의 설계 결정을 기록한다. 코드가 아니라 *왜 이렇게 만들었는가*에
집중한다.

## 1. Pydantic v2가 schema의 단일 원천인 이유

`src/agent_harness/domain/models.py`의 Pydantic 모델이 유일한 계약 정의이고,
`schemas/generated/*.json`은 `model_json_schema()`로부터 파생된 결과물이다. 손으로 JSON
Schema를 따로 관리하지 않는 이유는, 리뷰 문서의 M-03(JSON Schema와 Pydantic을 이중 관리하면
schema drift가 발생한다)을 그대로 반영한 것이다.

`tests/contract/test_schema_generation.py::test_checked_in_schemas_match_freshly_generated_schemas`가
재생성 결과와 커밋된 파일을 바이트 단위로 비교해서, 모델을 고치고 스키마 재생성을 잊는 실수를
빌드 실패로 잡는다. `python -m agent_harness.schema_export`가 유일한 생성 경로다.

## 2. requested capability와 granted capability의 분리

`TaskContract.requested_capabilities`(`RequestedCapabilities`)는 Codex Planner가 "이런 권한이
필요할 것 같다"고 적어 내는 요청일 뿐이며, 그 자체로는 아무 권한도 부여하지 않는다.
`TaskContract`에는 `approval_required`나 `granted_capabilities` 같은 필드가 존재하지 않는다
(`tests/unit/test_domain_models.py::test_task_contract_has_no_effective_capability_field`가
이 불변조건을 구조적으로 검증한다).

유일한 유효 권한의 원천은 `PolicyDecision.grants`(`PolicyGrants`)이며, 이는 신뢰된 Policy
Engine만 생성한다(이번 Phase에서는 Policy Engine 자체는 범위 밖이고, 모델과 불변조건만
존재한다). `AgentRunRequest.effective_policy_grants`도 `PolicyGrants`를 참조하지, Task
Contract의 요청 필드를 직접 참조하지 않는다. 이는 리뷰의 B-02(Planner가 실행 정책을 직접
만들면 스스로 권한을 부여하는 셈이 된다)에 대한 구조적 대응이다.

## 3. provider-reported evidence와 host-observed evidence의 구분

`WorkerResult`의 모든 주장 필드는 `reported_` 접두사를 가진다
(`reported_changed_files`, `reported_commands`, `reported_tests`). 이는 Claude Worker가
스스로 보고한 내용이 "사실"이 아니라 "주장"임을 타입 수준에서 드러내기 위한 명명 규칙이다
(리뷰 B-03).

`EvidenceRecord.provenance.trust_tier`는 `HOST_OBSERVED`부터 `PROVIDER_REPORTED`까지
신뢰 등급을 명시하고, `EvidenceRecord`의 모델 검증자는 `producer_type`이
`WORKER`/`PLANNER`/`VERIFIER`인데 `trust_tier`가 `HOST_OBSERVED`인 조합을 거부한다 —
에이전트가 자기 주장을 스스로 "host-observed"로 격상시킬 수 없다.

`VerificationResult`의 PASS 불변조건(`domain/validation.py::assert_valid_pass`)도 같은
원칙을 따른다: mandatory criterion이 전부 PASS 또는 유효한 승인 참조가 있는 WAIVED여야
하고, `NOT_VERIFIED`가 하나라도 남아 있거나 미해결 BLOCKER/HIGH 보안 finding이 있으면
PASS를 거부한다. Codex Verifier의 `decision=PASS`는 권고일 뿐이며, 공식 disposition은
Harness가 이 불변조건으로 재검증한 뒤에만 유효하다는 리뷰의 원칙(6번 섹션, "Official
decision")을 코드로 표현한 것이다.

## 4. immutable record와 canonical digest 규칙

- `ImmutableModel`(`frozen=True`, `extra="forbid"`)을 상속하는 모델(`TaskContract`,
  `WorkerResult`, `VerificationResult`, `ReworkContract`, `PolicyDecision`, `Approval`,
  `Artifact`, `EvidenceRecord`, `JournalEntry`, `ContextSnapshot`, `FailureRecord`,
  `CommandRun`, `AgentEvent`, `AgentRunResult`)은 accepted/terminal 레코드이며 생성 후
  변경할 수 없다. `Run`/`Task`/`AgentSession`/`AgentInvocation`/`WorkspaceLease`/
  `CommandSpec`은 Harness가 계속 갱신하는 살아있는 집합체이므로 `HarnessModel`
  (`frozen=False`, `extra="forbid"`)을 쓴다.
- digest는 항상 `sha256:<64자 소문자 hex>` 형식이며(`domain/digests.Digest`), canonical
  JSON은 key 정렬 + 압축 구분자 + UTF-8로 직렬화한다(`canonical_json_bytes`).
- `compute_model_digest(model, exclude_fields=...)`는 digest를 담는 필드 자신을 반드시
  제외하고 계산한다 — 그렇지 않으면 digest가 자기 자신을 참조하는 순환이 생긴다. 예를 들어
  `TaskContract`의 digest를 계산할 때는 `integrity` 필드를 제외해야 한다.
- 경로는 `RelativePath`(POSIX `/`, root-relative)로 강제하고 절대 경로, 빈 segment,
  `.`/`..` segment, NUL 바이트를 거부한다(`domain/digests.normalize_relative_path`).
- `base_commit_sha`는 symbolic ref가 아니라 40자 또는 64자 소문자 hex 전체 커밋 ID여야
  한다(`domain/models.validate_commit_sha`) — 리뷰 H-08(base branch가 이동해도 이전 결과가
  유효한 것처럼 보이는 문제)에 대한 첫 방어선이다.

## 5. ReworkContract scope 축소 검증

`domain/validation.is_scope_subset`은 rework의 `effective_scope`가 부모 `TaskContract.scope`
의 부분집합인지 순수 함수로 검사한다: 허용 경로는 부모의 허용 집합 안에 있어야 하고, 금지
경로는 부모의 금지 집합을 전부 포함해야 하며, 변경 파일/바이트 상한은 낮아지기만 할 수 있다.
새 승인 없이는 rework가 원래 계약보다 넓은 권한을 가질 수 없다(리뷰 H-09).

## 6. Provider Protocol과 Fake Provider

`providers/protocol.py`는 역할(PLANNER/WORKER/VERIFIER)마다 별도 Protocol을 만들지 않고
`AgentRole` enum과 단일 `AgentProvider` Protocol로 표현한다(리뷰 M-02). `UsageRecord`와
`ProviderError`는 `providers/`가 아니라 `domain/models.py`에 정의되어 있는데, 이는
`AgentInvocation`(domain 집합체)이 이 두 타입을 저장해야 하고 domain은 바깥 계층
(`providers`, `application` 등)에 의존할 수 없기 때문이다. `providers/protocol.py`는
domain으로부터 이 타입을 가져와 재사용한다 — 반대 방향 의존은 금지된다.

`providers/fake.py`의 `FakeAgentProvider`는 외부 네트워크나 API 키 없이 동작하는 최소
conformance double이다. `queue_invocation`으로 역할별 FIFO에 시나리오
(`ScriptedInvocation`)를 등록해 두면 `start_invocation`이 하나씩 소비하므로, 동시에 여러
invocation이 진행되어도 이벤트 시퀀스가 서로 섞이지 않는다. 지원하지 않는 capability
(`session_resume=NONE`인데 `resume_session` 호출, `native_cancel=False`인데 `cancel` 호출,
`supported_roles`에 없는 역할로 세션 시작 등)는 downgrade하지 않고 항상
`ProviderCapabilityError`로 fail-closed한다(리뷰: "요구 capability가 없으면 downgrade하지
말고 CAPABILITY_MISMATCH로 fail closed").

## 7. 이번 Phase의 비범위

다음은 의도적으로 구현하지 않았다 (핸드오프 프롬프트의 "명시적 비범위" 그대로):

- Codex/Claude 실제 SDK, CLI, App Server 연동
- API key/OAuth 처리
- SQLite 및 migration, Operation Journal의 실제 영속화
- Git worktree 생성/정리, subprocess/shell 실행, 실제 sandbox/container/WSL2
- Policy Engine의 실제 평가 로직 (모델과 불변조건만 존재; 정책 계산 자체는 없음)
- Approval CLI, Typer CLI
- MCP server/client
- 실제 파일 diff, test runner, evidence collector
- 상태 전이 엔진 (`LifecycleState` enum과 전이 허용 여부 표는 존재하지만
  `application/transitions.py`에 해당하는 전이 서비스는 구현하지 않음)
- orchestration pipeline, rework loop 실행
- commit, push, merge, deploy

코드나 이 문서 어디에도 위 항목을 지원한다고 주장하지 않는다. 다음 단계(Phase 1.2 이후)에서
상태 전이 엔진, SQLite 영속화, 실제 Provider 어댑터가 이 Contract Kernel 위에 쌓인다.
