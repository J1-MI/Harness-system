# Hardening and Recovery — Phase 13 (final roadmap phase)

로드맵 범위: "WSL2/container backend, fault injection, concurrency, stale base, retention, secret
incident", 비범위: "distributed scheduler". target: "security/integration suites". 테스트 기준:
"위협 모델 공격 fixture 통과, recovery drill". depends on: "전체"(all 12 prior phases). 이
문서는 무엇을 실제로 구현했고, 무엇을 정직하게 gap으로 남겼는지 둘 다 기록한다 — 이 phase가
"악성·장애 시나리오 검증"이라는 이름을 달고 있는 만큼, 실제로 검증되지 않은 걸 검증됐다고
써놓는 게 가장 나쁜 결과이기 때문이다.

## ContainerSandbox: 진짜 Docker 가용성 probe, 이 세션에선 실제 실행은 검증 못 했다

`execution/sandbox.py`에 `ContainerSandbox`(Docker 기반)를 추가했다. `probe_capabilities()`는
`docker info`를 실제로 서브프로세스로 실행해서 daemon이 살아있는지 확인한다 — `docker` 바이너리가
PATH에 있다는 것만으로 CONTAINER를 available로 주장하지 않는다(Phase 3.2의 fail-closed 원칙을
그대로 계승). 이 세션이 실행된 Windows 머신에는 Docker Desktop이 설치는 돼 있지만 daemon이 꺼져
있었다 — `docker info`가 실패하므로 `probe_capabilities()`는 정확히 `TRUSTED_LOCAL`만
보고한다. **Docker Desktop을 이 세션에서 직접 켜지 않았다** — 사용자 머신에서 무거운 백그라운드
서비스를 요청 없이 띄우는 건 "로컬, 되돌리기 쉬운" 범주를 벗어난다고 판단했기 때문이다. 그래서
`ContainerSandbox.run()`이 실제로 컨테이너를 띄워 명령을 실행하는 경로는 이번 세션에서
end-to-end로 검증되지 않았다 — `docker run` argv 조립 로직과 wrapper-env 격리(작업의 env가
outer docker CLI 프로세스로 새지 않는지)는 mocking으로 검증했지만, 진짜 Docker daemon 상대로는
못 돌렸다. `WSL2`는 여전히 미구현 — Docker Desktop 내부 WSL2 통합은 임의 명령을 실행할 수 있는
범용 WSL2 배포판이 아니라서 별도로 구현하지 않았다.

## Recovery Coordinator: 상태 기계가 이미 정의한 경계를 그대로 따랐다

`application/recovery.py`. 처음엔 "crash가 나면 RECOVERY_REQUIRED로 보낸다"를 모든 활성
상태에 똑같이 적용하려 했는데, Phase 1.2의 TRANSITIONS 테이블을 다시 확인해보니
`RECOVERY_REQUIRED`는 `PREPARING_WORKSPACE`/`EXECUTING`/`FREEZING_RESULT`/`HOST_VALIDATING`
네 상태에서만 도달 가능한 target이었다 — `PLANNING`/`CONTRACT_VALIDATING`/`VERIFYING`/
`REWORK_CONTRACTING`(순수 API 호출/인메모리 상태, 실제 host-side lease가 없는 상태)에서는
애초에 그 전이가 허용되지 않는다. 이게 우연이 아니라 의도된 설계라고 판단했다 — host 자원을
쥐고 있는 상태만 "조정이 필요한" 상태고, 나머지는 그냥 "재시도 가능한 실패"로 충분하다. 그래서
`run_recovery_scan`은 두 그룹을 분리해서 다르게 처리한다: 전자는 `RECOVERY_REQUIRED` +
`PendingAction`, 후자는 `FAILED` + `retriable=True`인 `FailureRecord`(같은 요청을 새 Run으로
다시 시도할 수 있다는 신호, 자동 재시도는 절대 하지 않는다 — "Provider invocation 자체를
자동 재실행하지 않는다"). `test_recoverable_and_no_recovery_path_sets_partition_all_active_states`
테스트가 이 두 집합이 실제로 모든 활성 상태를 빠짐없이, 겹침 없이 나누는지 코드로 직접
확인한다.

`check_base_revision_stale`(H-08/`BASE_REVISION_STALE`)은 순수 함수로 따로 만들었다 —
`run_recovery_scan`에 자동으로 연결하지 않았는데, 이유는 아래 "정직하게 남긴 gap" 참고.

## Retention: 리뷰의 기본값을 그대로 숫자로 옮겼다

`execution/retention.py`: `RetentionPolicy`(quarantine 7일, completed 24시간, artifact blob
30일)와 `is_workspace_cleanup_eligible`/`is_artifact_purge_eligible` — 순수 정책 함수만,
파일시스템/DB 접근은 전혀 하지 않는다. Phase 12의 `cleanup`/`purge` 커맨드가 실제 삭제를
수행하는 쪽이고, 이 모듈은 "지금 삭제해도 되는 나이인가"만 판단한다.

## Secret Incident: 기존 quarantine enum을 처음으로 실제로 썼다

`RedactionStatus.QUARANTINED`는 Phase 1.1부터 정의만 돼 있고 어디서도 실제로 만들어지지 않고
있었다. `execution/incident.py::quarantine_and_delete_artifact`가 처음으로 이 상태를 실제
시나리오에 연결한다 — 다만 `Artifact`가 불변(`생성 후 불변`) 모델이라 상태를 "바꿔서" 저장할
수는 없고, 그래서 Phase 12의 cleanup `--purge`와 정확히 같은 메커니즘(blob 즉시 삭제 +
tombstone)을 재사용했다. `"incident": true` 필드로 일상적 retention purge와 구분되는 tombstone을
남긴다. `scan_artifact_for_secrets`는 write 시점 redaction(Phase 2.2)이 놓친 걸 나중에
재검사할 수 있도록 같은 패턴 세트(`persistence.artifacts.contains_secret_pattern`, 이번에
새로 public으로 노출)를 재사용한다 — 패턴을 두 벌 유지하지 않는다.

## 이번 phase에서 새로 발견한 진짜 gap: scope 위반의 결정론적 검증이 없었다

위협 모델 공격 fixture를 짜다가 발견했다: 공격 표면 #9("Claude가 scope 밖 수정")의 탐지 통제로
리뷰는 "baseline/result/test 전후 manifest 비교"를 든다. Phase 3.3이 그 비교(`ManifestDiff`)는
만들었지만, **그 diff를 실제 `ScopeRules`(`allowed_path_rules`/`forbidden_path_rules`)와 대조하는
코드는 어디에도 없었다** — scope 위반 판정은 전적으로 Codex Verifier 자신의 자유 텍스트
판단(`VerificationResult.scope_violations`)에 맡겨져 있었다. 이건 Phase 8이 evidence_refs에
대해 이미 한 번 발견하고 고친 것과 정확히 같은 종류의 구멍이다(모델의 자기 보고를 유일한
근거로 삼지 말 것).

`execution/scope_guard.py::find_scope_violations`를 새로 만들어 이 구멍을 메웠다 — glob
매칭기(`**`/`*`/`?` 지원, gitignore 스타일)를 직접 구현해야 했는데(이 코드베이스에 지금까지
경로 패턴을 실제 파일 경로에 매칭하는 유틸이 전혀 없었다 — `policy/paths.py`의
`intersect_scope`는 패턴 문자열 자체의 집합 연산이지 실제 경로 매칭이 아니다), 표준
라이브러리만으로 처리했다(새 의존성 없음).

**하지만 `freeze_and_validate`/`run_verification`에는 연결하지 않았다.** 세션이 이미 매우
길었고, Phase 9/10에서 이미 검증을 마친 오케스트레이터 코드를 이 시점에 다시 건드리는 게
회귀 위험 대비 이득이 크지 않다고 판단했다. 대신 `find_scope_violations`는 독립적으로
완성되고 테스트됐고(`tests/unit/test_scope_guard.py`, `tests/security/test_attack_fixtures.py`의
end-to-end fixture), 실제 파이프라인에 연결하는 건 명시적인 후속 작업으로 남긴다: (1)
`FrozenValidationResult`에 `scope_violations: list[str]` 필드 추가 + `freeze_and_validate`에서
자동 계산, (2) `application/verification.py`의 `build_verifier_prompt`에 이 결정론적 결과를
명시적으로 포함, (3) `find_missing_evidence_violations`와 나란히 동작하는
`find_deterministic_scope_violations` 게이트를 `run_verification`에 추가해서 PASS 주장을
MANUAL_REVIEW로 낮추는 것 — Phase 8이 evidence에 대해 이미 한 것과 정확히 같은 패턴.

## 위협 모델 공격 fixture (`tests/security/test_attack_fixtures.py`)

공격 표면별 통제 표의 행을 그대로 이름 붙여서, 이미 만들어진 실제 통제를 end-to-end로
증명했다(mock 없이): path traversal(`RelativePath` pydantic validator가 `../`/절대경로를
모델 생성 시점에 거부), shell injection(마커 파일로 실제 shell 해석이 안 일어남을 증명 —
기존 `test_command_broker.py`의 argv-shape 증명과는 다른 각도), secret 유출(명령 출력에 가짜
GitHub 토큰을 심고 저장된 artifact에서 실제로 redact됐는지 확인), cloud metadata SSRF(ceiling이
그 도메인을 명시적으로 허용해도 hard-coded invariant가 이긴다는 것), 과도한 capability
요청(raw_shell), 출력 폭주 cap(`stdout_truncated`가 실제로 5MB 중 10KB만 남기는지 — 여기서
`output_cap_exceeded`와 `stdout_truncated`가 서로 다른 걸 의미한다는 걸 다시 확인했다:
전자는 "cap 때문에 강제 종료했다", 후자는 "캡처된 바이트가 실제로 잘렸다" — 빠르게 끝나는
프로세스는 강제 종료 전에 스스로 끝나버려서 전자는 False일 수 있다), 그리고 새로 만든
scope guard의 end-to-end 증명.

## Recovery drill (`tests/security/test_recovery_drill.py`)

두 시나리오: (1) `PREPARING_WORKSPACE` 중 크래시 시뮬레이션 → `run_recovery_scan` →
`RECOVERY_REQUIRED` → CLI `status`로 pending reason 확인 → `resume --to EXECUTING`는 거부됨
(state 기계가 그 target을 애초에 허용 안 함) → `resume --to PREPARING_WORKSPACE`는 성공. (2)
`PLANNING` 중 크래시(host-side lease 없음) → 깔끔하게 `retriable=True` `FAILED`로 귀결,
막히거나 자동 재시도되지 않음을 CLI `report`로 확인. Phase 12(CLI)와 Phase 13(recovery scan)을
실제로 엮은, 이 세션에서 유일하게 두 phase의 산출물을 함께 구동하는 통합 테스트다.

## 정직하게 남긴 gap 총정리

- **ContainerSandbox 실제 실행 미검증** (위 참고) — daemon을 켜면 같은 테스트 스위트가 실제
  컨테이너 실행 경로까지 검증할 것이다(`test_container_backend_matches_real_docker_availability_on_this_host`가
  daemon이 켜지는 순간 자동으로 실제 경로를 태운다).
- **WSL2 미구현.**
- **scope violation 검증이 오케스트레이터에 연결 안 됨** (위 참고, 다음 후속 작업으로 명시).
- **`check_base_revision_stale`이 영속화된 Run에 자동으로 연결 안 됨** — Phase 12 문서에서 이미
  지적한 것과 같은 이유: `TaskContract`(그 안의 `repository.base_commit_sha`/`target_ref`)가
  SQLite 어디에도 저장되지 않는다. 순수 함수 자체는 완성/테스트됐지만, `run_recovery_scan`이
  "이 stale Run이 원래 무슨 base revision을 쓰고 있었는지" 알 방법이 없다.
- **Orphan child process 종료 미구현** — PID+시작 시각+Job Object identity를 지속시키는 테이블이
  없어서, 정말로 크래시로 고아가 된 자식 프로세스를 다른 프로세스가 찾아서 죽이는 건 이번
  phase에서 구현하지 않았다. `application/recovery.py` 상단 docstring에 왜 안 했는지, 뭐가
  필요한지 명시.
- **Fault injection/concurrency 테스트**는 이번 phase에서 새로 대규모로 만들지 않았다 — 다만
  Phase 2.1(동시 상태 갱신 optimistic concurrency), Phase 3.1(concurrent worktree lock),
  Phase 3.2(process kill/timeout)이 각자 자기 phase에서 이미 이런 테스트를 갖고 있었다(예:
  `test_sqlite_persistence.py`의 concurrent update 테스트, `test_workspace.py`의 lock timeout
  테스트). 이번 phase가 새로 추가한 건 그 위에 얹는 recovery-scan 레벨의 통합 시나리오뿐이다.
- **distributed scheduler는 로드맵이 명시적으로 비범위.**
