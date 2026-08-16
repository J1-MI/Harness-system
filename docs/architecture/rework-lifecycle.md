# Rework Lifecycle — Phase 10

`application/rework.py` + `application/orchestrator.py`의 rework 루프 부분. 로드맵 범위:
"ReworkContract, attempt budget, scope subset, no-progress breaker", 비범위: "자동 rebase".
target: "rework service". 테스트 기준: "max iteration, scope expansion approval, repeated
failure test" — 이 세 개를 각각 정확히 하나씩 테스트로 만들었다.

## H-09가 지적한 문제를 그대로 해결한다

리뷰 원문: "재작업은 단순히 IMPLEMENTING으로 되돌아간다 → 이전 계약의 실패 항목, 허용 경로,
금지 변경이 유실된다. 별도 ReworkContract를 생성하고 검증 결과·수정 요구·시도 번호·추가
권한 요청을 바인딩한다." `application/rework.py::build_rework_contract`가 정확히 이 바인딩을
한다 — Verifier(Codex)의 자유 텍스트 `VerificationResult.required_fixes`/`prohibited_changes`를
그대로 믿고 흘려보내지 않고, Harness가 `parent_contract_digest`/`parent_verification_id`/
`parent_result_snapshot_digest`/`attempt_number`/`failed_criteria_ids`로 못박은 구조화된
`ReworkContract`를 직접 만든다 — Phase 9의 CONTRACT_VALIDATING이 Codex의 TaskContract 제안을
그대로 안 믿고 digest를 직접 계산하는 것과 동일한 패턴이다("Harness가 Codex 제안을 검증해
생성").

## 세 가지 가드 — 로드맵 테스트 기준과 1:1 대응

- **scope subset**: `build_rework_contract`는 반환 직전에 항상
  `domain.validation.assert_rework_scope_is_subset`(Phase 1.1에 이미 있던 함수)을 호출한다.
  `effective_scope`는 기본값이 부모 TaskContract의 scope 그대로(자기 자신의 부분집합이라
  자동으로 통과)지만, 호출자가 더 좁은 scope를 명시적으로 넘길 수도 있다 — 절대 넓힐 수는
  없다. `test_build_rework_contract_rejects_scope_widening`이 확인.
- **attempt budget**: `check_rework_budget(attempt_number, ceiling)`이
  `BudgetRequest.max_rework_iterations`를 넘는 시도를 `ReworkExhaustedError`로 거부한다 —
  "max rework 초과 시 생성 금지"를 문자 그대로 구현. `test_fake_e2e_pipeline_stops_with_
  rework_exhausted_at_max_iterations`가 오케스트레이터 레벨에서 확인.
- **no-progress breaker**: `detect_no_progress(previous_failed_ids, current_failed_ids)`가
  연속된 두 시도가 정확히 같은 criteria 집합에서 실패하면 `True`를 반환한다. 이건 "동일 수정
  반복 금지"(재작업 비용 폭주 공격 표면 #11의 예방 통제)를 iteration budget과 별개로 잡는
  안전장치다 — budget이 5번 남아있어도 두 번 연속 똑같이 실패하면 즉시 멈춘다.
  `test_fake_e2e_pipeline_stops_with_rework_exhausted_on_no_progress`가 확인 (budget은
  5로 여유 있게 설정하고, no-progress만으로 멈추는지 검증).

두 가드 모두 실패하면 `FAILED`로 가고 `FailureCode.REWORK_EXHAUSTED`(Phase 1.1에 이미 있던
코드)를 재사용한다 — 새 코드를 추가하지 않았다: "예산 초과"든 "진전 없음"이든 결국 같은
의미("이 재작업 경로는 포기한다")이기 때문이다.

## 오케스트레이터는 새 파이프라인이 아니라 루프다

Phase 9의 `run_task_pipeline`은 EXECUTING부터 VERIFYING까지 한 번만 돌았다. Phase 10은 그
구간을 `while True` 루프로 감쌌다 — VERIFYING이 `REWORK`을 돌려주면:

1. no-progress / budget 체크 (위 참고, 실패 시 즉시 return)
2. `build_rework_contract`로 ReworkContract 생성
3. `REWORK_CONTRACTING` → `CONTRACT_VALIDATING`으로 전이 (Phase 1.2 상태표에 이미 있던
   합법적 경로: `REWORK_CONTRACTING: {CONTRACT_VALIDATING, FAILED, CANCELLED}`)
4. `requested_additional_capabilities`가 있으면 — 즉 Worker가 이전 시도에서 권한 확장을
   요청했으면 — **기존 승인을 재사용하지 않고** `evaluate_policy`를 처음부터 다시 돈다
   (subject_type=`REWORK_CONTRACT`, capabilities는 원래 요청과 합집합). "추가 capability가
   있으면 기존 승인 재사용 금지"를 그대로 구현. `_gate_policy` 헬퍼를 초기 TaskContract 정책
   평가와 공유해서 두 경로가 완전히 같은 코드를 탄다.
5. `PREPARING_WORKSPACE`로 다시 전이하지만 **`create_worktree`를 다시 호출하지 않는다** — 같은
   worktree를 재사용한다. Worker는 "고치는" 거지 "처음부터 다시 만드는" 게 아니므로, 매
   rework마다 새 worktree를 파는 건 이전 시도의 (부분적으로 맞는) 변경사항을 버리는 셈이다.
6. 다음 `freeze_and_validate`의 baseline을 이전 시도의 **result** manifest/artifact로
   설정한다(rolling baseline) — 그래서 이번 라운드의 manifest diff는 "이번에 뭐가
   바뀌었는지"만 보여준다. 처음 체크아웃 이후 누적 diff가 아니다. 이건 트레이드오프다: 전체
   누적 변경 이력을 한 번에 보고 싶다면 이 설계로는 안 되고, evidence journal을 시도별로
   따로 훑어야 한다. no-progress 판정은 애초에 criteria 집합 비교로 하지 diff 크기로 하지
   않으므로 이 트레이드오프가 breaker의 정확도에 영향을 주지 않는다.

`run_verification`에는 매 시도마다 **원본** `contract`(ReworkContract가 아니라)를 넘긴다 —
acceptance criteria와 objective는 rework로 바뀌지 않기 때문이다("objective 변경 금지"). 오직
Worker의 프롬프트만 `_build_worker_prompt(contract, rework_contract)`로 바뀐다: rework
시도에서는 `required_fixes`/`prohibited_changes`를 명시적으로 나열한다.

## `AWAITING_FINAL_APPROVAL` 거부는 왜 자동으로 루프를 안 도는가

Verifier의 REWORK 판정과 달리, 사람이 최종 승인을 거부하는 경우엔 `required_fixes`가 없다 —
무엇을 고쳐야 하는지 구조화된 정보가 아예 없다. 그래서 이 경우엔 Phase 9와 동일하게
`REWORK_CONTRACTING`으로 전이하고 거기서 멈춘다(자동으로 CONTRACT_VALIDATING까지 안 간다).
사람이 왜 거부했는지 알려줘야 다음 rework를 만들 수 있는데, 그건 이번 phase의 범위 밖이다.

## 비범위

Base ref가 이동했을 때의 자동 rebase(H-08, 명시적으로 금지 — `BASE_REVISION_STALE`은 여전히
수동 재검토 대상), Verifier 전용 rework 승인 UI, `Task.attempt_count` 필드 자체의 영속화
갱신(현재 `attempt_number`는 오케스트레이터의 로컬 루프 변수로만 추적되고 `Task` 행에
다시 쓰지는 않는다 — Task 갱신 API는 아직 없다), 여러 Task 간 rework 우선순위 조정.
