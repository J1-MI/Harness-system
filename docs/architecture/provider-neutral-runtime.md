# Provider-neutral Runtime — Phase 5

`providers/registry.py`, `providers/capabilities.py`, `providers/event_stream.py`,
`providers/cancel.py`, `providers/replay.py`, `application/ports.py`,
`application/usage.py`의 설계 결정. 로드맵 범위: "registry, capability negotiation, event
normalization, cancel, usage, Fake/replay provider". 실제 Codex/Claude 연동은 비범위.
Handoff 프롬프트 없이 로드맵 행 + 섹션 7을 스펙으로 설계했다.

## Registry는 provider 인스턴스가 아니라 역할을 조회한다

`ProviderRegistry.register(role, provider)` / `.get(role)`. 하나의 provider 인스턴스가 여러
역할(PLANNER+VERIFIER 등)을 동시에 처리할 수 있다는 M-02 원칙 그대로: registry는 역할과
provider의 다대일 매핑을 허용할 뿐, "역할마다 다른 클래스"를 강제하지 않는다. `probe()`는
등록 시점 기준으로 capability를 캐시한다 — provider 프로세스가 재시작해서 driver 버전이 바뀔
수 있는 경우를 대비해, 캐시를 갱신하려면 `register()`를 다시 호출해야 한다는 걸 명시적으로
문서화했다.

## Capability negotiation: 실패는 전부 한 번에 보여준다

`require_capabilities()`는 첫 번째 미달 항목에서 멈추지 않고 모든 축을 검사한 뒤
`ProviderCapabilityError` 하나에 전부 모아서 던진다. `streaming`/`session_resume`/
`structured_output`/`mcp_control`/`usage_reporting`는 순서가 있는 enum이라 "더 강한 지원이
더 약한 요구를 만족한다"는 rank 비교를 쓰고(`PARTIAL_TOKENS`가 `EVENTS` 요구를 만족하는 식),
`native_cancel` 같은 boolean capability는 정확히 일치해야 한다. 요구를 충족하지 못하면
예외 하나로 끝 — silent downgrade 경로 자체가 없다(섹션 7: "요구 capability가 없으면
downgrade하지 말고 CAPABILITY_MISMATCH로 fail closed").

## Event normalization: 중복은 넘어가고, 역순은 무조건 예외

`normalize_events()`는 완전히 같은 `sequence`가 다시 오면(재연결 시 이미 처리한 이벤트를
다시 보내는 등 benign한 상황) 조용히 건너뛴다. 하지만 `sequence`가 거꾸로 가거나, 다른
invocation의 이벤트가 섞여 들어오면 `OutOfOrderEventError`로 즉시 중단한다 — 섹션 8의 공격
경로 15("schema downgrade·event replay")에 대한 탐지 통제를 그대로 구현했다. 어댑터 버그든
스트림 변조든, 순서를 신뢰할 수 없는 이벤트 위에서 계속 진행하지 않는다.

## Cancel: capability 없으면 아무것도 안 하는 대신 예외

`cancel_invocation()`은 `require_capabilities(capabilities, CapabilityRequirement
(native_cancel=True))`를 먼저 통과해야 실제로 `provider.cancel()`을 호출한다.
`native_cancel=False`인 provider에 취소를 요청하면 "취소된 것처럼 보이지만 사실 아무 일도
안 일어난" 상태가 되는 게 가장 위험하므로, 그 경우는 반드시 예외로 드러낸다. 실제로 hang된
provider host process를 강제로 죽이는 supervisor는 아직 없다(실제 Provider adapter가 없는
이번 Phase 범위 밖).

## Fake/Replay: 하나의 구현을 재사용한다

`providers/replay.py`는 `AgentProvider`를 처음부터 다시 구현하지 않는다.
`build_replay_provider()`는 JSON 레코딩(각 모델의 `model_dump(mode="json")` 그대로의 모양)을
파싱해서 `FakeAgentProvider.queue_invocation()`을 호출할 뿐이다 — Fake와 Replay는 "어떻게
시나리오가 채워지는가"만 다르고 실행 로직은 완전히 같다. 이 재사용이 곧 conformance suite의
전제이기도 하다: 둘 다 진짜로 같은 Protocol을 만족하는지 별도로 증명해야 의미가 있다.

## Conformance suite

`tests/contract/provider_conformance.py::run_conformance_suite()`는 `test_*.py`가 아닌
평범한 헬퍼 모듈이다(pytest가 이걸 테스트 파일로 오인해서 잘못 수집하지 않도록). Fake와
Replay 양쪽에 대해 동일한 시나리오(health_check → capabilities → session start/resume →
invocation 2개로 이벤트 격리 확인 → cancel(지원하는 경우만) → close_session)를 돌려서, "각
provider가 서로 다르게 동작해도 Protocol 계약만큼은 똑같이 지킨다"는 걸 실제로 검증한다.
나중에 실제 Codex/Claude adapter(Phase 6~7)가 생기면 이 suite에 한 줄만 추가하면 된다.

## Usage/budget: Phase 1.1의 필드를 실제로 쓰기 시작했다

`application/usage.py::accumulate_usage()`/`check_budget()`은 Phase 1.1에서 정의만 해두고
아무도 쓰지 않던 `Run.budget_used`/`budget_limits`, `PolicyGrants.budgets`를 실제로 연결한
첫 코드다. `BudgetRequest`가 상한을 두는 축(turns, rework)만 검사한다 — token/cost는
`UsageRecord`가 여전히 client-side 추정치(Claude Agent SDK cost-tracking 문서가 명시한 대로)
라서, 정밀한 과금 대신 안전 마진으로만 취급한다.

## 비범위

실제 Codex/Claude adapter(Phase 6~7), MCP governance, Provider Host 프로세스 분리(Phase 3.2
에서 만든 sandbox와는 별개로 Provider 자체를 감독하는 helper process), `Approval.single_use`
소비 추적과 마찬가지로 `BudgetUsage`의 실제 영속화·원자적 갱신(Phase 2.1의 `apply_transition`
에 아직 연결하지 않았다 — 이건 orchestration Phase(9)의 몫에 더 가깝다).
