# Dual-Agent Pipeline — Phase 9

`application/orchestrator.py`의 설계 결정. 로드맵 범위: "plan→policy→approval→workspace→
worker→freeze→host validate→verify→final approval", DAG/MCP/multi-repo는 비범위. 이 문서가
설명하는 건 Phase 1~8을 실제로 하나의 실행 경로로 엮은 첫 코드다 — 이전 8개 phase는 전부
독립적으로 검증된 부품이었고, Phase 9에서 처음으로 그 부품들이 실제로 서로를 호출한다.

## Provider는 role별로 registry에서 찾는다 — Claude/Codex를 직접 import하지 않는다

`run_task_pipeline`은 `providers.claude`/`providers.codex`를 전혀 import하지 않는다.
`PipelineDeps.provider_for_role: Callable[[AgentRole], AgentProvider]`로 Planner/Worker를,
`verifier_provider_factory`로 Verifier를(Phase 8의 "fresh provider per call" 계약을 그대로
받아서) 주입받는다. 그래서 정확히 같은 오케스트레이터 코드가 `FakeAgentProvider`(Fake E2E)와
실제 Claude/Codex 어댑터(opt-in live E2E) 양쪽에서 동일하게 돈다 — Fake E2E가 "진짜로
의미있는" 이유가 이거다: 오케스트레이터의 실제 제어 흐름을 태우는 거지, 테스트 전용 우회
경로가 아니다.

## Harness가 TaskContract의 digest를 직접 계산한다 — Planner가 뭐라고 주장하든 안 믿는다

Planner(Codex)의 structured output은 `TaskContract` 스키마를 따르지만, 그 안의
`integrity.canonical_digest`는 신뢰하지 않는다. CONTRACT_VALIDATING 단계에서
`compute_model_digest(draft_contract, exclude_fields={"integrity"})`로 Harness가 직접
계산한 다음, 그 값으로 새 `IntegrityRef`를 만들어 갈아끼운다 — "accepted revision"을
Harness가 만든다는 설계 의도(review 원문: "TaskContract는 Codex가 제안하고 Harness가
accepted revision을 생성")를 코드로 그대로 옮긴 것이다.

## 정책 승인/최종 승인은 주입된 콜백 — 오케스트레이터가 UI를 모른다

`decide_policy_approval`/`decide_final_approval`은 둘 다 `Callable[..., Awaitable[bool]]`로
주입된다. 기본값은 `_default_approve`(항상 `True`) — Fake E2E는 이걸 그대로 쓰거나 호출
횟수를 세는 콜백으로 교체해서 "승인이 실제로 호출됐는지"를 검증한다. 실제 배포에서는 이
자리에 사람에게 물어보는 콜백이 들어간다. 오케스트레이터 자신은 승인이 어떻게 이루어지는지
전혀 모른다 — bool 하나만 돌려받는다.

## 상태 전이는 전부 Phase 2.1의 `apply_transition`을 그대로 쓴다

새 상태 전이 로직을 만들지 않았다. `_advance()`/`_fail()` 두 헬퍼가 전부 Phase 2.1
`persistence.sqlite.apply_transition`을 호출한다 — Phase 1.2의 순수 `transition_run()`을
SQLite journal에 원자적으로 묶은 바로 그 함수다. 그래서 이 오케스트레이터가 실수로 허용되지
않은 전이(TRANSITIONS 테이블에 없는 target)를 시도하면 `IllegalTransition`이 그대로 터진다 —
오케스트레이터 레벨에서 상태 기계를 다시 구현하지 않았기 때문에 이 안전장치가 공짜로 따라온다.

`PENDING_ACTION_REQUIRED_STATES`(`AWAITING_APPROVAL`/`AWAITING_MANUAL_REVIEW`/
`AWAITING_FINAL_APPROVAL`)로 전이할 때만 `PendingAction`을 넘기고, 그 외에는 넘기지 않는다 —
이것도 Phase 1.2가 이미 강제하는 불변조건이라 오케스트레이터는 그냥 맞춰서 호출할 뿐이다.

## Verifier 판정에 따른 분기 — REWORK/MANUAL_REVIEW는 "멈춘다", REJECT만 FAILED

Phase 8의 `VerifiedVerification.accepted_decision`을 그대로 분기한다:

- `REJECT` → `FAILED` (FailureRecord 기록, 종료)
- `REWORK` → `REWORK_CONTRACTING`으로 전이하고 **거기서 멈춘다** — 실제 재작업 루프 실행은
  Phase 10
- `MANUAL_REVIEW` → `AWAITING_MANUAL_REVIEW`로 전이하고 멈춘다 — 사람이 봐야 함
- `PASS` → `AWAITING_FINAL_APPROVAL` → 콜백 승인되면 `READY_FOR_MERGE`, 거부되면
  `REWORK_CONTRACTING`(마찬가지로 거기서 멈춘다)

REWORK_CONTRACTING으로의 전이 자체는 상태 기계가 이미 허용하는 유효한 target이라 (Phase
1.2 TRANSITIONS 테이블에 VERIFYING→REWORK_CONTRACTING, AWAITING_FINAL_APPROVAL→
REWORK_CONTRACTING 둘 다 있음) 이 상태로 "들어가는" 것과 "재작업을 실제로 도는" 것을
분리해서, 전자만 이번 phase가 하고 후자는 다음 phase로 명확히 넘겼다.

## Fake E2E: 실제로 파일을 건드리는 fake Worker/Verifier

`FakeAgentProvider`(Phase 1.1)는 스크립트된 결과만 돌려줄 뿐 파일시스템을 건드리지 않는다.
하지만 Phase 3.3의 freeze/host-validate 단계는 뭔가 실제로 바뀌어야 diff가 의미있다. 그래서
테스트 전용 서브클래스 두 개를 만들었다:

- `FileWritingWorkerProvider`: `start_invocation`에서 `request.workspace_handle`(실제
  worktree 경로)에 파일 하나를 직접 씀 — 실제 Worker 어댑터가 도구 실행으로 파일을 바꾸는
  것과 동일한 자리에서 동일한 정보(workspace_handle)만 갖고 흉내낸다.
- `EvidenceCitingVerifierFactory`: Verifier의 `evidence_refs`는 `freeze_and_validate`가
  runtime에 생성하는 진짜 evidence ID라서 테스트 작성 시점엔 알 수 없다. 그래서 이
  fake Verifier는 자신에게 주어진 프롬프트 텍스트를(`resolve_prompt`로 실제로 resolve해서)
  파싱해 `build_verifier_prompt`가 넣은 "EVIDENCE RECORDS" JSON 섹션에서 진짜 evidence_id를
  뽑아 그대로 인용한다 — Phase 8의 `find_missing_evidence_violations`가 가짜/빈 evidence를
  진짜로 거부하는지까지 자연스럽게 같이 검증된다.

세 개의 Fake E2E 테스트가 각각: (1) 정상 경로로 `READY_FOR_MERGE`까지 도달 + journal의 상태
시퀀스 9개가 정확히 일치 + hash-chain 연결까지 확인, (2) `raw_shell=True`로
`AWAITING_APPROVAL`을 강제로 태우고 승인 콜백이 정확히 1번 불렸는지 확인, (3) evidence 없는
PASS 주장이 `AWAITING_MANUAL_REVIEW`에서 멈추는지(= `disposition`이 아직 `None`인지, 종료
상태가 아님을 직접 확인) 검증한다.

## Opt-in live E2E는 작성만 하고 이번 세션에서 실행하지 않았다

`test_live_dual_agent_pipeline_smoke`(env var `RUN_LIVE_DUAL_AGENT_E2E=1`로 opt-in)는 실제
`ClaudeAgentAdapter` + `CodexPlannerAdapter` 두 개를 오케스트레이터에 그대로 꽂는다. Phase
6/7과 달리 이번엔 새 실제-SDK 어댑터를 만든 게 아니라 이미 개별적으로 라이브 검증된 어댑터
두 개를 조합하는 것뿐이고, Phase 6에서 이미 기록된 "Anthropic 계정 크레딧 부족"
billing 이슈가 Worker 호출에서 그대로 재현될 게 확실해서(같은 키, 같은 계정), 이번엔 실행
비용 대비 새로 배울 게 없다고 판단해 opt-in 코드만 남기고 실제 실행은 생략했다.

## 비범위

DAG(Run당 여러 Task를 의존성 그래프로 실행), MCP 도구 승인 흐름, multi-repo Task, 그리고
`REWORK_CONTRACTING`/`AWAITING_MANUAL_REVIEW`에 실제로 진입한 뒤의 루프 실행(ReworkContract
생성, 재시도, `attempt_count` 증가) — 전부 Phase 10 이후. Verifier 전용 RoleProfile 설정
파일도 Phase 8과 동일하게 아직 문자열 ref만 받는다.
