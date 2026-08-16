# SQLite State and Journal — Phase 2.1

`persistence/migrations.py`, `persistence/sqlite.py`의 설계 결정. 로드맵 범위: "migrations,
Run/Task/Invocation store, journal, optimistic version". Blob store는 Phase 2.2로 미룬다.

## 왜 이 다섯 테이블만 있는가

섹션 10의 "권장 테이블" 목록은 시스템 전체(모든 Phase)의 최종 모습이다. 이번 Phase는 로드맵
행이 명시한 범위만 만든다: `schema_migrations`, `runs`, `tasks`, `agent_invocations`,
`journal_entries`, 그리고 `FAILED` 전이에 항상 동반되는 `failure_records`. `task_contracts`,
`agent_sessions`, `policy_decisions`, `approvals`, `workspace_leases`, `artifacts`,
`evidence`, `command_runs`, `verifications`, `rework_contracts`는 각각 나중 Phase(2.2 blob
store, 3.x execution, 4 policy)에서 다룬다.

## Migration 전략

각 migration은 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`로 작성해
스크립트 자체를 멱등으로 만들고, `schema_migrations` 테이블에 적용된 version만 기록해
같은 migration을 두 번 적용하지 않는다. SQLAlchemy는 리뷰의 권고대로 아직 도입하지 않았다
(리뷰: "여러 DB backend, 다중 호스트 scheduler, 복잡한 query/report, PostgreSQL 전환,
repository 구현 2종 이상 중 하나가 생기기 전까지 재검토하지 않는다").

## 원자적 전이: `apply_transition`

섹션 10의 pseudocode를 그대로 구현했다:

```
BEGIN IMMEDIATE
  SELECT state_version
  validate expected state/version      -> ConcurrentModificationError
  (재검증은 Phase 1.2의 순수 transition_run()을 재사용)
  INSERT journal entry                 -> hash chain (previous_entry_hash)
  UPDATE run SET state=?, state_version=state_version+1 WHERE state_version=?
  INSERT failure_record (있으면)
COMMIT / 예외 시 ROLLBACK
```

핵심 설계 포인트:

- **정책 로직을 중복 구현하지 않는다.** `apply_transition`은 자체적으로 "이 전이가 허용
  되는가"를 판단하지 않고, Phase 1.2의 `application.transitions.transition_run()`을 그대로
  호출한다. 상태 기계 규칙은 오직 한 곳에만 존재한다.
- **Optimistic concurrency는 두 번 검사한다.** 트랜잭션 시작 직후 `state_version`을 비교해
  빠르게 실패시키고, `UPDATE ... WHERE state_version = ?`의 `rowcount`도 다시 확인한다.
  `BEGIN IMMEDIATE`가 이미 writer를 직렬화하므로 이론상 두 번째 검사는 항상 통과해야 하지만,
  단일 SQL 문에 조건을 넣어두면 가정이 깨졌을 때 조용히 넘어가지 않고 즉시
  `ConcurrentModificationError`로 fail-closed한다.
- **crash는 전부 ROLLBACK으로 이어진다.** 함수 전체가 `try/except Exception: ROLLBACK; raise`
  로 감싸여 있어서, journal INSERT는 성공했는데 run UPDATE는 실패하는 것 같은 부분 반영을
  허용하지 않는다. `tests/unit/test_sqlite_persistence.py`가 `UPDATE runs`에서 강제로
  예외를 던지는 커넥션 서브클래스로 이를 검증한다(`sqlite3.Connection`은 C 타입이라 클래스
  레벨 monkeypatch가 불가능해서 서브클래싱했다).
- **journal entry hash 계산은 자기 자신을 제외한다.** `entry_hash`가 없는 상태로 모델을
  만든 뒤 `compute_model_digest(..., exclude_fields={"entry_hash"})`로 계산하고, 그 결과를
  `model_copy(update=...)`로 채워 넣는다 — Phase 1.1 `digests.py`의 규칙을 그대로 따른다.

## 직렬화 규칙

중첩된 Pydantic 모델(`BudgetRequest`, `BudgetUsage`, `ProviderError`, `UsageRecord` 등)은
컬럼에 `model_dump_json()`/`model_validate_json()`으로 통째로 저장한다 — 이번 Phase에서
그 안쪽 필드로 쿼리할 필요가 없기 때문에 정규화하지 않았다. 필요해지면(예: budget 사용량으로
검색) 그때 컬럼을 쪼갠다. 시간은 전부 UTC ISO 8601 문자열로 저장한다.

## 비범위

Blob/artifact 저장(Phase 2.2), `AgentSession`/`PolicyDecision`/`Approval`/`WorkspaceLease`/
`TaskContract`/`Verification`/`ReworkContract`/`CommandRun` 등 나머지 테이블, 실제 크래시
복구 절차(startup recovery, lease 만료 처리)는 전부 이후 Phase다.
