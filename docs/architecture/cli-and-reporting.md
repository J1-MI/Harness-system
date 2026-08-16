# CLI and Reporting — Phase 12

`interfaces/cli.py` (+ `interfaces/_demo_pipeline.py`) and `application/reporting.py`. 로드맵
범위: "run/status/approve/reject/resume/cancel/report/cleanup, final manifest", 비범위: "Web UI".
target: "Typer CLI, report builder" — 그래서 report builder(`application/reporting.py`)를 CLI와
분리된 독립 모듈로 만들었다. 테스트 기준: "crash-safe commands, ambiguous approval 방지".

## "crash-safe": 커맨드마다 새 연결, 하나의 원자적 작업, 그리고 종료

모든 커맨드가 정확히 같은 모양이다: `_open(db)`로 새 SQLite 연결을 열고, Phase 2.1의 이미
원자적인 함수(`apply_transition`) 하나 또는 `application/reporting.py`의 읽기 전용 조회 하나를
수행하고, 연결을 닫는다. 프로세스 안에 커맨드 사이로 이어지는 상태가 전혀 없다 — 그래서
커맨드 실행 중 프로세스가 죽어도 "그 원자적 트랜잭션이 커밋됐거나 안 됐거나" 둘 중 하나뿐이다
(`BEGIN IMMEDIATE`는 Phase 2.1이 이미 보장). 새 crash-safety 메커니즘을 만들지 않고 Phase
2.1이 이미 준 보장을 그대로 노출한 것뿐이다.

## "ambiguous approval 방지": 실제로 기다리고 있는지 먼저 확인하고, 뭘 승인하는지 보여준다

`approve`/`reject`는 Run의 `state`가 `AWAITING_APPROVAL`/`AWAITING_FINAL_APPROVAL`/
`AWAITING_MANUAL_REVIEW` 중 하나가 **실제로 아니면** 아무것도 하지 않고 거부한다 — 상태를
추측하지 않는다. 그리고 실행하기 전에 마지막 journal entry에 실제로 기록된 `PendingAction`의
`description`을 그대로 출력해서, 사용자가 "내가 지금 뭘 승인/거부하는지" 확인할 수 있게 한다.
이 description은 새로 만든 게 아니라 Phase 1.2/2.1이 전이 시점에 이미 기록해둔 값을 그대로
읽는 것이다 — approve 커맨드가 "왜 기다리고 있는지"를 다시 판단하거나 지어내지 않는다.

## `resume`은 `RECOVERY_REQUIRED` 전용 — 상태 기계가 이미 정의한 안전한 target만 허용

`resume --to <STATE>`는 Run이 `RECOVERY_REQUIRED`가 아니면 거부하고, `--to`가 Phase 1.2의
`RECOVERY_RESTART_TARGETS`(예: `EXECUTING`은 의도적으로 빠져있다 — "crash가 발생하면 자동으로
같은 명령을 재실행해서는 안 된다")에 없으면 유효한 target 목록을 보여주며 거부한다. 새 recovery
로직을 만들지 않았다 — Phase 1.2가 이미 "안전하게 재시작 가능"이라고 선언한 상태 집합을 CLI가
그대로 강제할 뿐이다. 실제 Recovery Coordinator(lease 만료 탐지, orphan process 정리 등)는
Phase 13 몫이다.

## `cleanup`/`purge` 구분을 코드로 그대로 옮겼다

리뷰: "cleanup: process/worktree/temp/branch 정리, purge: evidence/artifact/journal 데이터
삭제." `cleanup`은 terminal Run의 worktree/branch만 지운다(`execution.workspace.cleanup_worktree`
재사용, 새 로직 없음). `--purge`는 `--yes` 없이는 절대 실행되지 않고, evidence artifact blob을
지우되 "삭제 시 tombstone과 digest metadata는 남긴다"를 그대로 지켜서 `<blob>.tombstone` 파일에
purge 시각과 원래 digest를 남기고 journal/evidence record 자체는 전혀 건드리지 않는다.

worktree 위치를 별도 lease 테이블 없이 찾기 위해 `execution/workspace.py`에
`worktree_path_for()`/`branch_ref_for()`를 새로 노출했다 — `create_worktree`가 쓰던 것과
정확히 같은 결정적 경로 계산(`data_root/workspaces/<repository_id>/<run_id>`)을 재사용 가능한
함수로 뽑아낸 것뿐이다(중복 제거 겸 CLI가 재사용). `WorkspaceLease` 자체를 저장하는 테이블은
없다는 게 이번 phase에서 드러난 실제 gap이다 — 아래 "비범위/한계" 참고.

## `report`는 서명이 아니라 canonical digest다 — 정직하게 그렇게 문서화했다

리뷰의 "감사의 한계" 섹션은 "서명된 final report manifest"를 강한 감사 보장을 원할 때 추가할
수 있는 여러 선택지 중 하나로 나열한다(OS keychain HMAC key, 외부 append-only anchor 등과
나란히). `FinalManifest.manifest_digest`는 이 프로젝트 전체가 이미 쓰는 것과 같은
`compute_model_digest` 패턴이다 — 자기 자신을 제외한 나머지 필드의 canonical digest일 뿐,
암호학적 서명이 아니다. `FinalManifest` 모델 자체의 docstring에 이 구분을 명시했다. 실제
서명이 필요해지면 별도 키 인프라가 필요하다는 것도 명시.

## `demo` 커맨드: 데모 경로다 — 실제 Claude/Codex 연동은 이번 phase가 아니다

(2026-08-15 Codex implementation review B-06 반영: 원래 이 커맨드 이름이 `run`이었는데,
실제로는 Fake Provider만 실행하면서 이름이 `run`인 건 스스로 뭘 하는지 오해하게 만드는
overclaim이라는 지적을 받아 `demo`로 이름을 바꿨다. `pyproject.toml`에
`[project.scripts] harness = "agent_harness.interfaces.cli:main"` entry point도 이때
추가했다 — 이전에는 `pip install -e .` 이후에도 `harness` 실행 파일이 안 만들어졌다.)

`interfaces/_demo_pipeline.py`는 `application.orchestrator.run_task_pipeline`을 실제로
호출하지만, Planner/Worker/Verifier 전부 `FakeAgentProvider`다 — TaskContract·WorkerResult는
CLI가 결정적으로 만들고, Verifier는 Phase 9 테스트에서 썼던 것과 같은 트릭(자기 프롬프트에
박힌 실제 evidence ID를 읽어서 인용)으로 항상 PASS를 낸다. 이건 "CLI가 진짜 오케스트레이터에
제대로 연결돼 있다"는 걸 증명하는 데모 경로이지, 실제 자동화가 아니다. 실제 Claude/Codex 연동은
config 파일 로더(`config/policy.yaml`, `command_catalog.yaml` 등 — 리뷰의 디렉터리 구조에는
나오지만 어떤 phase에도 "이걸 만들어라"고 명시적으로 할당되지 않은 진짜 gap)와 credential
전달, 그리고 TaskContract/PolicyDecision 영속화(진짜 `run` 커맨드가 crash-safe하게 재개되려면
필요)까지 필요해서, 여전히 별도 후속 작업으로 남아 있다 — 지금 CLI엔 `run`이라는 이름의
커맨드 자체가 없다.

## 비범위/한계 (정직하게 남겨둔 gap)

- **worktree/lease/contract 영속화가 없어서 진짜 cross-process resume은 안 된다.** `approve`/
  `reject`는 Run의 **상태 기계 전이**는 정확하게 수행하지만(그래서 audit·상태 추적으로는 완전히
  유효하다), 실제로 멈춰있던 파이프라인(Worker 재호출 등)을 새 프로세스에서 이어서 실행하지는
  않는다 — `TaskContract`/`PolicyDecision`/`WorkspaceLease`를 저장하는 테이블이 Phase 2.1에
  없기 때문이다. `run_task_pipeline`은 한 프로세스 안에서 콜백으로 승인을 기다리는 모델이라(
  Phase 9/10 설계), 별도 `approve` 커맨드가 그 콜백을 대신 채워줄 수 없다. 이 gap을 메우려면
  최소 TaskContract 영속화 + resumable orchestrator가 필요한데, 이건 로드맵이 Phase 12에
  명시적으로 할당한 범위(target: "Typer CLI, report builder"뿐)를 넘어선다.
- Web UI, 실제 config/*.yaml 로더, 실제 Claude/Codex 자격증명 CLI 플래그, 여러 Run 동시 처리,
  Recovery Coordinator(Phase 13)는 전부 비범위.
