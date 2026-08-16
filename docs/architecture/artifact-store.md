# Artifact and Snapshot Store — Phase 2.2

`persistence/artifacts.py` (blob bytes) + `persistence/sqlite.py`의 artifact/context_snapshot
/evidence 확장 (metadata) + `domain/validation.py::assert_evidence_matches_artifact`의 설계
결정. 로드맵 범위: "content-addressed blob, redaction, caps, context snapshot". 이 Phase에도
별도 handoff 프롬프트가 없어 로드맵 행 + 섹션 10의 Artifact Store 레이아웃/쓰기 순서를 스펙
삼아 직접 설계했다.

## bytes와 metadata의 분리

`persistence/artifacts.py`는 파일시스템만 다루고 SQLite를 전혀 모른다.
`persistence/sqlite.py`는 `artifacts`/`context_snapshots`/`evidence` 테이블에 digest·ref·
provenance만 저장하고 원본 바이트는 절대 컬럼에 넣지 않는다 — 섹션 10: "큰 파일 내용은
Artifact Store를 참조한다." 두 계층을 잇는 유일한 값은 `Artifact.content_digest`다.

## 쓰기 순서 (섹션 10 그대로)

`write_blob()`: 크기 상한 검사(초과 시 staging 파일도 만들지 않고 즉시 거부) → redaction →
digest 계산 → `staging/<uuid>.tmp`에 쓰기 → `flush`+`fsync` → 같은 볼륨(`data_root` 하위) 안에서
`os.replace()`로 원자적 rename → 실패 시 staging 파일 정리. `blobs/sha256/<hex[:2]>/<hex>`
목적지에 이미 파일이 있으면(dedup) rename을 건너뛰고 staging 파일만 지운다.

## Quota: 자르지 않고 거부한다

크기 상한을 넘으면 `ArtifactQuotaExceededError`를 던지고 **아무 것도 디스크에 남기지 않는다**
(staging 파일도 생성 전에 거부). 섹션 10: "조용히 잘라내고 PASS해서는 안 된다"를 Artifact
계층에서는 "잘라서 저장"이 아니라 "저장 자체를 거부"로 구현했다 — truncation은 `EvidenceRecord
.truncated` 필드가 있는 evidence 계층의 책임이지, byte-addressed 저장소인 Artifact 계층의
책임이 아니라고 판단했다.

## Corrupted digest 탐지

`read_blob()`은 읽은 뒤 다시 해시를 계산해 `Artifact.content_digest`와 비교한다. 디스크에서
파일이 사후 변조되면(관리자 권한 공격자, 디스크 오류 등) `CorruptedArtifactError`로 fail
closed한다 — 섹션 8의 감사 한계("SQLite journal hash chain은 동일 권한 공격자의 재작성까지
막지는 못한다")를 완전히 해결하지는 못하지만, 최소한 조용한 데이터 손상은 탐지한다.

## Redaction

정규식 기반 최소 구현(`sk-…`, `ghp_…`, AWS `AKIA…`, Slack `xox…`, PEM private key 블록)이며
`TEXT`/`JSON`/`LOG`/`DIFF` media kind에만 적용한다. `BINARY`는 스캔하지 않는다 — 바이너리에서
정규식 매칭은 신뢰할 수 없고 오히려 파일을 깨뜨릴 수 있다. 이건 방어의 마지막 층일 뿐이고,
1차 방어는 섹션 8의 원칙(Provider credential을 애초에 워크스페이스 프로세스 환경에 주입하지
않는 것)이라는 점을 코드 주석에도 남겼다.

## Evidence ↔ Artifact 무결성

`assert_evidence_matches_artifact(evidence, artifact)`는 두 가지를 검사한다:
`evidence.content_digest == artifact.content_digest`, 그리고
`artifact.artifact_id in evidence.artifact_refs`. Evidence가 실제로 존재하는 Artifact를
가리키는지, 그 Artifact가 정말 Evidence가 주장하는 내용인지를 순수 함수로 검증한다 — I/O 없이
이미 로드된 두 객체만 비교한다.

## 비범위

실제 Git diff/테스트 실행이 만들어내는 진짜 evidence(Phase 3.3), `AgentSession`/
`PolicyDecision`/`Approval`/`WorkspaceLease`/`TaskContract`/`Verification`/`ReworkContract`/
`CommandRun` 테이블, orphan staging sweeper(주기적 정리 프로세스), retention/purge 정책은
전부 이후 Phase다.
