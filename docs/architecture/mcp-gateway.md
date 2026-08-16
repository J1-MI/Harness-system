# MCP Governance — Phase 11

`execution/mcp_gateway.py` + `domain.models.McpToolSpec`. 로드맵 범위: "registry, strict config,
tool policy, credential broker, audit", 비범위: "범용 marketplace 자동 연결". target: "MCP
gateway". 테스트 기준: "unauthorized tool/server/side effect 차단" — 13개 테스트가 이 기준을
직접 겨냥한다. 스펙 출처는 "## MCP의 위치" 섹션과 공격 표면 표의 5번 행이다.

**이 Phase는 MVP(Phase 10) 완료 이후, 로드맵이 명시적으로 "post-MVP"라고 부르는 범위다** —
architecture review 섹션 11 직후: "Phase 11 이후에야 MCP 사용을 지원한다고 주장할 수 있다."

## "Provider가 `.mcp.json`에 직접 연결해서는 안 된다" — 이 모듈은 파일을 아예 안 읽는다

리뷰 원문을 코드가 아니라 **코드의 부재**로 지킨다: `mcp_gateway.py`에는 `.mcp.json`이나 어떤
MCP 설정 파일을 읽는 코드가 단 한 줄도 없다. `McpToolCatalog`에 tool이 등록되는 유일한 경로는
`register()`를 직접 호출하는 것뿐이다(`execution.command_broker.CommandCatalog`와 동일한
전례). `test_gateway_never_reads_a_planted_mcp_json`이 이를 직접 증명한다 — 진짜처럼 생긴
`.mcp.json`을 심어놓고, 그걸 읽었다면 등록됐을 tool ID로 조회하면 `UnauthorizedMcpToolError`가
난다는 걸 확인한다.

## 권한의 유일한 원천은 여전히 Phase 4의 `PolicyGrants.mcp_tools`

`invoke_mcp_tool`의 검사 순서: (1) catalog에 등록돼 있는가 (2) 호출 역할이
`spec.allowed_roles`에 있는가 (3) **`grants.mcp_tools`에 실제로 있는가**. 세 번째가 핵심이다
— catalog에 등록돼 있고 역할도 맞아도, Phase 4의 `evaluate_policy`가 실제로 승인하지 않은
tool은 절대 호출할 수 없다. `test_tool_not_in_policy_grants_is_rejected`가 이 구분을 정확히
테스트한다: 등록은 돼 있지만 grants에 없는 경우를 별도로 확인한다. "registry에 있다"와
"승인됐다"를 섞으면 안 된다는 게 이 설계의 핵심 불변조건이다.

## `readOnlyHint`는 신뢰 경계가 아니다 — `classification`은 Harness 자신의 판단

리뷰: "MCP의 `readOnlyHint`나 destructive annotation은 참고 정보일 뿐 신뢰 경계가 아니다."
`McpToolSpec.classification`은 MCP 서버가 스스로 보고하는 어떤 값도 읽어서 채우지 않는다 —
admin이 `register()` 시점에 직접 지정하는 필드다. `requires_approval=True`인 tool은
Phase 4/Approval 엔진과 완전히 같은 방식으로 바인딩된 `Approval`(subject_type=`MCP_TOOL`,
subject_digest=이번에 등록된 `McpToolSpec`의 정확한 digest, decision=`APPROVED`, 만료 전)이
없으면 거부된다 — subject가 다른 tool을 가리키는 승인, 만료된 승인 둘 다 각각 테스트로
확인했다(`test_destructive_tool_with_wrong_subject_approval_is_rejected`,
`test_destructive_tool_with_expired_approval_is_rejected`).

## Credential broker: resolve는 호출 시점에만, 절대 로그에 안 남는다

`McpCredentialBroker.resolve(server_id)`는 실제 `transport` 호출 바로 직전에만 불린다. 반환된
값은 `transport`에 직접 전달될 뿐, 이 모듈의 어떤 audit record에도 절대 들어가지 않는다 —
Command Broker의 env allowlist가 provider credential을 child process 환경에 안 넣는 것과
같은 원칙("provider credential이 child process 환경에 없음"). `test_credential_never_appears_
in_audit_evidence`가 실제로 저장된 audit blob을 읽어서 비밀값 문자열이 어디에도 없음을
확인한다.

## 모든 호출이 audit evidence를 남긴다 — 성공이든 거부든

`invoke_mcp_tool`은 성공 시 `EvidenceRecord`(kind=`mcp_tool_call_succeeded`,
trust_tier=`EXTERNAL_MCP_REPORTED`)를 반환값에 담고, **거부돼도** 마찬가지로
`EvidenceRecord`(kind=`mcp_tool_call_rejected`)를 만든다 — 다만 예외가 발생하는 경로라
정상적인 반환값으로 전달할 수 없으므로, `McpGatewayError`에 `.evidence` 속성으로 붙여서
raise한다. 그래서 거부된 호출도 호출자가 원하면 그 audit record를 여전히 꺼내서 저장할 수
있다. 두 경로 모두 Phase 2.2의 `write_blob`(secret redaction 포함)을 그대로 재사용한다 — 새
저장 메커니즘을 만들지 않았다.

## 나머지 통제: input schema, rate limit, result size

- **input schema**: 전체 JSON Schema 검증기가 아니라 `required` 키 존재 + 선언된 top-level
  property 타입 체크만 하는 의도적으로 부분적인 검증기다(`_validate_input_schema`). 새
  의존성(`jsonschema` 등)을 추가하지 않기 위한 선택이며, 이 경계에서 필요한 최소한의 안전성은
  충족한다 — 더 엄격한 검증이 필요해지면 나중에 교체 가능하도록 함수 하나로 분리해뒀다.
- **rate limit**: `McpRateLimiter`는 순수 in-memory sliding window이고, 전역 상태가 아니라
  호출자가 소유해서 주입하는 인스턴스다(이 코드베이스의 다른 모든 mutable state와 동일한
  패턴).
- **result size**: 새 캡 로직을 만들지 않고 Phase 2.2 `write_blob`의 기존 `max_size_bytes` 인자
  + `ArtifactQuotaExceededError`를 그대로 재사용해서 `McpResultTooLargeError`로 감싼다 — "조용히
  잘라내고 PASS해서는 안 된다" 원칙이 이미 `write_blob`에 있으므로 재구현하지 않았다.

## 비범위

실제 MCP 서버 전송(stdio/SSE/HTTP)은 `transport: Callable`로 주입만 받고 이 phase에서 구현하지
않는다 — 이 코드베이스는 여전히 실제 MCP 클라이언트 SDK를 갖고 있지 않다. Claude/Codex 어댑터
(Phase 6/7)를 이 게이트웨이에 실제로 연결하는 작업(`ClaudeAgentOptions.mcp_servers` 등)도
이번 phase엔 없다 — 로드맵의 target이 "MCP gateway" 자체였지 "Dual-Agent Pipeline에 MCP
와이어링"이 아니었고, Phase 9 로드맵 행도 MCP를 명시적으로 비범위 처리했다. 범용 MCP
marketplace 자동 연결, 여러 서버에 걸친 tool discovery도 비범위.
