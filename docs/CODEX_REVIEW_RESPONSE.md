# Codex Implementation Review — 대응 결과

**대상 리뷰**: `docs/CODEX_IMPLEMENTATION_REVIEW.md` (판정 `REWORK`)
**대응일**: 2026-08-15
**결과**: BLOCKER 7건 중 6건 해결(1건 부분 해결), HIGH 6건 중 4건 해결(2건은 사전에 알려진/문서화된
gap으로 재확인), MEDIUM 5건 전부 해결. 693 tests passing, 2 skipped, schema drift 없음.

이 문서는 finding별로 "무엇을 고쳤는지"와 "무엇을 못 고쳤는지"를 정직하게 기록한다 — 리뷰
자체가 "구현 요약이 완료를 선언하면서 스스로 gap을 인정하는 모순"을 지적했으므로, 같은 실수를
반복하지 않는다.

## 사실관계 확인

`632 passed, 4 skipped`(리뷰) vs `634 passed, 2 skipped`(구현 요약)는 오류가 아니다 — 리뷰
실행 환경에 symlink 생성 권한이 없어서 `test_manifest_validation.py`의 symlink 테스트 2개가
추가로 skip된 것뿐이다(전체 636개로 동일). `where harness`가 실행 파일을 못 찾은 건 실제
문제였고 아래 B-06에서 고쳤다.

## BLOCKER 대응

| ID | 판정 | 조치 |
|---|---|---|
| B-01 | 해결 | `providers/claude.py`에 `_check_tool_path()` 추가 — `can_use_tool`이 이제 tool_name뿐 아니라 `tool_input`의 실제 경로(`file_path`/`notebook_path`/`path`)를 검사해서 worktree 밖 경로와 (write 계열 tool의 경우) scope 밖 경로를 차단한다. `orchestrator.py`의 Worker 호출도 `active_grants.command_ids`(Command Broker namespace) 대신 `_claude_tool_ids_for()`(진짜 Claude SDK tool 이름, workspace_access 기반)를 쓰도록 고쳤다. Worker prompt에 ALLOWED/FORBIDDEN PATHS·제약·acceptance criteria를 명시하도록 강화했다. **Bash는 여전히 기본적으로 허용 목록에 없다** — `PolicyGrants`에 raw_shell 승인 여부를 담는 필드가 없어서(정책 평가는 그걸 승인 게이트로만 쓰고 결과 grants에는 반영 안 함), fail-closed 상태로 남겨뒀다. |
| B-02 | 해결 | `execution/evidence.py::freeze_and_validate`가 이제 host check 실행 **후**에도 다시 freeze해서(`post_test_manifest`) 그 차이를 `test_side_effects`로 기록한다. `application/verification.py`에 `find_check_execution_violations()`를 추가해 timeout/output cap 초과/미실행/비허용 exit code/test_side_effects를 전부 deterministic PASS 거부 사유로 만들었다 — Verifier의 자기 판단과 무관하게 작동한다. |
| B-03 | 해결 | `PipelineDeps`의 승인 콜백 기본값을 `_default_approve`(항상 True)에서 `_default_deny`(항상 False, fail-closed)로 변경. |
| B-04 | **부분 해결** | `persistence/migrations.py`에 `task_contracts`/`policy_decisions`/`verification_results`/`rework_contracts` 테이블 추가, `persistence/sqlite.py`에 insert/get/list 함수 추가, `orchestrator.py`가 각 객체가 만들어지는 시점에 실제로 저장하도록 연결(accepted TaskContract, 모든 PolicyDecision(거부 포함), Verifier의 VerificationResult, 매 ReworkContract). **`Approval`/`AgentSession`/`WorkspaceLease`/`CommandRun`은 여전히 영속화되지 않는다** — 리뷰가 요구한 완료 기준("프로세스 종료 후 DB만으로 다음 합법 상태를 복원")에는 아직 못 미친다. |
| B-05 | **미해결** | CLI `approve`/`resume`이 Run 상태 기계는 정확히 전이시키지만, 중단된 파이프라인 step을 재개하지는 못한다 — B-04에서 설명한 대로 아직 완전한 재개에 필요한 모든 객체가 저장되지 않고, 무엇보다 `run_task_pipeline()`이 여전히 monolithic 함수라서(re-entrant step executor 없음) 애초에 "중간부터 재개"할 진입점이 없다. 이건 Codex가 "Priority 0.2"로 명시한, 사실상 Phase 9/10을 처음부터 다시 설계하는 수준의 작업이라 이번 대응에서는 시도하지 않았다. |
| B-06 | 해결 | `pyproject.toml`에 `[project.scripts] harness = "agent_harness.interfaces.cli:main"` 추가(`where harness` 이제 찾음). `harness run`을 `harness demo`로 이름 변경 — 실제로는 FakeAgentProvider만 실행하면서 이름이 `run`인 게 오해 소지였다는 지적을 그대로 반영했다. 실제 Provider/config loader가 붙은 `run` 커맨드는 여전히 없다(B-04/B-05와 같은 이유로 후속 작업). |
| B-07 | 해결 | `orchestrator._invoke_role`/`verification.run_verification`이 이제 `budget.timeout_seconds`로 실제 `asyncio.wait_for()`를 걸고, timeout 시 `provider.cancel()`을 best-effort로 호출한 뒤 세션을 닫고 `PipelineTimeoutError`를 던진다. `AgentRunRequest.max_turns`도 budget에서 채운다. 매 invocation 후 `accumulate_usage()`로 budget_used를 누적하고 다음 invocation 전 `check_budget()`으로 검사한다(단, Run 행 자체에 대한 영속화는 B-04와 같은 이유로 아직 없음 — 세션 내 메모리에서만 추적). |

## HIGH 대응

| ID | 판정 | 조치 |
|---|---|---|
| H-01 | 해결 | `run_task_pipeline`이 Planner를 부르기 **전에** `git_client.rev_parse("HEAD")`/`compute_repository_fingerprint()`로 authoritative base SHA·fingerprint를 미리 고정하고, Planner가 뭘 제안하든 contract acceptance 시점에 `repository_id`/`base_commit_sha`/`target_ref`/`expected_repository_fingerprint`를 전부 덮어쓴다. `create_worktree` 직전에 `verify_repository_fingerprint()`도 실제로 호출한다(TOCTOU 방지). |
| H-02 | 해결 | `domain/digests.py`에 `normalize_identifier_slug`/`IdentifierSlug` 추가(영숫자/`.`/`_`/`-`만 허용하는 slug), `Run.run_id`/`Run.repository_id`/`RepositoryRef.repository_id`/`WorkspaceLease.repository_id`에 적용. `execution/workspace.py::worktree_path_for`에도 resolved path가 실제로 data_root 아래인지 확인하는 방어선을 추가(모델 검증을 우회한 호출자에 대한 defense-in-depth). |
| H-03 | 해결 | `execution/validation.py::build_file_manifest`가 이제 `dirnames`도 lstat해서 directory symlink/junction(Windows reparse point 포함, `st_file_attributes` 체크)을 manifest entry로 기록한다 — 이전엔 `os.walk(followlinks=False)`가 이런 항목을 완전히 누락시켰다. |
| H-04 | 해결 | `test_live_dual_agent_pipeline_smoke`에서 하드코딩된 credential 경로 제거(`skipif`가 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 환경변수 존재 여부만 확인), 성공 조건을 `final_run.state is not None`(FAILED도 통과)에서 `READY_FOR_MERGE` + journal/evidence 검증으로 강화. |
| H-05 | 재확인(미해결) | 이미 Phase 13 문서에 기록된 gap 그대로다 — 이 세션의 Docker Desktop daemon이 꺼져 있어서 `ContainerSandbox`의 실제 컨테이너 실행 경로는 검증 못 했다. daemon을 이번 대응에서도 직접 켜지 않았다(요청 없이 무거운 백그라운드 서비스를 켜는 건 범위 밖이라고 판단). |
| H-06 | 재확인(부분 문서화된 gap) | stale RepoLock 회수·orphan process 종료·WSL2·provider invocation reconciliation은 Phase 13 자체 문서(`docs/architecture/hardening-and-recovery.md`)가 이미 명시적으로 "미구현"이라고 적어둔 항목들이다. `check_budget`/persistence 관련 일부(B-04/B-07)는 이번에 진전이 있었지만, 리뷰가 요구하는 "완전한 복구"수준에는 못 미친다. |

## MEDIUM 대응

| ID | 조치 |
|---|---|
| M-01 | `execution/scope_guard.find_scope_violations()`를 `freeze_and_validate`(effective granted scope 대상)와 `run_verification`(deterministic PASS gate)에 실제로 연결. |
| M-02 | `find_scope_violations()`에 `max_changed_bytes`(result/baseline manifest의 size 합산)와 `declared_generated_paths`(카운팅에서만 제외, scope 멤버십 검사는 그대로 적용) 지원 추가. |
| M-03 | `find_test_mutations()`가 이제 `diff.added`도 검사한다 — 원래 실패하던 테스트를 안 건드리고 새 테스트 파일을 추가하는 방식의 회피를 이제 감지한다. |
| M-04 | `persistence/sqlite.py`에 `count_artifacts_with_content_digest()` 추가, `cli.py`의 `cleanup --purge`가 다른 Artifact 행이 같은 content_digest를 참조 중이면(다른 Run 포함) blob을 지우지 않고 skip한다. |
| M-05 | `providers/codex.py::_safe_dump`에서 pydantic `UserWarning`을 좁게 필터링 — 원인은 `openai_codex` SDK 자체의 `Turn.items_view` 필드 기본값이 enum이 아니라 raw string인 업스트림 버그였다(우리 코드의 버그가 아님, 실제 직렬화 값은 정확함). |

## 재검증

```
.venv\Scripts\python.exe -m pytest -q
→ 693 passed, 2 skipped
python -m agent_harness.schema_export → drift 없음
git add -A → 전부 staged, 커밋 없음
```

## 남은 진짜 gap (정직하게)

1. **B-05/Priority 0.2**: re-entrant step executor 없음 — `run_task_pipeline`은 여전히 하나의
   monolithic 코루틴이라, 프로세스가 승인 대기 중 종료되면 다른 프로세스가 이어받을 방법이
   없다. CLI `approve`/`resume`은 Run의 상태 기계만 정확히 전이시킬 뿐이다.
2. **B-04 나머지**: `Approval`/`AgentSession`/`WorkspaceLease`/`CommandRun` 영속화 없음.
3. **B-01 나머지**: Bash tool은 여전히 Worker에게 기본적으로 허용되지 않는다(raw_shell 승인을
   `PolicyGrants`에 반영하는 필드가 없어서).
4. **B-06 나머지**: 실제 Claude/Codex config loader가 붙은 `harness run` 커맨드 없음.
5. **H-05/H-06**: Container 실제 실행 미검증(daemon 꺼짐), orphan process 정리·WSL2·stale
   lock 회수 미구현 — 전부 Phase 13 문서에 이미 있던 gap.

이 다섯 가지는 전부 "Priority 0.1(durable persistence 전체)"과 "Priority 0.2(re-entrant
executor)"로 수렴한다 — Codex 리뷰가 제시한 순서 그대로, 이 두 가지가 남은 작업 중 가장 크고
가장 근본적인 항목이라는 뜻이다.
