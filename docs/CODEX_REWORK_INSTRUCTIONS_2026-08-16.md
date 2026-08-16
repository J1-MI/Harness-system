# Claude Code 재작업 지시서 — 독립 검증 2차

이 문서는 현재 저장소와 `docs/WORK_LOG_2026-08-15_codex_rework_response.md`를 Codex가 독립적으로 다시 검토한 뒤 작성한 재작업 계약이다. Claude Code는 이 문서만으로 작업 범위, 우선순위, 완료 조건, 반환 증거를 이해해야 한다.

## 1. 역할과 목표

당신은 이 저장소의 구현 담당자다. 기존 작업 로그의 자기평가나 테스트 개수 자체를 성공 근거로 사용하지 말고, 실제 코드·DB 레코드·worktree 상태·명령 종료 코드로 아래 요구사항을 입증하라.

현재 판정은 **REWORK**다. 기존 수정은 의미 있는 진전이지만 다음 두 문제가 아직 병합 차단 요소다.

1. 결정적인 scope/test 실패가 `MANUAL_REVIEW`와 일반 CLI 승인을 거쳐 `READY_FOR_MERGE`로 승격될 수 있다.
2. Verifier가 반환한 `task_id`, contract/snapshot/evidence digest, `invocation_id`가 현재 Run의 실제 객체와 일치하는지 하네스가 검증하지 않는다.

목표는 이 우회 경로를 제거하고, 독립 테스트와 증거가 실제 Run에 암호학적으로 결합되며, 재시작 후에도 판정 근거를 복구할 수 있도록 만드는 것이다.

## 2. 작업 전 준수사항

- 먼저 `git status --short`를 확인하라. 현재 저장소의 기존 staged/uncommitted 변경은 사용자 소유이므로 reset, checkout, clean, stash 또는 임의 삭제하지 마라.
- 이 지시서 자체는 수정하지 마라.
- commit, push, merge, deploy를 수행하지 마라.
- 실제 Codex/Claude API 키나 네트워크가 없어도 모든 필수 테스트가 실행되어야 한다.
- 외부 패키지는 새로 설치하지 마라. 정말 필요하면 구현을 중단하고 이유와 대안을 보고하라.
- 설치된 Skill System이 있으면 `skill-system-dev:analysis-codebase`, `skill-system-dev:workflow-bug-fix`, `skill-system-quality:workflow-validation`을 선택적으로 활용할 수 있다. Skill의 존재를 제품 코드, 테스트, 설치 절차의 전제로 삼지 마라.
- 요구사항이 불명확하면 권한이나 범위를 넓히지 말고 fail-closed 기본값을 택한 뒤 작업 로그에 가정을 기록하라.

## 3. 절대 불변 조건

1. Provider가 반환한 자연어 주장과 자기 보고 식별자·digest는 신뢰 데이터가 아니다.
2. scope 위반, 필수 명령 미실행, 예상하지 않은 종료 코드, timeout, output cap 초과, 테스트에 의한 금지된 파일 변경은 일반 승인으로 PASS 처리할 수 없다.
3. 예외 허용은 대상 criterion/finding, 실제 subject digest, 허용 범위, 만료 및 사용 여부에 결합된 명시적 Approval로만 가능하다.
4. worktree 밖 경로와 reparse point/junction을 통한 우회는 Windows와 POSIX 모두에서 차단해야 한다.
5. 최종 판정은 실제 TaskContract, Worker 결과 snapshot, host evidence set, 실제 Verifier invocation에 결합되어야 한다.
6. 최종 승인과 검증 실패 면제는 서로 다른 행위다. 최종 승인은 실패한 검증을 면제하지 않는다.
7. 하네스가 재구성할 수 있다고 주장하는 데이터는 먼저 내구성 있게 저장되어 있어야 한다.

## 4. 필수 재작업 범위

아래 순서대로 구현하라. 선행 항목의 테스트가 통과하기 전 다음 항목으로 넘어가지 마라.

### R2-01 — 결정적 검증 실패를 비우회성 hard gate로 변경

현재 `application/verification.py`는 Verifier가 PASS를 주장했지만 결정적 위반이 있으면 `accepted_decision=MANUAL_REVIEW`로 내린다. `interfaces/cli.py`는 `AWAITING_MANUAL_REVIEW -> AWAITING_FINAL_APPROVAL -> READY_FOR_MERGE`를 일반 승인으로 허용한다. 이 조합을 제거하라.

필수 요구사항:

- 검증 결과에 최소한 다음 두 범주를 구분하라.
  - `hard_gate_violations`: 하네스가 직접 관찰했고 일반 승인으로 우회할 수 없는 실패
  - `review_findings`: 증거 불충분, 비필수 criterion, 모델 판단 불확실성처럼 실제 수동 판단이 가능한 항목
- 다음은 항상 hard gate다.
  - effective scope 밖 변경 또는 forbidden path 변경
  - mandatory COMMAND criterion의 명령 미등록·미실행
  - timeout 또는 output cap 초과
  - 종료 코드가 명시된 예상 집합에 없음
  - 테스트 전후 manifest에 허용되지 않은 side effect가 있음
  - 필수 host evidence 누락 또는 digest/subject 불일치
- hard gate가 하나라도 있으면 일반 `MANUAL_REVIEW`/최종 승인 경로로 보내지 마라.
- 수정 가능한 구현·테스트 실패는 `REWORK`와 구체적인 host-generated required fix로, 보안 위반·계약 위조·허용 불가능한 상태는 `REJECT` 또는 실패 종료로 보낸다. 분류 규칙은 enum/typed 함수로 만들고 테스트하라.
- `AWAITING_MANUAL_REVIEW` 승인은 검증 실패 면제가 아니다. generic approve가 hard gate를 제거하거나 `READY_FOR_MERGE`로 승격시키지 못하게 하라.
- Verifier의 `required_fixes`가 비어 있어도 host hard gate에서 생성한 재작업 사유가 ReworkContract에 들어가야 한다.

필수 부정 테스트:

- scope 위반 + Verifier PASS + generic approve를 조합해도 `READY_FOR_MERGE`에 도달하지 않는다.
- exit code 1 + Verifier PASS + generic approve를 조합해도 `READY_FOR_MERGE`에 도달하지 않는다.
- timeout, output cap, 명령 미실행, test side effect 각각에 같은 성질을 검증한다.
- 순수한 비필수·판단형 finding만 manual review로 갈 수 있음을 검증한다.

### R2-02 — VerificationResult의 실제 Run 바인딩

현재 Pydantic schema가 digest 형식만 검사하며 반환값을 현재 Run의 실제 값과 비교하지 않는다. 모델이 넣은 식별자를 신뢰하지 마라.

권장 설계:

- Provider 구조화 출력은 모델이 판단할 필드만 담는 `VerifierDecisionPayload` 같은 별도 DTO로 축소하라.
- 다음 host-owned binding 필드는 Provider payload에서 받지 말고 하네스가 실제 상태로 구성하라: `task_id`, `contract_digest`, `result_snapshot_digest`, `evidence_set_digest`, `invocation_id`.
- 외부 schema 호환 때문에 Provider가 이 필드를 계속 반환해야 한다면 값이 정확히 일치하는지 비교하고, 하나라도 다르면 `SCHEMA_INVALID` 또는 계약 위조 성격의 `REJECT`로 종료하라. 덮어써서 숨기지 마라.
- `result_snapshot_digest`는 WorkerResult 문자열만 해시하지 말고 frozen result manifest/diff와 worker result의 canonical envelope 결합 규칙을 문서화하라.
- `evidence_set_digest`는 정렬된 EvidenceRecord 식별자와 content/subject digest의 canonical 표현으로 계산하라. 입력 순서에 따라 값이 달라지지 않아야 한다.
- `invocation_id`는 실제 persistence에 기록한 Verifier invocation ID여야 한다.
- `run_verification`은 trusted binding을 검증 또는 조립하는 데 필요한 명시적 입력을 받아야 한다.

필수 부정 테스트:

- 다른 task ID
- 다른 contract digest
- 다른 result snapshot digest
- 다른 evidence set digest
- 존재하지 않거나 다른 invocation ID
- evidence 순서만 바뀐 정상 입력

첫 다섯 경우는 fail-closed, 마지막 경우는 동일 digest가 되어야 한다.

### R2-03 — criterion 검증 규칙과 host evidence 완성

현재 `expected_exit_codes`가 빈 목록이면 어떤 비정상 종료도 허용된다. `expected_output_match`와 `required_evidence_kinds`는 실질적으로 검증되지 않으며 command evidence는 stdout/stderr만 만든다.

필수 요구사항:

- `VerificationMethod.COMMAND`인 mandatory criterion은 `command_id`와 비어 있지 않은 `expected_exit_codes`를 요구하도록 model validation을 강화하라.
- `expected_output_match`의 문법을 하나로 명시하라. MVP 권장값은 literal substring 또는 별도 enum이 붙은 match spec이다. 임의 regex라면 ReDoS 방지 timeout/길이 제한을 구현하라.
- `required_evidence_kinds`를 실제 frozen evidence set과 대조하라. kind 존재 여부만 보지 말고 run_id, task_id, subject, trust tier, artifact integrity까지 검증하라.
- 각 host command 실행에 대해 stdout/stderr와 별도로 canonical command-result Artifact/Evidence를 생성하라. 최소 필드는 registered command spec ID 및 digest, duration, exit code, timed_out, output_cap_exceeded, stdout/stderr refs 및 digests, sandbox profile이다.
- 데모의 `required_evidence_kinds=["command_exit_code"]`가 실제 EvidenceRecord로 충족되거나, 새로운 표준 kind로 schema·데모·테스트를 일관되게 변경하라.
- Worker가 보고한 `reported_tests`는 참고 자료일 뿐 host evidence를 대체하지 못하게 하라.

### R2-04 — 증거, invocation, snapshot 및 최종 판정 영속화

현재 orchestrator 정상 경로는 `insert_verification_result`만 호출하고, 제공된 `insert_artifact`, `insert_evidence_record`, `insert_invocation`, `insert_context_snapshot`을 사용하지 않는다. “나중에 재구성 가능”이라는 설명은 실제 persistence 없이는 성립하지 않는다.

필수 요구사항:

- 정상 pipeline에서 다음을 실제 저장하라.
  - Planner/Worker/Verifier AgentSession 및 AgentInvocation 또는 현재 schema의 동등 객체
  - prompt/context snapshot과 digest
  - baseline/result/post-test manifest Artifact metadata
  - command stdout/stderr/result Artifact metadata
  - 모든 EvidenceRecord
  - model-claimed VerificationResult
  - host-accepted decision, hard gate violations, review findings, 적용된 waiver refs
  - 단계별 BudgetUsage
- DB 행과 blob 저장의 부분 성공을 방치하지 마라. SQLite transaction과 content-addressed artifact finalization 순서를 명시하고 복구 가능하게 하라.
- 재시작 시 최종 판정 근거를 DB+Artifact Store만으로 재검증할 수 있어야 한다. 메모리의 `FrozenValidationResult`가 유일한 근거이면 안 된다.
- persistence API를 application port 뒤에 두거나 최소한 repository/unit-of-work 경계를 만들어 orchestrator가 구체 SQLite 함수에 더 결합되지 않게 하라. ORM은 도입하지 마라.
- content-addressed blob 삭제 시 Artifact뿐 아니라 Evidence와 다른 참조도 고려하여 공유 blob을 조기 삭제하지 않게 하라.

필수 통합 테스트:

- Fake Provider + 실제 임시 Git repo + 실제 등록 host command로 전체 pipeline을 실행한다.
- 종료 후 새 DB connection에서 invocation, snapshots, artifacts, evidence, accepted verification을 읽는다.
- 모든 artifact 파일이 존재하고 DB digest와 일치함을 검사한다.
- 메모리 객체 없이도 report가 동일한 결론과 근거를 제시하는지 검사한다.

### R2-05 — timeout과 예산을 전체 invocation 생명주기에 적용

현재 wall-clock 제한은 `await_result()`에만 적용되며 `start_session`, `start_invocation`, `cancel`, `close_session`은 무제한 대기할 수 있다. rework 사용량과 실제 invocation 수 회계도 완전하지 않다.

필수 요구사항:

- 하나의 절대 deadline을 `start_session -> start_invocation -> await_result -> cancel/close` 전체에 적용하라.
- 각 단계는 남은 시간을 사용하고 cleanup에는 짧고 제한된 grace timeout을 둔다. cleanup timeout 때문에 Run 종료가 무한 대기하면 안 된다.
- timeout 시 child process 정리 결과와 cancel/close 결과를 journal에 남기되 비밀값은 기록하지 마라.
- Provider가 usage를 반환하지 않아도 실제 시작된 invocation/turn은 최소 1회로 회계하라.
- rework loop 진입 시 `rework_used`를 정확히 증가시키고 내구성 있게 저장하라.
- 유효 예산은 `TaskContract.request <= PolicyDecision.grants <= configured ceiling`의 교집합이어야 한다. contract 요청값만 `check_rework_budget`에 사용하지 마라.
- 각 Provider 호출 전후 누적 예산을 검사하고 재시작 후에도 같은 누적값으로 계속한다.

필수 테스트:

- `start_session`, `start_invocation`, `await_result`, `cancel`, `close_session` 각각이 hang하는 Fake Provider
- usage 미보고 Provider
- policy ceiling보다 큰 contract rework 요청
- 재시작 전후 누적 예산 초과

### R2-06 — Windows junction/reparse 및 path 정책 강화

현재 directory reparse point를 manifest에 기록하지만 `os.walk`의 `dirnames`에서 제거하지 않는다. path glob 비교가 Windows의 대소문자 비구분 의미와 다를 수 있고, path-bearing tool이 예상 path key를 누락하면 허용된다.

필수 요구사항:

- directory symlink/junction/reparse point를 `lstat`으로 기록한 즉시 `dirnames`에서 제거하여 절대 하위로 순회하지 마라.
- worktree containment은 최종 `Path.resolve()` 한 번만 믿지 말고 쓰기 대상의 기존 상위 component에 reparse point가 있는지 검사하라. 검사와 사용 사이 교체 위험을 줄이는 방식을 문서화하라.
- Windows에서는 path matching을 case-insensitive로, POSIX에서는 case-sensitive로 유지하라. 동일 규칙을 Claude tool guard, scope guard, manifest scope validation에 공통 적용하라.
- Windows 예약 장치명(`CON`, `NUL`, `COM1` 등), trailing dot/space, alternate data stream 문법 등 식별자·경로 위험을 명시적으로 거부하라.
- path-bearing tool이 필수 path key를 누락하거나 타입이 잘못되면 allow가 아니라 deny하라.
- 실제 Windows junction 테스트를 추가하라. Windows에서 symlink 권한 부족을 이유로 junction 테스트까지 skip하지 마라. POSIX에서는 directory symlink로 동등 성질을 검증하라.

### R2-07 — 기준 revision 고정과 TOCTOU 검사 연결

`repository fingerprint`는 저장소 정체성 검사와 target ref 이동 검사를 혼동하면 안 된다. standalone `check_base_revision_stale`만 존재하고 pipeline에 연결되지 않은 상태를 완료로 주장하지 마라.

필수 요구사항:

- 계약 승인 시점의 `base_commit_sha`를 worktree 생성의 유일한 기준으로 사용하라.
- `target_ref`의 resolved SHA를 계획 검증/승인 시 고정하고, 최소 worktree 생성 직전과 최종 `READY_FOR_MERGE` 직전에 다시 검사하라.
- target ref가 이동하면 새 revision을 자동 수용하지 말고 `BASE_REVISION_STALE` 또는 명시적 재계획/재승인 상태로 종료하라.
- repository identity fingerprint와 target revision pin을 서로 다른 필드·검사로 유지하라.
- 원본 working directory의 dirty 여부는 commit SHA에 포함되지 않는다. dirty repo 정책을 명시하고 기본값은 계획 입력으로 사용하지 않거나 명시 승인 없이 거부하는 것이다.

### R2-08 — 의미 있는 keyless E2E와 정직한 live 테스트

현재 opt-in live E2E는 빈 `CommandCatalog`, 빈 `check_command_ids`를 사용하면서 Evidence DB row가 있다고 주장한다. 이 구성으로 정상적인 evidence-backed PASS는 만들 수 없다.

필수 요구사항:

- 필수 CI E2E는 외부 키 없는 Fake/Replay Provider를 사용하되 임시 Git repo와 고정 base SHA, 독립 worktree, 허용 경로 변경, 등록된 host command subprocess, pre/post-test manifest, Artifact/Evidence persistence, 별도 Verifier session, 최종 승인 전·후 상태를 실제로 통과해야 한다.
- E2E verifier는 존재하는 Evidence ID만 인용해야 하며 존재하지 않는 ID나 임의 digest를 반환하면 실패해야 한다.
- actual adapter live test는 opt-in으로 유지해도 되지만 빈 test catalog로 성공을 주장하지 마라. 필요한 command를 등록하거나 adapter smoke test와 pipeline E2E를 분리하라.
- live 인프라/키 skip과 Windows 권한 skip을 최종 보고에서 구분하라.

### R2-09 — 문서와 완료 주장 정정

- `docs/CODEX_REVIEW_RESPONSE.md`의 “6/7 blockers 해결” 같은 주장을 실제 완료 상태와 일치시키라.
- `docs/IMPLEMENTATION_SUMMARY.md`에는 구현된 것, standalone utility/stub만 있는 것, pipeline에 연결되지 않은 것을 구분하라.
- 다음은 실제 구현과 통합 테스트가 없으면 지원 또는 완료로 표현하지 마라: 재진입 가능한 단계 실행기와 중단 invocation 복구, 실제 `harness run` 및 provider config/credential loader, Docker/OS 수준 강제 격리, MCP gateway pipeline 연결, 실제 Claude/Codex 양쪽 live E2E.
- 이번 재작업에서 이 별도 기능까지 무리하게 확장하지 말고 remaining work로 남겨라.

## 5. 구현 비범위

이번 변경은 보안·정합성 결함 수정이다. 다음은 구현하지 않는다.

- 자동 commit, push, merge, deploy
- 웹 UI
- SQLAlchemy 또는 외부 workflow engine
- 새로운 범용 plugin/skill 프레임워크
- 실제 운영 credential 자동 탐색
- Provider가 하네스를 다시 호출하는 재귀 구조

비범위를 구현해 테스트 수만 늘리지 마라.

## 6. 설계 및 테스트 방식

- 가능한 한 테스트를 먼저 추가하고 각 테스트가 기존 코드에서 의도대로 실패하는지 확인한 다음 구현하라.
- public model/schema를 바꾸면 Pydantic 모델을 source of truth로 유지하고 generated JSON Schema drift 테스트를 갱신하라.
- 모델 반환값과 host-observed 값을 같은 타입에 섞지 마라. 이름에도 `claimed_*`, `observed_*`, `accepted_*`를 구분하라.
- security-sensitive 분기는 문자열 메시지 대신 enum/typed reason code를 사용하라.
- subprocess는 argv 배열과 `shell=False`만 사용하라.
- 모든 오류 로그와 artifact output에 redaction 정책을 적용하라.
- 테스트를 통과시키기 위해 hard gate를 약화하거나 skip/xfail을 추가하지 마라.
- 잘못된 fixture가 임의 task ID/digest를 생성한다면 제품 검증을 느슨하게 하지 말고 fixture를 실제 binding으로 수정하라.

## 7. 최소 완료 기준

1. R2-01부터 R2-09까지 코드 또는 정직한 문서 상태로 반영되었다.
2. hard gate를 generic approval로 우회하는 모든 테스트가 차단을 입증한다.
3. VerificationResult의 다섯 binding mismatch가 모두 차단된다.
4. command result evidence와 required evidence 검증이 실제로 동작한다.
5. 새 DB connection만으로 invocation/evidence/artifact/accepted decision을 조회할 수 있다.
6. 전체 provider lifecycle timeout과 effective policy budget이 테스트된다.
7. Windows junction traversal 방지 테스트가 Windows에서 실행된다.
8. keyless E2E가 실제 host command와 저장된 evidence를 사용한다.
9. 전체 테스트, schema drift, CLI help, diff hygiene 검사가 통과한다.
10. 새 warning을 만들지 않는다.

## 8. 반드시 실행할 검증 명령

현재 가상환경을 사용하라. 환경과 맞지 않으면 동등한 명령과 변경 이유를 보고하라.

```text
.venv\Scripts\python.exe -m pytest -q -rs
.venv\Scripts\python.exe -m pytest tests\unit\test_verification_service.py -q
.venv\Scripts\python.exe -m pytest tests\unit\test_orchestrator.py -q
.venv\Scripts\python.exe -m pytest tests\unit\test_manifest_validation.py tests\unit\test_claude_path_guard.py -q -rs
.venv\Scripts\python.exe -m pytest tests\unit\test_sqlite_contracts_persistence.py tests\unit\test_sqlite_persistence.py -q
.venv\Scripts\python.exe -m pytest tests\contract\test_schema_generation.py -q
.venv\Scripts\harness.exe --help
git diff --check
git status --short
```

`git diff --check`가 이 지시서 이전부터 존재한 문서의 Markdown hard line break 때문에 실패한다면 의미 없는 trailing whitespace만 안전하게 정리하라. 구현 결함을 문서 공백 문제와 섞어 보고하지 마라.

## 9. 완료 후 반환할 증거

`docs/WORK_LOG_2026-08-16_codex_rework_round2.md`를 새로 작성하고 응답에도 같은 핵심을 요약하라.

```text
# Rework Round 2 Result

## Verdict
COMPLETED | PARTIAL | BLOCKED | FAILED

## Requirement Matrix
| ID | Status | Changed files | Tests | Evidence |

## Security Invariants
- hard gate 우회 불가 증거
- verification binding mismatch 차단 증거
- junction/reparse 방어 증거
- timeout/budget enforcement 증거

## Persistence Evidence
- 생성된 DB 객체 종류와 조회 테스트
- artifact/evidence digest 검증 방식
- 재시작 후 복구 테스트

## Commands Executed
| Command | Exit code | Summary |

## Test Summary
- passed/skipped/failed/warnings 정확한 수
- 각 skip의 구체적 이유와 필수/선택 테스트 여부

## Changed Files
- 파일별 변경 목적

## Assumptions

## Unverified Items

## Remaining Risks

## Scope Exceptions Requested
```

다음은 증거로 인정하지 않는다.

- “코드를 추가했다”는 설명만 있는 경우
- 전체 테스트 수만 있고 새 부정 테스트 이름이 없는 경우
- 모델 보고 test result를 host 실행 결과 대신 제시한 경우
- live test가 skip되었는데 실제 연동 완료라고 주장하는 경우
- persistence API 존재만으로 정상 pipeline에 연결됐다고 주장하는 경우

## 10. 작업 중 중단해야 하는 조건

다음 상황에서는 임의 확장하지 말고 `BLOCKED` 또는 `PARTIAL`로 보고하라.

- 기존 staged 변경과 충돌하여 사용자 작업을 덮어써야 하는 경우
- 외부 API 키, 네트워크, 관리자 권한 또는 새 패키지가 필수인 경우
- schema 호환성 변경이 외부 소비자 결정을 필요로 하는 경우
- hard gate와 사용자 waiver 정책 사이에서 제품 정책 결정이 반드시 필요한 경우
- 실제 Windows junction 테스트를 실행할 Windows 환경이 전혀 없는 경우

단, Fake Provider, 임시 Git repo, SQLite, 일반 subprocess만으로 해결할 수 있는 테스트는 외부 의존성 부족을 이유로 생략하지 마라.
