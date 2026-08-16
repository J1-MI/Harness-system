# Frozen Result and Host Validation — Phase 3.3

`execution/validation.py`(파일 manifest 기반 변경 증거) + `execution/evidence.py`(freeze →
승인된 host check 실행 → HOST_OBSERVED evidence 조립)의 설계 결정. 로드맵 범위:
"baseline/result manifest, diff artifact, approved checks, test mutation 감지". Codex
verifier는 비범위. Handoff 프롬프트 없이 로드맵 행 + 섹션 10(M-05)을 스펙으로 설계했다.

## Git diff가 아니라 파일시스템 전체 스캔

M-05를 그대로 구현했다: `build_file_manifest()`는 Git을 전혀 모른다. `os.walk`로 워크트리를
직접 순회해서 tracked든 untracked든 ignored든 상관없이 모든 파일을 본다. `.git/` 디렉터리만
제외한다. 이러면 "diff를 canonical 증거로 쓰면 untracked/ignored/binary/symlink 변화가
누락될 수 있다"는 문제가 구조적으로 사라진다 — 애초에 diff를 신뢰의 원천으로 쓰지 않는다.
`git diff --binary`는 여전히 유용하지만 사람이 읽기 위한 부가 artifact일 뿐 판정 근거가
아니다(이번 Phase는 그 display artifact 자체는 만들지 않았다 — 아래 비범위 참고).

## Symlink: 절대 따라가지 않는다

`os.walk(..., followlinks=False)`와 `lstat()`(not `stat()`)을 쓴다. symlink는 `sha256=None`,
`symlink_target`에 링크가 가리키는 경로 문자열만 기록하고 **절대 열어서 읽지 않는다**.
`test_manifest_symlink_to_outside_worktree_is_not_read`가 워크트리 밖을 가리키는 symlink를
만들어서, manifest가 그 대상 내용을 전혀 건드리지 않았음을 증명한다 — path traversal/symlink
escape 방어(H-05)의 evidence 계층 버전이다.

## Test mutation 탐지

`find_test_mutations()`는 baseline과 result manifest의 diff에서 "수정되었거나 삭제된" 파일 중
`test_path_patterns`(glob)에 매치하는 것만 골라낸다. Worker가 실패하는 테스트를 고쳐서
통과시키는 대신 테스트 자체를 약화시키거나 지우는 시나리오를 잡기 위한 것이다 — 이게 바로
`WorkerResult.reported_tests`(Worker 자기 보고)를 신뢰하지 않고 `VerificationResult`가 이
frozen snapshot 기반 host-observed evidence를 봐야 하는 이유다(Phase 1.1에서 이미 다뤘던
"provider-reported vs host-observed" 원칙의 실제 적용 사례).

## Freeze 순서: 먼저 얼리고, 그다음 승인된 검사를 돌린다

`freeze_and_validate()`의 순서는 의도적이다: **result manifest를 먼저 동결**하고 test-mutation
탐지까지 끝낸 뒤에야 승인된 host check 명령(Phase 3.2의 `CommandBroker`)을 실행한다. 그래야
검사 명령 자체가 워크트리를 바꿔도 이미 관찰한 결과 스냅샷을 오염시키지 못한다. Host check가
추가로 파일을 바꾸는 경우는 이번 스냅샷의 범위 밖이며, 그걸 보려면 별도 freeze가 한 번 더
필요하다는 것도 문서로 명시했다.

## Evidence는 자기 자신을 검증한다

`build_command_evidence()`가 만드는 모든 `EvidenceRecord`는 반환하기 *전에*
`domain.validation.assert_evidence_matches_artifact()`로 자기 자신을 검사한다(Phase 2.2에서
만든 그 함수). 이 모듈이 조립 과정에서 digest나 artifact_refs를 잘못 넣는 버그를 냈다면, 그
결과물이 나가기 전에 바로 예외로 드러난다 — 방어적 자기 점검이다.

## 비범위

Codex Verifier 연동(Phase 8), 사람이 읽는 `git diff --binary` display artifact 생성,
acceptance criterion별 `NOT_VERIFIED` 승격 로직(예: 출력이 잘렸을 때 해당 criterion을
자동으로 `NOT_VERIFIED` 처리하는 것 — 지금은 `EvidenceRecord.truncated` 플래그만 세팅하고
그 이후 판단은 하지 않는다), `TaskContract.scope`의 `forbidden_path_rules`와 실제 변경 파일을
대조해 scope violation을 탐지하는 로직(이건 Policy Engine, Phase 4의 몫에 더 가깝다).
