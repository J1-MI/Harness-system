# Agent Harness 구현 결과 요약

**작성일**: 2026-08-15 (2026-08-15 Codex implementation review 대응 반영으로 갱신)
**상태**: 로드맵 13개 phase 코드 전부 작성 완료, 단 **Codex의 독립 검토에서 `REWORK` 판정**을
받았고 그 대응을 거친 상태 — 아래 "Codex 검토 및 대응" 섹션 필독.
**소스 오브 트루스**: `C:\Users\user\Harness\AGENT_HARNESS_ARCHITECTURE_REVIEW.md`
**코드 위치**: `C:\Users\user\Harness\agent-harness`
**테스트 현황**: 693 passed, 2 skipped(opt-in live smoke), schema drift 없음

Codex가 작성한 architecture review(verdict: `REDESIGN_REQUIRED`)의 13-phase 로드맵을
Claude Code가 phase 순서대로 구현했다. Section 11 로드맵 표의 각 행(target / scope /
비범위 / test 기준)을 스펙으로 삼아 phase마다 설계 결정을 내렸고, phase마다
`docs/architecture/*.md`에 "왜 이렇게 만들었는지" 별도 문서를 남겼다.

## Codex 검토 및 대응 (필독)

이 문서가 처음 "13개 phase 전부 완료"라고 선언한 뒤, Codex가 독립적으로 코드를 검토하고
`docs/CODEX_IMPLEMENTATION_REVIEW.md`에 **`REWORK`** 판정을 남겼다 — 핵심 지적: "테스트는
잘 갖춰졌지만 실제 Codex-Claude 하네스로 실행·중단·재개·감사할 수 있는 상태는 아니다."
BLOCKER 7건(Claude tool 권한이 실제로 강제 안 됨, 실패한 host test로도 PASS 가능, 승인
기본값이 자동 승인, 핵심 객체 영속화 없음, CLI 재개가 실제 재개 아님, `run` 커맨드가 사실은
데모, timeout/budget 미집행) 등 총 18건의 findings를 남겼다.

**대응 결과**(`docs/CODEX_REVIEW_RESPONSE.md`에 finding별 상세 기록): BLOCKER 6/7건 해결(1건
부분), HIGH 4/6건 해결(2건은 이미 문서화된 gap 재확인), MEDIUM 5/5건 해결. 아래 이 문서의
나머지 내용(phase별 요약)은 **코드가 무엇을 하도록 작성됐는지**는 여전히 정확하지만, "완료"
여부에 대한 최종 판단은 이 섹션과 `CODEX_REVIEW_RESPONSE.md`를 기준으로 삼아야 한다. 특히:

- **아직 안 된 것**: 진짜 cross-process pipeline 재개(re-entrant step executor 없음 —
  `run_task_pipeline`이 여전히 하나의 monolithic 코루틴), `Approval`/`AgentSession`/
  `WorkspaceLease`/`CommandRun` 영속화, 실제 Provider가 붙은 `harness run` 커맨드(현재는
  `harness demo`만 있음, Fake Provider 전용), Bash tool의 Worker 허용(raw_shell 승인이
  `PolicyGrants`에 반영 안 됨), Container sandbox 실제 실행 검증(daemon 꺼짐).
- **이번에 새로 된 것**: Claude Worker의 tool-call 경로 검증(worktree 밖 파일 접근 차단),
  host test 실행 후 재검증(test_side_effects 탐지), 승인 기본값 fail-closed, TaskContract/
  PolicyDecision/VerificationResult/ReworkContract 영속화(부분), timeout/budget 실제 집행,
  Planner의 authoritative 정보 위조 차단, path traversal 방어, symlink manifest 버그,
  scope_guard 실제 연결 등.

---

## 1. 전체 아키텍처

```
Planner(Codex) → Policy/Approval → Workspace(Git worktree) → Worker(Claude)
    → Freeze/Host-Validate → Verifier(Codex, fresh session) → Final Approval
```

레이어 구조(의존 방향: `interfaces → application → domain`, `application → policy/
providers/execution/persistence`):

```
src/agent_harness/
├─ domain/        # 순수 모델·enum·digest. 바깥 계층에 의존하지 않음
├─ application/    # orchestrator, transitions, verification, rework, recovery, reporting
├─ policy/        # 결정론적 정책 엔진 + 승인
├─ execution/      # git, process/sandbox, command broker, evidence, retention, incident, scope_guard
├─ providers/      # Claude/Codex 어댑터 + Fake/Replay + registry
├─ persistence/    # SQLite + 원자적 전이 + content-addressed blob store
└─ interfaces/     # Typer CLI
```

---

## 2. Phase별 요약

### MVP 구간 (Phase 1.1 ~ 10) — 리뷰가 명시한 MVP 종료선은 Phase 10

| Phase | 핵심 산출물 | 한 줄 요약 |
|---|---|---|
| 1.1 Executable Contract Kernel | `domain/` 전체, canonical digest, Provider-neutral Protocol | 모든 phase가 재사용하는 스키마 단일 원천 |
| 1.2 State Machine Kernel | `application/transitions.py` | 16개 LifecycleState, 순수 함수, side effect 없음 |
| 2.1 SQLite State and Journal | `persistence/sqlite.py`, `migrations.py` | `BEGIN IMMEDIATE` 원자적 전이 + hash-chain journal |
| 2.2 Artifact and Snapshot Store | `persistence/artifacts.py` | content-addressed blob, 정규식 secret redaction |
| 3.1 Git Workspace Isolation | `execution/git_client.py`, `workspace.py` | hooks/pager/credential-helper 차단된 하네스 전용 Git |
| 3.2 Process and Sandbox Backend | `execution/process.py`, `sandbox.py` | Windows Job Object 기반 process-tree kill |
| 3.3 Frozen Result and Host Validation | `execution/validation.py`, `evidence.py` | git diff 대신 filesystem manifest, symlink no-follow |
| 4 Policy and Approval Engine | `policy/` | requested ∩ ceiling = grants, hard-coded invariant 우회 불가 |
| 5 Provider-neutral Runtime | `providers/registry.py` 등 | Fake/Replay 포함 공용 conformance suite |
| 6 Claude Provider Adapter | `providers/claude.py` | 실 Claude Agent SDK, 실 API 라이브 콜로 버그 2건 발견/수정 |
| 7 Codex Planner Adapter | `providers/codex.py` | 실 Codex SDK, read-only+deny_all 고정, 라이브 콜로 버그 2건 발견/수정 |
| 8 Codex Verifier Adapter | `application/verification.py` | fresh session, evidence-only context, missing-evidence PASS 거부 |
| **9 Dual-Agent Pipeline** | `application/orchestrator.py` | **Phase 1~8을 실제로 엮은 첫 오케스트레이터** |
| **10 Rework Lifecycle** | `application/rework.py` | **제한된 재작업 루프: scope subset, budget, no-progress breaker** |

### Post-MVP 구간 (Phase 11 ~ 13) — 리뷰가 명시적으로 "post-MVP"라고 부르는 범위

| Phase | 핵심 산출물 | 한 줄 요약 |
|---|---|---|
| **11 MCP Governance** | `execution/mcp_gateway.py` | registry+정책+승인+credential broker+audit, `.mcp.json` 절대 안 읽음 |
| **12 CLI and Reporting** | `interfaces/cli.py`, `application/reporting.py` | Typer CLI 8개 커맨드, crash-safe, ambiguous-approval 방지 |
| **13 Hardening and Recovery** | `application/recovery.py`, `execution/{retention,incident,scope_guard}.py` | 복구 조정자, 보존 정책, secret incident, 위협모델 fixture |

---

## 3. 이번 세션에서 새로 완성한 부분 (Phase 9~13) 상세

### Phase 9 — Dual-Agent Pipeline
- `run_task_pipeline()`: CREATED부터 시작해 plan→policy→approval→workspace→worker→
  freeze→host-validate→verify→final-approval을 Phase 1.2 상태기계 그대로 구동.
- TaskContract의 `integrity.canonical_digest`는 Codex가 뭐라고 주장하든 Harness가
  직접 재계산 — "Harness가 Codex 제안을 검증해 생성" 원칙을 코드로 구현.
- Provider는 role별로 registry에서 조회 — Claude/Codex를 직접 import하지 않아서
  Fake E2E와 실제 어댑터가 완전히 같은 코드 경로를 탄다.
- Fake E2E 3종 + opt-in live E2E(이번 세션엔 실행 안 함 — Phase 6에서 이미 확인된
  Anthropic 계정 크레딧 부족 문제가 그대로 재현될 것이 확실해서).

### Phase 10 — Rework Lifecycle
- `build_rework_contract()`: Verifier의 REWORK 판정(자유 텍스트)을 구조화된
  ReworkContract로 변환 — scope subset 검증(`assert_rework_scope_is_subset`),
  attempt budget(`check_rework_budget`), no-progress breaker(`detect_no_progress`,
  두 번 연속 같은 criteria가 실패하면 예산이 남아도 즉시 중단) 3중 가드.
- 오케스트레이터가 `while True` 루프로 확장: REWORK_CONTRACTING→CONTRACT_VALIDATING으로
  돌아가되 **같은 worktree를 재사용**(rolling baseline)하고, capability 확장 요청이
  있으면 승인을 처음부터 다시 받음("기존 승인 재사용 금지").
- 신규 테스트 12개(rework 8 + orchestrator 4: 성공 복구, max iteration 소진,
  no-progress 소진, capability 확장 승인).

### Phase 11 — MCP Governance
- `McpToolCatalog`은 `register()`로만 채워짐 — `.mcp.json`을 읽는 코드가 모듈
  어디에도 없다는 걸 테스트로 직접 증명.
- `invoke_mcp_tool()` 검사 순서: 등록 여부 → role 허용 여부 → **`PolicyGrants.mcp_tools`에
  실제로 있는지**(등록만으로는 부족) → 승인(destructive tool) → input schema →
  rate limit → result size cap.
- 모든 호출(성공/거부 불문)이 audit `EvidenceRecord`를 남기고, credential은 호출
  시점에만 resolve되어 audit에 절대 남지 않음(실제로 블롭을 읽어서 확인).
- 신규 테스트 13개.

### Phase 12 — CLI and Reporting
- Typer CLI: `status / approve / reject / resume / cancel / report / cleanup / run`.
- 모든 커맨드가 자기만의 SQLite 연결을 열고 이미 원자적인 연산 하나만 수행하고
  종료 — 이게 "crash-safe commands"의 전부(새 메커니즘을 만들지 않음).
- `approve`/`reject`는 Run이 실제로 `AWAITING_*` 상태가 아니면 거부하고, journal에
  기록된 실제 `PendingAction.description`을 보여주고서야 실행("ambiguous approval
  방지").
- `FinalManifest`(final report manifest)는 canonical digest이지 암호학적 서명이
  아님을 모델 docstring에 명시(리뷰의 "감사의 한계" 섹션 그대로 인용).
- Windows 환경에서 실제로 발견한 버그: Typer의 `rich` 렌더러가 이 터미널의 cp949
  코드페이지에서 em-dash(—)를 만나 `UnicodeEncodeError` — CLI 텍스트를 전부 ASCII로
  교체해서 해결.
- 신규 테스트 23개(CLI 20 + reporting 3).

### Phase 13 — Hardening and Recovery
- `ContainerSandbox`(Docker) 추가, `docker info` 실제 실행으로 가용성 확인(fail-closed).
  **이 머신은 Docker Desktop이 설치는 됐지만 daemon이 꺼져 있어서, 실제 컨테이너 실행은
  이번 세션에서 검증하지 못했다** — 무거운 백그라운드 서비스를 요청 없이 직접 켜지
  않기로 판단.
- `application/recovery.py`(Recovery Coordinator): Phase 1.2 상태 전이표를 그대로
  존중 — host-side lease를 쥔 4개 상태(PREPARING_WORKSPACE/EXECUTING/FREEZING_RESULT/
  HOST_VALIDATING)만 `RECOVERY_REQUIRED`로, 나머지(PLANNING 등, 상태표에 그 경로가
  아예 없음)는 `retriable=True`인 `FAILED`로 정리.
- `execution/retention.py`: 리뷰가 제시한 보존 기본값(quarantine 7일/completed
  24시간/blob 30일)을 그대로 숫자로.
- `execution/incident.py`: Phase 1.1부터 정의만 되고 한 번도 안 쓰인
  `RedactionStatus.QUARANTINED`를 처음으로 실제 시나리오에 연결.
- **이번 phase에서 새로 발견한 진짜 gap**: 공격 표면 fixture를 짜다가, Worker의 실제
  파일 변경을 TaskContract의 `ScopeRules`와 대조하는 결정론적 코드가 어디에도 없다는
  걸 발견(전적으로 Codex Verifier의 자유 텍스트 판단에 의존 — Phase 8이 evidence_refs에
  대해 이미 한 번 고친 것과 같은 종류의 구멍). `execution/scope_guard.py`로 gitignore
  스타일 glob 매처를 새로 만들어 메웠지만, **오케스트레이터(`freeze_and_validate`/
  `run_verification`)에는 아직 연결하지 않음** — 세션이 이미 매우 길어서 이미 검증
  끝난 Phase 9/10 코드를 다시 건드리는 회귀 위험을 피하기 위한 판단.
- 위협모델 공격 fixture 7개(`tests/security/test_attack_fixtures.py`) + recovery
  drill 2개(`tests/security/test_recovery_drill.py`, Phase 12 CLI와 Phase 13 recovery
  scan을 실제로 엮어 구동하는 이 세션 유일의 테스트).

---

## 4. 정직하게 남긴 gap 목록 (전부 각 phase 문서에 이미 기록됨)

1. **TaskContract/WorkspaceLease/PolicyDecision 영속화 없음** — CLI의 `approve`/`reject`가
   Run의 상태 기계는 정확히 전이시키지만, 별도 프로세스에서 실제 파이프라인(Worker
   재호출 등)을 이어서 실행하지는 못함. `check_base_revision_stale`도 이 gap 때문에
   자동으로 연결 안 됨.
2. **scope_guard가 실제 verify 경로에 연결 안 됨** — 함수 자체는 완성/테스트됐지만
   `run_verification`이 아직 호출하지 않음.
3. **ContainerSandbox 실제 실행 미검증** — daemon이 꺼져 있었음. 켜지면 같은 테스트가
   자동으로 실제 경로를 검증하도록 이미 작성돼 있음.
4. **WSL2 미구현**.
5. **Orphan child process 종료 미구현** — PID+시작시각+Job Object identity를 지속시킬
   테이블이 없음.
6. **실제 Claude/Codex credential을 읽는 config 로더가 CLI에 없음** — `config/*.yaml`
   로더는 어떤 phase에도 명시적으로 할당된 적이 없는, 로드맵 자체의 gap.
7. **distributed scheduler는 로드맵이 명시적으로 비범위**.

---

## 5. 참고 문서

- `docs/architecture/dual-agent-pipeline.md` (Phase 9)
- `docs/architecture/rework-lifecycle.md` (Phase 10)
- `docs/architecture/mcp-gateway.md` (Phase 11)
- `docs/architecture/cli-and-reporting.md` (Phase 12)
- `docs/architecture/hardening-and-recovery.md` (Phase 13)
- 그 외 Phase 1.1~8 문서 전부 `docs/architecture/`에 있음.

git 저장소는 로컬에 초기화만 돼 있고 전부 staged 상태(커밋은 명시적 요청 시에만).
