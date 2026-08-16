# Codex Verifier Adapter — Phase 8

`application/verification.py`의 설계 결정. 로드맵 범위: "fresh session, evidence-only snapshot,
VerificationResult, PASS invariants". 재작업 loop 실행(Phase 10)은 비범위. Handoff 프롬프트
없이 로드맵 행 + 섹션 4/6(H-06)을 스펙으로 설계했다.

## Provider 코드는 안 건드렸다 — 이미 role-neutral이었다

`providers/codex.py`(Phase 7)는 이미 PLANNER/VERIFIER 둘 다 처리하도록 만들어져 있었다(M-02:
역할은 데이터지 별도 Protocol이 아니다). Phase 8이 실제로 만든 건 "Verifier 호출이 무엇을 볼 수
있는가"를 강제하는 **application 계층 서비스**다 — 로드맵이 target을 "verifier
profile/application service"라고 명시한 이유가 이거였다.

## Fresh session을 가장 강한 의미로 해석했다

`run_verification()`은 이미 만들어진 provider를 받지 않는다. 대신
`provider_factory: Callable[[resolve_prompt], AgentProvider]`를 받아서 **매 호출마다 새
provider 인스턴스를 만들고, 그 안에서 새 session을 연다.** 세션만 새로 여는 게 아니라 provider
객체 자체도 새로 만드는 이유: 세션 재사용 방지만으로는 "그 provider 인스턴스가 이전 호출에서
내부적으로 뭘 기억하고 있는지"까지는 보장할 수 없다. H-06의 "새 세션"을 가장 보수적으로
해석했다.

## Evidence-only context: Worker의 말은 기본적으로 아예 안 보여준다

`build_verifier_prompt()`는 `TaskContract.acceptance_criteria`, `FrozenValidationResult`의
manifest diff/test mutation/host check 실행/evidence record만 넣는다. `WorkerResult`
(`untrusted_worker_claims` 파라미터)는 **기본값이 `None`**이다 — 아예 프롬프트에 들어가지
않는다. 호출자가 명시적으로 넘기면 "UNTRUSTED WORKER CLAIMS" 섹션에 EVIDENCE RECORDS 섹션과
분리해서 넣지만, 리뷰의 권장 기본 경로는 아예 안 주는 쪽이다. 테스트가 Worker의
`implementation_summary` 텍스트가 기본 프롬프트에 전혀 나타나지 않음을 직접 문자열로 확인한다.

## PASS invariant: Phase 1.1 검증기로는 부족했다 — 진짜 evidence 대조를 추가했다

`domain.validation.find_pass_invariant_violations`(Phase 1.1)는 `VerificationResult` 자체의
구조만 본다 — mandatory criterion이 다 있는지, `NOT_VERIFIED`가 없는지, unresolved
BLOCKER/HIGH가 없는지. 하지만 모델이 `criteria=[{"id": "crit-1", "result": "PASS",
"evidence_refs": []}]`처럼 **구조적으로는 멀쩡하지만 실제로는 아무 근거도 없는** PASS를 주장하면
Phase 1.1의 검증기만으로는 못 잡는다 — 그 함수는 Artifact/Evidence store 존재 자체를 모르는
Phase에서 만들어졌기 때문이다.

그래서 `find_missing_evidence_violations()`를 새로 만들었다: `criterion.result == PASS`인 모든
항목에 대해 `evidence_refs`가 (a) 비어있지 않고 (b) 이번 Run의 `FrozenValidationResult.evidence`
안에 실제로 존재하는 `evidence_id`만 가리키는지 확인한다. 둘 다 아니면 위반이다. 이게 진짜
"missing evidence PASS 거부"다 — 모델이 근거 없이 PASS를 주장하거나, 존재하지 않는 evidence
ID를 지어내도 통과 못 시킨다. 테스트 세 개가 각각: evidence_refs가 빈 경우, 존재하지 않는 ID를
지어낸 경우, 진짜 evidence ID를 정확히 인용한 경우(이건 통과해야 함)를 검증한다.

## Official decision은 아직 아니다 — 여기서 하는 건 그 중 한 항

섹션 4: `Official decision = deterministic safety gates + host validation results + validated
Codex VerificationResult + required human approvals`. `VerifiedVerification.accepted_decision`
은 이 합 중 "validated Codex VerificationResult" 항만 계산한다. 모델이 PASS를 주장했는데
불변조건 위반이 있으면 `MANUAL_REVIEW`로 낮춘다 — `REJECT`로 자동 뒤집지 않는다. 거짓 PASS를
자동으로 REJECT로 바꾸는 것도 일종의 자동 판정이라, 사람이 봐야 한다는 원칙(REWORK/REJECT
자체를 우리가 대신 결정하지 않는다)을 지켰다. REWORK/REJECT/MANUAL_REVIEW를 모델이 이미
주장했다면 그대로 통과시킨다 — 모델이 "지나치게 신중한" 방향으로 틀리는 건 안전 문제가 아니다.

## 비범위

실제 host validation 결과와 사람 승인을 합쳐 최종 official disposition을 계산하는 오케스트레이션
(Phase 9), ReworkContract 생성/재작업 루프(Phase 10), Verifier 전용 별도 RoleProfile 설정 파일
(`worker_profile_ref`처럼 저장된 profile — 지금은 호출자가 `role_profile_ref`/`role_profile_digest`
문자열만 넘긴다), evidence-artifact 무결성을 넘어선 더 깊은 교차검증(예: manifest diff와
evidence 사이의 일관성 검사).
