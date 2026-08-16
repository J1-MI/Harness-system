# 작업 로그 — Codex REWORK 판정 대응

**작업일**: 2026-08-15
**작업 범위**: `docs/CODEX_IMPLEMENTATION_REVIEW.md`(판정 `REWORK`)의 findings 조치
**작업 결과**: 693 passed, 2 skipped, schema drift 없음, 전부 staged(커밋 없음)

---

## 배경

이전 세션에서 Agent Harness 로드맵 13개 phase 구현을 완료했다고 선언했으나, Codex가 독립적으로
코드를 검토하고 `REWORK` 판정을 남겼다. 핵심 지적:

> "현재 결과물은 테스트가 잘 갖춰진 기반 구현이지만, 실제 Codex–Claude 하네스로 실행·중단·재개·
> 감사할 수 있는 상태는 아니다. 구현 요약도 완료를 선언하면서 핵심 실행 경로가 연결되지 않았다고
> 스스로 인정하므로 완료 선언과 기술적 사실이 모순된다."

BLOCKER 7건, HIGH 6건, MEDIUM 5건, 총 18개 findings가 나왔고, Codex는 이를 Priority 0.1~0.6과
Priority 1로 순서를 매겨 제시했다. 이 로그는 그 findings를 하나씩 처리한 과정을 기록한다.

## 사실관계 먼저 확인

작업 착수 전 Codex 리뷰의 구체적 주장부터 검증했다.

- **테스트 수 불일치**(`632 passed, 4 skipped` vs 기존 요약의 `634 passed, 2 skipped`): 오류
  아님. Codex 검토 환경에 symlink 생성 권한이 없어서 `test_manifest_validation.py`의 symlink
  테스트 2개가 추가로 skip된 것뿐(총계 636개로 동일).
- **`where harness` 실패**: 실제 문제였음 — `pyproject.toml`에 `[project.scripts]`가 없어서
  `pip install -e .` 후에도 `harness` 실행 파일이 안 만들어지고 있었다. 아래 B-06에서 수정.

## 처리한 findings (순서대로)

### 1. B-03 — 승인 기본값이 자동 승인이었던 문제

`PipelineDeps.decide_policy_approval`/`decide_final_approval`의 기본값이 항상 `True`를
반환하는 `_default_approve`였다 — 호출자가 콜백을 안 넘기면 묵시적으로 전부 승인되는 구조.

**조치**: 기본값을 `_default_deny`(항상 `False`, fail-closed)로 교체. `evaluate_policy`/
`probe_capabilities` 등 이 코드베이스 전체가 이미 따르는 fail-closed 원칙과 일치시켰다.
기존 Fake E2E 테스트들은 자동승인이 필요했으므로 테스트 헬퍼(`make_deps`)에 명시적
approve-everything 기본값을 추가해서 분리했다. 기본값 자체가 거부하는지 확인하는 전용 테스트
추가.

### 2. H-03 — manifest이 directory symlink/junction을 놓치는 버그

`build_file_manifest`가 `os.walk(followlinks=False)`로 순회하면서 `filenames`만 기록하고
`dirnames`는 검사하지 않았다. Directory symlink는 `dirnames`에 나타나므로 manifest에서
완전히 누락됐다 — 공격자가 디렉터리 symlink를 심어도 diff/scope 검사에 전혀 안 잡힌다는 뜻.

**조치**: `dirnames`의 각 항목도 `lstat`해서 symlink/junction이면 manifest entry로 기록하도록
수정. Windows junction은 `S_ISLNK`로 안 잡히므로 `st_file_attributes &
FILE_ATTRIBUTE_REPARSE_POINT`도 추가로 검사하는 `_is_reparse_point()` 헬퍼를 만들었다. 실제
디렉터리 symlink를 만들어 밖의 파일을 참조시키고, manifest에 symlink entry로만 잡히고 내부
파일은 절대 순회 안 되는지 확인하는 회귀 테스트 추가.

### 3. H-04 — 라이브 E2E 테스트 위생 문제

`test_live_dual_agent_pipeline_smoke`에 사용자 이름이 포함된 credential 파일 경로가 소스에
하드코딩돼 있었고, API 키를 테스트 내부에서 읽어 전역 `os.environ`에 직접 설정했으며, 마지막
assertion이 `final_run.state is not None` 하나뿐이라 `FAILED`로 끝나도 테스트가 통과했다.

**조치**: 하드코딩된 경로 제거 — `skipif` 조건에 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 환경변수
존재 여부만 넣어서, 호출자가 셸에서 미리 export해야만 테스트가 돌게 바꿨다(Phase 6/7에서 이미
쓰던 패턴과 통일). 성공 조건을 `READY_FOR_MERGE` + journal/evidence 실제 존재 확인으로
강화했다. 이후 저장소 전체에 `sk-ant-`/`sk-proj-` 패턴이 없는지 grep으로 재확인.

### 4. M-03/M-04/M-05 — 세 개의 작은 버그

- **M-03**: `find_test_mutations()`가 `modified`/`deleted`만 검사하고 `added`는 검사 안
  했다 — 원래 실패하던 테스트를 안 건드리고 새로 트리비얼하게 통과하는 테스트 파일을 추가하는
  방식으로 회피 가능했다. `diff.added`도 검사 대상에 포함시켜 수정.
- **M-04**: CLI의 `cleanup --purge`가 content-addressed 블롭을 지울 때 다른 Artifact 행(다른
  Run 포함)이 같은 `content_digest`를 참조 중인지 확인하지 않고 그냥 지웠다 — 우연히 같은
  내용(예: 동일한 "ok" stdout)을 만든 다른 Run의 evidence가 깨질 수 있었다.
  `count_artifacts_with_content_digest()`를 추가해서 다른 참조가 있으면 blob을 지우지 않고
  skip하도록 수정.
- **M-05**: Codex adapter 테스트 3건에서 pydantic 직렬화 경고가 발생했다. 원인 추적 결과
  `openai_codex` SDK 자체의 `Turn.items_view` 필드 기본값이 enum이 아니라 raw string("full")로
  선언된 업스트림 버그였다(실제 직렬화 값 자체는 정확했음 — 우리 코드 버그 아님). 그 한 가지
  경고만 정확히 좁혀서 필터링하는 방식으로 노이즈 제거.

### 5. M-01/M-02 — scope_guard가 실제 경로에 연결 안 되어 있던 문제

이전 세션(Phase 13)에서 `execution/scope_guard.py::find_scope_violations()`를 만들었지만
standalone 함수로만 존재하고 `freeze_and_validate`/`run_verification` 어디에서도 호출되지
않았다 — 즉 "Worker가 scope 밖을 수정했는지"를 실제로 검사하는 결정론적 코드가 파이프라인에
없었고, 전적으로 Codex Verifier의 자유 텍스트 판단에 의존하고 있었다.

**조치**:
- `find_scope_violations()`에 `max_changed_bytes`(baseline/result manifest의 size 합산)와
  `declared_generated_paths`(카운팅에서만 제외, forbidden/allowed 멤버십 검사는 그대로 적용)
  지원을 추가.
- `execution/evidence.py::freeze_and_validate`에 `scope: ScopeRules | None = None` 파라미터를
  추가하고, 주어지면 `find_scope_violations()`를 실제로 호출해서 `FrozenValidationResult
  .scope_violations`에 채운다.
- `application/verification.py::run_verification`이 `frozen_result.scope_violations`를 prompt
  에도 포함시키고, 비어있지 않으면 모델이 PASS를 주장해도 `MANUAL_REVIEW`로 강등하는 deterministic
  gate에 추가.
- `orchestrator.py`가 `freeze_and_validate` 호출 시 `scope=active_grants.path_rules`(효과적으로
  승인된 scope)를 실제로 넘기도록 연결.
- 이 변경으로 기존 Fake E2E 테스트 4개가 실패했는데, 원인은 테스트 fixture 자체가 worktree
  루트에 파일을 쓰면서 계약의 scope(`src/**`)를 벗어나 있었기 때문이었다 — 즉 새 검사가 실제
  버그(테스트 fixture의 scope 불일치)를 정확히 잡아낸 것이었다. fixture를 `src/` 아래에 쓰도록
  수정해서 해결.

### 6. H-01 — Planner가 authoritative 저장소 정보를 바꿀 수 있던 문제

orchestrator는 Planner의 구조화 출력에서 `task_id`/`run_id`만 덮어쓰고, `repository_id`/
`base_commit_sha`/`target_ref`/`expected_repository_fingerprint`는 Planner가 제안한 값을
그대로 받아들이고 있었다 — 악의적이거나 오작동하는 Planner가 완전히 다른 commit SHA를 지정해도
그대로 체크아웃됐다. `GitClient.verify_repository_fingerprint()`도 실제 worktree 생성 경로에서
호출되지 않고 있었다.

**조치**: `run_task_pipeline`이 Planner를 부르기 **전에** `git_client.rev_parse("HEAD")`와
`compute_repository_fingerprint()`로 authoritative base SHA·fingerprint를 미리 고정한다.
Planner가 뭘 제안하든, contract acceptance 시점에 `repository_id`(=`run.repository_id`,
Harness 소유)/`base_commit_sha`/`target_ref`/`expected_repository_fingerprint`를 전부
강제로 덮어쓴다. `create_worktree` 직전에 `verify_repository_fingerprint()`를 실제로 호출해서
TOCTOU(확인 시점과 사용 시점 사이 저장소가 바뀌는 경우)도 방어한다. Planner가 존재하지 않는
가짜 commit SHA와 다른 repository_id를 주장해도 파이프라인이 실제 HEAD로 정상 진행되는지
확인하는 테스트 추가.

### 7. H-02 — repository_id/run_id의 path containment 검증 부재

`repository_id`/`run_id`가 worktree 경로의 segment로 직접 쓰이는데(`data_root / "workspaces" /
repository_id / run_id`) 어떤 형식 검증도 없었다. 실제로 확인해보니 Windows에서
`Path("data_root") / "workspaces" / "C:\\evil" / "run-1"`처럼 절대경로 형태의 segment를 넣으면
`Path.__truediv__`가 전체 경로를 그 값으로 **완전히 교체**해버리는 실제 취약점이 있었다(원래
data_root와 무관한 `C:\evil\run-1`이 됨).

**조치**: `domain/digests.py`에 `normalize_identifier_slug()`/`IdentifierSlug` 타입을 추가 —
영숫자로 시작하고 영숫자/`.`/`_`/`-`만 허용하는 slug 형식만 통과시킨다. `Run.run_id`/
`Run.repository_id`/`RepositoryRef.repository_id`/`WorkspaceLease.repository_id`에 적용.
추가로 `execution/workspace.py::worktree_path_for()`에도 resolve된 경로가 실제로 data_root
아래에 있는지 확인하는 방어선을 넣었다 — 모델 검증을 우회한 호출자에 대한 defense-in-depth.
`../../etc`, `C:\evil` 등 실제 공격 패턴이 거부되는지 확인하는 테스트 추가.

### 8. B-06(일부) — console script entry point + `run`/`demo` 명령 분리

`pyproject.toml`에 `[project.scripts]`가 없어서 `where harness`가 실행 파일을 못 찾고 있었다.
또한 `harness run` 커맨드는 이름과 달리 실제로는 `FakeAgentProvider`만 실행하는 데모였다 —
오해 소지가 있는 이름이라는 지적.

**조치**: `[project.scripts] harness = "agent_harness.interfaces.cli:main"` 추가하고
재설치해서 `harness.exe`가 실제로 생성/작동하는지 확인. `run` 커맨드를 `demo`로 이름 변경하고,
docstring에 "이건 데모 경로이지 실제 자동화가 아니다"를 명시. 실제 Provider/config loader가
붙은 진짜 `run` 커맨드는 만들지 않았다(아래 "남은 gap" 참고).

### 9. B-02(나머지) — post-test manifest + deterministic exit-code/timeout hard gate

`freeze_and_validate`가 host check 실행 **전에만** manifest을 동결하고 있었다 — host test가
실행 중 worktree를 추가로 변경해도(악성/오작동 스크립트) 그 변경은 어떤 evidence에도 안 잡혔다.
또한 command의 실제 exit code/timeout/output cap 초과 여부를 검사하는 deterministic 코드가
전혀 없어서, 실패한 command의 evidence를 인용해도 Verifier가 PASS라고 하면 그대로 받아들여질
수 있었다.

**조치**:
- `freeze_and_validate`가 host check 실행 **후**에 두 번째 freeze(`post_test_manifest`)를
  수행하고, 그 차이를 `FrozenValidationResult.test_side_effects`로 기록.
- `application/verification.py`에 `find_check_execution_violations()`를 새로 추가 — 각
  COMMAND-verified acceptance criterion에 대해 매칭되는 command execution이 실제로 있는지,
  timeout/output cap 초과가 없었는지, exit code가 criterion이 선언한 `expected_exit_codes`
  안에 있는지, 그리고 `test_side_effects`가 비어있는지를 전부 deterministic하게 검사해서
  하나라도 위반이면 PASS를 무조건 거부한다 — 모델의 판단과 무관하게 작동.
- `orchestrator.py`의 rework 루프가 rolling baseline으로 쓰던 `result_manifest`(host test
  실행 전 상태)를 `post_test_manifest`(진짜 최종 상태)로 바꿔서, 이번 attempt의 host test가
  만든 변경이 다음 attempt의 "새 변경"으로 잘못 재분류되지 않게 했다.
- 이 변경으로 기존 Fake E2E 테스트가 다시 실패했는데, 이번엔 테스트 fixture의 계약이
  `command_id="pytest"`(factory 기본값)를 참조하면서 실제로는 "check"라는 다른 명령만
  등록/실행하고 있었기 때문이었다 — 새 게이트가 "criterion이 요구하는 명령이 실행된 적 없음"을
  정확히 잡아낸 것. fixture의 acceptance criteria가 실제 등록된 명령을 참조하도록 수정.

### 10. B-07 — timeout/usage/budget/cancellation이 전혀 연결 안 되어 있던 문제

orchestrator가 Provider 요청의 `deadline`에 `_utc_now()`(호출 시점의 현재 시각, 사실상 이미
지난 데드라인)를 그대로 넣고 있었고, Claude/Codex adapter의 `await_result()`는 그 deadline을
전혀 집행하지 않아서 무기한 대기했다. `accumulate_usage()`/`check_budget()`은 단위 함수로만
존재하고 orchestrator 어디에서도 호출되지 않았다.

**조치**:
- `_invoke_role()`/`run_verification()`이 `budget: BudgetRequest`를 받아서
  `now + timedelta(seconds=budget.timeout_seconds)`로 진짜 deadline을 계산하고,
  `asyncio.wait_for()`로 실제 wall-clock timeout을 건다. Timeout 발생 시
  `provider.cancel()`을 best-effort로 호출(지원 안 하는 provider는 조용히 스킵)한 뒤 세션을
  닫고 `PipelineTimeoutError`를 던진다.
- `AgentRunRequest.max_turns`도 budget에서 채우도록 연결(이전엔 항상 `None`).
- 매 invocation(Planner/Worker/Verifier) 완료 후 `accumulate_usage()`로 세션 내
  `budget_used`를 누적하고, 다음 invocation 전에 `check_budget()`으로 검사해서 초과 시
  `FAILED`로 전이한다.
- 실제로 3600초 동안 응답하지 않는 fake provider를 만들어 1초 timeout budget으로 정말
  `PipelineTimeoutError`가 나고 `cancel()`이 호출되는지 확인하는 테스트, budget이 이미
  소진된 상태로 파이프라인을 시작하면 Planner 호출 전에 깔끔하게 `FAILED`로 끝나는지 확인하는
  테스트 추가.

### 11. B-01 — Claude Worker 권한이 실제 실행 경계에서 강제되지 않던 문제 (가장 큰 항목)

`ClaudeAgentAdapter._make_can_use_tool()`이 도구 **이름**만 검사하고 `tool_input`(실제
인자 — 예: Write/Edit 도구의 `file_path`)은 전혀 검사하지 않았다. `PolicyGrants.path_rules`가
Claude SDK 도구 실행에 전혀 적용되지 않아서, 이론상 Write/Edit이 허용된 경우 Claude가
절대경로나 `..` 상대경로로 worktree 밖 호스트 파일 어디든 접근 가능했다. 게다가 orchestrator는
Host Test Runner용 `command_ids`(예: "pytest", "check")를 그대로 Claude SDK의
`allowed_tool_ids`로 전달하고 있었는데, 이 둘은 완전히 다른 ID namespace라서 실제로는 어떤
진짜 Claude 도구(Read/Write/Edit/Bash)도 허용되지 못하고 있었다(command_id는 유효한 Claude
도구 이름이 아니므로).

**조치**:
- `providers/claude.py`에 `_check_tool_path()`를 새로 추가 — Read/Write/Edit/NotebookEdit/
  Glob/Grep 도구의 경로 인자(`file_path`/`notebook_path`/`path`)를 실제로 resolve해서
  workspace root 밖으로 나가면 무조건 거부. Write/Edit/NotebookEdit(쓰기 가능한 도구)은
  추가로 `execution.scope_guard.path_matches_glob()`을 재사용해서 `ScopeRules
  .allowed_path_rules`/`forbidden_path_rules`까지 검사(읽기 전용 도구는 scope 제한 없이
  worktree 안이면 허용 — 코드 이해를 위해 scope 밖 파일도 읽어야 할 때가 있으므로).
  `_make_can_use_tool()`이 이 검사를 실제로 호출하도록 연결.
- `orchestrator.py`에 `_claude_tool_ids_for()`를 새로 추가 — `PolicyGrants.workspace_access`
  기반으로 진짜 Claude SDK 도구 이름 목록(`Read`/`Write`/`Edit`/`Glob`/`Grep`)을 만든다.
  `Bash`는 의도적으로 기본값에서 제외했다 — `PolicyGrants`에 raw_shell 승인 여부를 담는
  필드가 없어서(정책 평가는 그걸 승인 게이트로만 쓰고 결과 grants에는 반영 안 함) fail-closed로
  남겨뒀다. Worker 호출의 `allowed_tool_ids`를 `active_grants.command_ids`(잘못된 namespace)
  대신 이 함수 결과로 바꿨다.
- `_build_worker_prompt()`가 이제 ALLOWED PATHS/FORBIDDEN PATHS/제약/acceptance criteria를
  명시적으로 포함한다 — 이전엔 objective 한 줄만 전달했다.
- worktree 밖 절대경로/`..` 상대경로 거부, scope 밖(하지만 worktree 안) 쓰기 거부, 같은 조건
  읽기는 허용, 실제 Claude 도구 이름이 command_id가 아님을 확인하는 테스트 10개+2개 추가.

### 12. B-04(부분) — 핵심 계약/결정 객체 영속화

TaskContract/PolicyDecision/Approval/AgentSession/WorkspaceLease/CommandRun/
VerificationResult/ReworkContract 중 어느 것도 durable table이 없어서, 프로세스가 끝나면
"무엇을 왜 결정했는지"가 전부 사라졌다.

**조치**: 이번 대응에서는 감사 관점에서 가장 중요한 네 가지만 영속화했다.
- `persistence/migrations.py`에 `task_contracts`/`policy_decisions`/`verification_results`/
  `rework_contracts` 테이블 추가(각각 전체 모델을 `data_json`으로 저장 + 조회용 인덱스 컬럼).
- `persistence/sqlite.py`에 각각 insert/get/list 함수 추가.
- `orchestrator.py`가 실제로 이 함수들을 호출하도록 연결: accepted TaskContract는
  digest 계산 직후, PolicyDecision은 **거부된 것 포함** 모든 `evaluate_policy()` 호출 직후,
  VerificationResult는 Verifier 호출 직후, ReworkContract는 생성 직후.
- 실제 Fake E2E 파이프라인을 한 번 돌려서 DB에서 TaskContract/PolicyDecision/
  VerificationResult를 다시 꺼내 원본과 일치하는지 확인하는 통합 테스트 추가.
- **Approval/AgentSession/WorkspaceLease/CommandRun은 여전히 영속화하지 않았다** — 아래
  "남은 gap" 참고.

## 최종 검증

```
.venv\Scripts\python.exe -m pytest -q
→ 693 passed, 2 skipped, schema drift 없음

python -m agent_harness.schema_export → 변경 없음(drift 없음)

grep -r "sk-ant-\|sk-proj-" . (소스 전체) → 없음

git add -A → 전부 staged, 커밋은 하지 않음
```

새로 추가한 테스트 파일: `test_claude_path_guard.py`(10개), `test_sqlite_contracts_persistence
.py`(5개). 기존 파일에 추가한 테스트: 약 25개. 이전 세션 대비 총 테스트 수 634 → 693(+59).

## 문서화한 결과 파일

- `docs/CODEX_REVIEW_RESPONSE.md` — finding별 표 형태 요약(BLOCKER/HIGH/MEDIUM 각각 대응
  상태와 조치 내용, 남은 gap 목록).
- `docs/IMPLEMENTATION_SUMMARY.md` 상단에 "Codex 검토 및 대응" 섹션 추가 — 원래 "13-phase 전부
  완료" 선언이 정정됐음을 명시.
- 이 파일(`docs/WORK_LOG_2026-08-15_codex_rework_response.md`) — 이번 작업 세션 자체의
  타임라인/근거/조치 상세 기록.

## 이번 작업으로도 못 고친 것 (정직하게)

1. **재개 가능한 파이프라인 없음(B-05, Codex Priority 0.2)**: `run_task_pipeline()`이 여전히
   하나의 monolithic 코루틴이다. CLI `approve`/`resume`은 Run의 상태 기계 전이는 정확히
   수행하지만, 승인 대기 중 프로세스가 종료되면 다른 프로세스가 이어받아 Worker 재호출 등을
   재개할 방법이 없다. 이건 Phase 9/10 오케스트레이터를 상태별 idempotent step executor로
   다시 설계하는 수준의 작업이라 이번 대응 범위에서 제외했다.
2. **B-04 나머지**: Approval/AgentSession/WorkspaceLease/CommandRun 영속화 없음.
3. **B-01 나머지**: Bash tool은 Worker에게 기본적으로 허용되지 않는다(위 설명 참고).
4. **B-06 나머지**: 실제 Claude/Codex config loader + credential 연결이 붙은 진짜 `harness
   run` 커맨드가 없다(`demo`만 있음).
5. **H-05/H-06(이전부터 알려진 gap 재확인)**: 이 세션의 Docker Desktop daemon이 꺼져 있어서
   `ContainerSandbox`의 실제 컨테이너 실행 경로는 여전히 검증 못 했다(요청 없이 무거운
   백그라운드 서비스를 직접 켜지 않기로 판단). orphan process 정리, stale lock 회수, WSL2도
   미구현 — Phase 13 자체 문서에 이미 있던 gap 그대로.

이 다섯 가지는 전부 Codex가 "Priority 0.1(durable persistence 전체)"과 "Priority
0.2(re-entrant executor)"로 명시한 두 항목으로 수렴한다.
