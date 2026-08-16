# State Machine Kernel — Phase 1.2

`application/transitions.py`의 설계 결정. 로드맵 표(섹션 11, "1.2 — State Machine Kernel")
행에 명시된 범위: transition table, terminal immutability, pending action, FailureRecord
연결. 이번 Phase에는 별도 handoff 프롬프트가 없어서, 섹션 5(State Machine Review)를 스펙으로
삼아 직접 설계했다.

## 전이표

섹션 5의 "허용 전이" 표를 `TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]]`로
그대로 옮겼다. `tests/unit/test_transitions.py`는 이 표를 독립적으로 다시 옮겨 적은 뒤
16×16 전체 상태쌍에 대해 `can_transition()`과 대조한다 — 구현이 스펙과 다르면(오타 포함)
반드시 실패하도록, 테스트가 구현의 테이블을 그대로 import하지 않는다.

`RECOVERY_REQUIRED`의 목적지는 섹션 5의 "재시작 가능한 상태" 두 목록(즉시 재시작 가능 +
reconcile 후 재시작 가능)의 합집합으로 계산했다. `EXECUTING`은 의도적으로 제외했다 —
"EXECUTING 중 crash가 발생하면 자동으로 같은 명령을 재실행해서는 안 된다"는 문장을,
RECOVERY_REQUIRED에서 EXECUTING으로 직접 돌아갈 수 없다는 전이 규칙으로 표현했다.

## Terminal immutability

`READY_FOR_MERGE`/`FAILED`/`CANCELLED`는 `TRANSITIONS`에서 빈 집합으로 매핑되어 있고,
`validate_transition()`은 그와 별개로 `current in TERMINAL_LIFECYCLE_STATES`를 명시적으로
먼저 검사해 항상 예외를 던진다 — 표가 우연히 비어 있는 것과, "터미널이라 전이 자체가
금지"라는 의미를 구분하기 위함이다.

## Pending action (설계 해석)

섹션 6의 `Run` 도메인 모델 표에는 `pending_action` 필드가 없고, 섹션 10의 원자적 전이
pseudocode는 "INSERT/UPDATE pending action or failure"를 별도 행으로 다룬다. 따라서
`PendingAction`을 `Run`에 새 필드로 추가하지 않고, `transition_run()`이 반환하는
`TransitionOutcome`의 곁가지 값으로 모델링했다. `AWAITING_APPROVAL`,
`AWAITING_MANUAL_REVIEW`, `AWAITING_FINAL_APPROVAL`, `RECOVERY_REQUIRED`로 들어갈 때는
`PendingAction`이 필수이고, 그 외 상태로 갈 때 `PendingAction`을 넘기면 거부된다
(`WorkerResult.blocked_reason` 검증과 같은 패턴).

## FailureRecord 연결

`FAILED`로의 모든 전이는 `FailureRecord`를 요구하고, `FAILED`가 아닌 target에 `FailureRecord`
를 넘기면 거부된다 — "상태와 실패 원인의 분리" 원칙(`FAILED` 상태 자체는 원인을 모르고,
원인은 항상 별도 레코드로 붙는다)을 타입 수준에서 강제한다.

## 순수 함수, side effect 없음

`transition_run(run, target, ...)`은 `run`을 in-place로 바꾸지 않고 `run.model_copy(update=...)`
로 새 `Run`을 반환한다. I/O나 영속화는 전혀 하지 않는다 — 그건 Phase 2.1
(`persistence/sqlite.py::apply_transition`)의 책임이고, 실제로 그 함수는 이 순수 커널을
그대로 재사용한다(같은 검증 로직을 두 번 구현하지 않기 위해).
