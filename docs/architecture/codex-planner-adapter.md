# Codex Planner Adapter — Phase 7

`providers/codex.py`의 설계 결정. 로드맵 범위: "read-only RoleProfile, fresh plan session,
structured TaskContract". Verifier 연동(Phase 8용 별도 fresh session 원칙 적용)은 다음 Phase
몫이지만, 이 어댑터 자체는 PLANNER/VERIFIER 둘 다 처리할 수 있게 만들었다 — 리뷰의 M-02
원칙(역할은 데이터지 별도 Protocol이 아니다)을 여기서도 지켰다. Handoff 프롬프트 없이 로드맵
행 + 섹션 7의 Codex 서술을 스펙으로 설계했다.

## 어떤 SDK인가

`pip install openai-codex`("Python SDK for Codex", 로컬 app-server를 JSON-RPC로 제어). 실제
설치된 0.144.4 패키지를 직접 `inspect`해서 `AsyncCodex`/`AsyncThread`/`AsyncTurnHandle`의 진짜
시그니처를 확인했다 — Claude adapter와 같은 원칙("never guess SDK usage").

## no-write test: read-only + deny-all

모든 스레드를 `sandbox=Sandbox.read_only`, `approval_mode=ApprovalMode.deny_all`로 시작한다.
Planner는 분석하고 `TaskContract`를 제안할 뿐 코드를 수정할 필요가 없으므로, 애초에 쓰기
권한을 요청하지 않는다 — 승인이 필요한 도구 호출이 시도되더라도 자동으로 거부된다.
`test_successful_turn_is_parsed_and_thread_is_read_only_deny_all`이 매 `thread_start` 호출에
이 두 값이 실제로 전달되는지 직접 확인한다.

## Structured TaskContract: Phase 1.1 스키마 커널을 그대로 재사용

`Thread.turn(output_schema=...)`에 Codex 네이티브 구조화 출력 기능이 있다. Phase 1.1이 이미
만들어 둔 `schemas/generated/*.json`과 그 원천인 Pydantic 모델(`schema_export.EXPORTED_MODELS`)
을 그대로 가져다 썼다: `output_schema_id`가 `task_contract`/`verification_result` 등 우리가
아는 이름이면 `model_json_schema()`를 Codex에 넘겨 출력을 유도하고, 응답이 오면 **같은 Pydantic
모델로 다시 검증**한다(`model_validate_json`). 별도 JSON Schema validator를 새로 만들지 않았다
— 이건 "스키마와 검증기를 이중 관리하면 drift가 생긴다"는 Phase 1.1의 원칙을 그대로 이어받은
것이다.

## Invalid schema retry limit

Codex가 `output_schema`의 안내를 받아도 실제로 그 모양을 지킨다는 보장은 없다(모델 자체의
불확실성). 그래서 마지막 메시지가 우리 Pydantic 모델 검증에 실패하면, **같은 스레드에서 같은
입력으로 새 turn을 재시도**한다. `max_schema_retries`(기본 2)를 넘기면 포기하고
`ProtocolStatus.INVALID_OUTPUT`으로 확정한다. 재시도가 일어날 때마다 `AgentEventType.WARNING`
이벤트를 스트림에 남겨서, 소비자가 "몇 번째 시도인지"를 볼 수 있게 했다. 테스트가 성공 케이스
(1번 실패 후 2번째에 성공)와 소진 케이스(설정한 상한만큼 정확히 재시도하고 포기)를 각각
증명한다.

## 실제 라이브 호출에서 두 번째로 잡은 진짜 버그

이번에도 keyless 테스트로는 못 잡는 버그를 라이브 호출로 잡았다: `AsyncThread.turn()`은
`inspect.signature`로는 `-> AsyncTurnHandle`처럼 보이지만, 실제로는 `async def`라서 반환값이
코루틴이다 — `await` 없이 호출하면 `turn_handle.stream()` 호출 시점에
`'coroutine' object has no attribute 'stream'`이 난다. `inspect.signature`는 async 함수의
반환 타입 애노테이션만 보여주고 코루틴으로 감싸진다는 사실 자체는 알려주지 않는다는 걸 이번에
체감했다. `await session_state.thread.turn(...)`으로 고쳤고, 이후 실제 API로 "pong"을 정확히
받아 `ProtocolStatus.SUCCEEDED`까지 확인했다.

또한 처음엔 `Turn` 객체에 `.usage` 필드가 있다고 가정했다가(`TurnResult`와 헷갈림) 실제로는
없다는 걸 알게 됐다 — usage는 별도 `ThreadTokenUsageUpdatedNotification` 이벤트로 스트림에
따로 온다. 스트림을 드레인하면서 이 알림을 별도로 추적해 최종 `AgentRunResult.usage`를
채운다.

## Cancel과 "fresh plan session"

`cancel()`은 `turn_handle.interrupt()`를 호출한 뒤 최종 상태를 무조건 `CANCELLED`로 확정한다
(Claude adapter와 동일 패턴). `state.active_turn`은 `start_invocation()` 안에서 **동기적으로**
(백그라운드 태스크가 스케줄되기 전에) 설정한다 — 그렇지 않으면 `start_invocation` 직후 바로
`cancel()`을 호출하는 경우 아직 아무 turn도 시작되지 않은 것으로 오인해 interrupt가 씹히는
race가 있었다(테스트로 잡음).

`start_session`은 기본적으로 새 스레드를 뜻하고(`thread_start`), `resume_session`은
`thread_resume`을 쓴다. 리뷰가 말한 "fresh plan session"은 이 어댑터가 강제하는 게 아니라
호출자(orchestration, 아직 없음)가 Planner 실행마다 새 세션을 여는 관례로 지켜야 하는
것으로 문서화해 둔다 — 어댑터 자체는 resume도 정상 지원한다.

## 비범위

Verifier 전용 "evidence-only context" 격리(Phase 8), Codex의 MCP/skill 연동, 실제 파일 시스템
접근이 필요한 workspace_write/full_access sandbox 사용, 실시간 라이브 스모크 테스트를 커밋된
테스트 스위트에 포함하는 것(로드맵이 Phase 7에는 요구하지 않아 생략했다 — 대신 실제 API로 한
번 수동 검증만 하고 그 과정에서 위 두 버그를 잡았다).
