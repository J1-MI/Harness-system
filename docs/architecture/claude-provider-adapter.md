# Claude Provider Adapter — Phase 6

`providers/claude.py`의 설계 결정. 로드맵 범위: "Python Agent SDK, strict settings, session
resume, structured result, explicit tools, keyless replay tests". Skill/MCP 외부 연결과
production network 사용은 비범위. Handoff 프롬프트 없이 로드맵 행 + 섹션 7의 Claude 관련
서술을 스펙으로 설계했다.

## 어떤 SDK인가 — Messages API가 아니라 Agent SDK

`pip install claude-agent-sdk`(패키지 설명: "Python SDK for Claude Code")를 썼다. 이건
`anthropic` 패키지(Messages API, 단일 completion 호출)와 다르다. 리뷰가 "Claude: Python Agent
SDK 우선"이라고 명시한 이유— Worker는 세션·도구·권한 관리가 필요한 자율 코딩 에이전트지, 한 번의
LLM 호출이 아니다. `claude-api` 스킬이 자동으로 로드하는 문서는 base Messages API SDK 기준이라
그대로 베끼면 틀렸을 것 — `ClaudeSDKClient`, `ClaudeAgentOptions`, 메시지 dataclass들의 실제
필드는 설치된 패키지를 직접 `inspect`해서 확인했다("never guess SDK usage" 원칙).

## Strict 설정 3종 세트

- `setting_sources=[]` — 프로젝트 `CLAUDE.md`, hook, user/project 설정을 전혀 자동으로 읽지
  않는다. 리뷰가 말한 "--bare가 자동 hooks, skills, plugins, MCP, CLAUDE.md 로드를 차단하는
  권장 방식"의 SDK-레벨 대응.
- `mcp_servers={}` + `strict_mcp_config=True` — 저장소 자체의 `.mcp.json`에 연결하지 않는다
  (MCP governance는 Phase 11).
- `permission_mode="dontAsk"` — 승인 대기로 멈추지 않고, 미리 허용된 것만 자동 진행한다.

## 도구 통제는 3중이다 (리뷰의 정정 사항을 그대로 반영)

리뷰 섹션 7: "Claude의 `allowed_tools`는 도구 가시성 제한이 아니라 자동 승인 규칙이다. 엄격한
프로필은 `tools`/bare-name deny로 가시성을 줄이고 `dontAsk`를 함께 사용해야 한다." 그래서 세
층을 각각 다른 걸 담당하게 했다:

1. `tools=list(request.allowed_tool_ids)` — 가시성. 여기 없는 도구는 모델에게 존재조차
   보이지 않는다.
2. `allowed_tools=list(request.allowed_tool_ids)` + `permission_mode="dontAsk"` — 자동 승인.
   보이는 도구 중 이 목록에 있는 것만 프롬프트 없이 실행된다.
3. `can_use_tool` 콜백 — **런타임 재검증**. `AgentRunRequest.allowed_tool_ids`와 다시 대조해서
   1번·2번이 어떤 이유로든 잘못 설정돼도 마지막에 한 번 더 막는다. 실제로 라이브 스모크
   테스트에서 `options.can_use_tool("write_file", ...)`이 `allowed_tool_ids=["bash"]`일 때
   `PermissionResultDeny`를 반환하는 걸 직접 호출해서 확인했다.

`can_use_tool`을 설정하면 SDK가 **streaming 모드(AsyncIterable 프롬프트)를 강제**한다는 걸
실제 라이브 호출에서 처음 발견했다 — 문자열 프롬프트를 바로 넘기면
`ValueError: can_use_tool callback requires streaming mode`. `_single_user_message_stream()`
이 `query()`가 문자열 프롬프트에 내부적으로 하는 것과 똑같은 변환
(`{"type": "user", "message": {"role": "user", "content": prompt}, ...}`)을 미리 해서
async generator로 감싼다. 이건 keyless 테스트로는 절대 못 잡는 종류의 버그였다 — Fake
클라이언트는 SDK의 내부 유효성 검사를 재현하지 않기 때문에, 실제 API 호출을 한 번 해보고서야
드러났다.

## Session resume / fork

`start_session`은 새 opaque 세션과 새 Claude `session_id`(UUID)를 만든다.
`resume_session`은 이전 세션의 `claude_session_id`를 `ClaudeAgentOptions.resume`에 넣은 새
opaque 세션을 만든다 — 실제 SDK 프로세스 연결(`ClaudeSDKClient`)은 `start_invocation`에서
처음 필요할 때(lazy)만 만든다. 같은 세션에서 두 번째 이후 턴은 새로 `connect()`하지 않고
`query()`만 호출해서 SDK 프로세스를 재사용한다.

## Structured result

`ResultMessage.structured_output`을 `AgentRunResult.structured_output`에 그대로 매핑하되,
dict가 아니면(예: 파싱 실패로 다른 타입이 온 경우) `None`으로 떨어뜨린다. `stop_reason`
문자열에 "schema"/"invalid_output" 계열 키워드가 있으면 `ProtocolStatus.PROVIDER_ERROR`가
아니라 `ProtocolStatus.INVALID_OUTPUT`으로 분류한다 — "schema invalid" 테스트 기준이 요구하는
구분이다.

## Cancel

`cancel()`은 `sdk_client.interrupt()`를 호출한 뒤 드레인이 끝나기를 기다리고, **최종
`AgentRunResult.protocol_status`를 무조건 `CANCELLED`로 덮어쓴다** — SDK가 무슨 이유를
`ResultMessage`에 담아 보내든(성공처럼 보이는 결과라도) 취소 경로를 거쳤다면 그 사실이
우선한다.

## Keyless replay 테스트 vs 라이브 스모크

기본 테스트 스위트(`tests/unit/test_claude_adapter.py`)는 `FakeSdkClient`에 **실제 SDK
dataclass 인스턴스**(`SystemMessage`, `AssistantMessage`, `ResultMessage` 등, 우리가 만든
가짜 shape이 아니라 설치된 패키지가 내보내는 진짜 타입)를 주입해서 정규화 로직을 검증한다 —
API 키도 네트워크도 필요 없다. `test_live_smoke_against_real_api`는 `RUN_LIVE_CLAUDE_SMOKE=1`
을 명시적으로 설정해야만 실행되는 opt-in 테스트다(실제 과금 발생).

**실제로 한 번 라이브 실행해서 검증했다**: 실제 `claude` CLI 바이너리를 통해 진짜 세션이
연결되고, 실제 `SESSION_STARTED` 이벤트(설치된 skill/agent 목록 등 진짜 capability 데이터
포함)가 정확히 정규화되는 것을 확인했다. 이번 실행에서는 해당 API 키의 계정에 크레딧 잔액이
없어(`billing_error`, "Credit balance is too low") 실제 완료까지는 못 갔지만, 어댑터는 이
실패를 정확히 감지하고 `ProtocolStatus.PROVIDER_ERROR` + 원본 메시지를 보존한 `ProviderError`
로 정확히 분류했다 — 즉 오류 자체가 이 코드의 버그가 아니라 계정 크레딧 문제였고, 그 구분을
어댑터가 정확히 해냈다는 뜻이다.

## 비범위

MCP/Skill 외부 연결(strict 설정으로 명시적으로 차단), production 네트워크에서의 무제한 사용,
`Provider Host` 프로세스 분리(현재는 harness 프로세스 안에서 SDK를 직접 호출 — Provider
자격증명과 워크스페이스 명령 실행 프로세스를 분리하는 것은 섹션 8이 권고하는 다음 단계),
`Provider credential`을 명시적으로 scrub된 `Workspace Tool Broker` 환경으로 넘기는 것(현재는
Phase 3.2의 `CommandBroker`가 이미 자체적으로 env allowlist를 강제하므로 `ANTHROPIC_API_KEY`
가 거기 들어갈 일이 없다는 사실에 구조적으로 의존하고 있을 뿐, 별도 Provider Host 프로세스
분리는 하지 않았다), Codex Planner/Verifier adapter(Phase 7, 8).
