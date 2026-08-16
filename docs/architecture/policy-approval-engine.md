# Policy and Approval Engine — Phase 4

`policy/models.py`(PolicyCeiling), `policy/paths.py`+`policy/commands.py`(intersection),
`policy/evaluator.py`(결정적 PolicyDecision 생성), `policy/approvals.py`(Approval 바인딩)의
설계 결정. 로드맵 범위: "requested/granted split, path/command/network policy, approval
digest/expiry". MCP 실행 자체는 비범위. Handoff 프롬프트 없이 섹션 3의 우선순위 표를 스펙으로
설계했다.

## 우선순위를 코드 구조로 그대로 옮겼다

```
Hard-coded safety invariants > administrator policy > deployment profile
> user approval > repository suggestions > planner requests
```

- **Hard-coded invariant**: `policy/evaluator.py`의 `_HARD_DENIED_NETWORK_DOMAINS`
  (클라우드 metadata endpoint — `169.254.169.254` 등). 이건 `PolicyCeiling` 설정으로도,
  `Approval`로도 절대 뒤집을 수 없다. `test_metadata_endpoint_is_denied_even_when_ceiling
  _explicitly_allows_it`가 ceiling이 명시적으로 허용해도 여전히 DENY임을 증명한다.
- **administrator policy**: `PolicyCeiling` — command_id 허용 목록, network domain 허용
  목록, raw_shell/package_install 허용 여부, MCP tool/external system/database target
  허용 목록, sandbox_profile, budget 상한. 지금은 관리자가 코드로 직접 구성한다
  (`policy.yaml` 로딩은 비범위).
- **deployment profile / repository suggestions**: 아직 모델링하지 않음.
- **user approval**: `policy/approvals.py`.
- **planner requests**: `TaskContract.requested_capabilities`(Phase 1.1) — evaluator의
  입력일 뿐 절대 출력이 아니다.

## requested ∩ ceiling = grants

`evaluate_policy()`는 요청된 capability를 `PolicyCeiling`과 교집합해서 `PolicyGrants`를
계산한다: 경로는 허용 집합의 교집합·금지 집합의 합집합(`policy/paths.py::intersect_scope`),
숫자 상한은 더 엄격한 쪽, command_id는 교집합(`policy/commands.py`). ceiling이 전혀 허용하지
않는 것을 요청하면(`CEILING_FORBIDS_*` reason code) 전체 결정이 `DENY`가 된다 — 일부만 조용히
깎아서 승인하지 않는다.

## 승인이 필요한 capability는 ceiling이 허용해도 자동 ALLOW되지 않는다

network access, package install, raw_shell, MCP tool, external system, database target 중
하나라도 요청되면(그리고 ceiling이 허용하는 한) `REQUIRE_APPROVAL`이다. 이 목록에 없는
capability(기본 workspace read/write, ceiling에 있는 command_id)만 `ALLOW`로 자동 통과한다.
raw_shell은 ceiling이 허용해도 여전히 사람 승인이 필요하다 — 리뷰의 "raw shell capability에
대한 별도 정책 결정" 원칙 그대로다.

## Approval은 DENY를 절대 뒤집지 못한다

`create_approval()`과 `resolve_decision_with_approval()` 둘 다 `decision.outcome is DENY`이면
즉시 `PolicyApprovalError`를 던진다 — 승인 객체를 아예 만들 수조차 없고, 손으로 조작해서
만들어 온 "APPROVED" Approval을 억지로 들이밀어도(`test_resolve_decision_with_approval
_rejects_hard_deny_even_with_forged_approval`) 통과시키지 않는다. DENY를 바꾸려면 정책
자체(`PolicyCeiling`)를 바꿔야 한다.

## Approval은 정확한 digest에 바인딩되고 만료된다

`resolve_decision_with_approval()`은 `domain.validation.validate_approval_binding`(Phase
1.1에서 이미 만든 함수)으로 `subject_digest`와 `policy_decision_digest`(=
`PolicyDecision.integrity_digest`)가 정확히 일치하는지 먼저 확인한다. 계약이 재평가되어 새
`PolicyDecision`(새 digest)이 나오면, 옛 승인은 자동으로 적용되지 않고 새로 받아야 한다
(`test_approval_does_not_apply_to_a_re_evaluated_decision`). `expires_at`이 지난 승인도
`PolicyApprovalError`로 거부한다(`test_resolve_decision_with_approval_rejects_expired
_approval`) — stale approval이 조용히 통과하지 않는다.

## 비범위

`policy.yaml`/`command_catalog.yaml` 파일 로딩, deployment profile, repository가 제안하는
정책 힌트, MCP tool 실제 실행/governance, `Approval.single_use` 실제 소비 추적(한 번 쓰인
승인을 "사용됨"으로 마킹해서 재사용을 막는 것 — 이건 persistence 연동이 필요해서 이후 Phase),
Phase 1.1의 `TaskContract.acceptance_criteria`와 이 Phase의 결정을 실제로 orchestration에
연결하는 것(Phase 9).
