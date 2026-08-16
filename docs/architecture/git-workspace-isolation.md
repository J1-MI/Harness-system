# Git Workspace Isolation — Phase 3.1

`execution/git_client.py` (harness 전용 Git 실행 환경) + `execution/workspace.py`(lock,
worktree 생성/정리)의 설계 결정. 로드맵 범위: "repo fingerprint, full SHA, locks, branch
naming, dirty policy, cleanup". Agent 실행은 비범위. Handoff 프롬프트가 없어 섹션 3
"Worktree 격리" 정책을 스펙으로 직접 설계했다.

## Worktree는 보안 경계가 아니다

리뷰의 B-01을 다시 강조한다: 이 Phase가 만드는 것은 **버전/동시성 격리**일 뿐이다. 악성
테스트/빌드 스크립트로부터 호스트를 보호하는 OS 샌드박스는 Phase 3.2다. 지금 워크트리
안에서 임의 명령을 실행하면 그 명령은 여전히 사용자 권한으로 호스트 전체에 접근할 수 있다.

## 하네스 전용 Git 실행 환경

`GitClient.run()`은 모든 호출 앞에 고정된 `-c` 오버라이드를 붙인다: pager/editor 비활성화,
`diff.external` 제거, `core.fsmonitor=false`, `credential.helper` 제거,
`core.hooksPath=<빈 디렉터리>`(저장소가 제공하는 어떤 hook도 절대 실행되지 않는다 —
`test_git_client_uses_no_shell_and_disables_hooks`가 실제 post-checkout hook을 심어두고
검증한다), `GIT_TERMINAL_PROMPT=0`(자격증명 프롬프트로 멈추지 않음), `GIT_LFS_SKIP_SMUDGE=1`,
`GIT_CONFIG_NOSYSTEM=1`. 인자는 항상 리스트이고 `shell=False`다(H-04).

`core.autocrlf=false`도 고정했다 — 처음에는 없었는데, `GIT_CONFIG_NOSYSTEM=1`이 Windows
Git의 시스템 레벨 `core.autocrlf=true`를 무력화하면서 `git status`가 방금 체크아웃한 파일을
전부 "수정됨"으로 잘못 보고하는 실제 버그를 테스트 중 발견했다. 호스트마다 다른 system/global
gitconfig에 좌우되지 않고 워크트리 체크아웃이 바이트 단위로 재현 가능해야 한다는 이 Phase의
목표("재현 가능한 worktree") 자체가 고정값을 강제해야 하는 이유였다.

## Repository fingerprint

`compute_repository_fingerprint()`는 `git rev-list --max-parents=0 HEAD`로 얻은 root
commit SHA(들)를 정렬해서 해시한다. Clone 위치나 원격 URL이 달라도 같은 저장소의 root
commit은 동일하므로, `TaskContract.repository.expected_repository_fingerprint`(Phase 1.1
에서 정의만 해두고 아무도 계산하지 않던 필드)를 실제로 채우고 검증하는 첫 코드다.

## Lock과 Lease는 다른 수명을 가진다

`RepoLock`은 `os.open(..., O_CREAT | O_EXCL)`로 구현한 원자적 파일 락이다(Windows/POSIX
동일 동작 — `fcntl`/`msvcrt`처럼 플랫폼별 API를 쓰지 않는다). worktree *생성* 동안만 잡고
바로 놓는다. Run 전체 수명 동안의 예약은 `WorkspaceLease`(Phase 1.1 도메인 모델, 아직
영속화하지 않음 — persistence 연동은 이후 Phase)가 별도로 표현한다. 이 둘을 분리했기 때문에,
이전 Run이 abandoned(크래시)된 worktree를 정리하지 않은 상태에서도 같은 저장소에 대한 새
Run이 lock 경합 없이 진행할 수 있다(`test_lock_is_released_even_though_worktree_was_never
_cleaned_up`).

## 삭제는 항상 명시적이다

`cleanup_worktree()`가 유일한 삭제 경로다. 크래시나 방치된 worktree를 자동으로 찾아 지우는
코드는 이 Phase에 없다 — 섹션 3: "비정상 종료: 삭제하지 않고 RECOVERY_REQUIRED로 격리."
`list_worktrees()`로 존재를 확인할 수 있을 뿐, 아무 함수도 암묵적으로 지우지 않는다.

## Dirty policy

원본 저장소가 dirty해도 `create_worktree()`는 항상 지정된 `base_commit_sha`(커밋된 상태)만
체크아웃한다 — Git worktree 자체가 커밋되지 않은 변경을 포함할 방법이 없으므로, 이는 우리
코드의 로직이라기보다 Git의 근본 동작이다. `test_dirty_source_repo_does_not_leak_into
_worktree`가 이를 명시적으로 증명한다. dirty 변경을 대상으로 삼고 싶다면(섹션 3의 "sealed
snapshot" 옵션) 이후 Phase에서 별도 승인 절차와 함께 구현한다.

## Submodule과 LFS 기본값

`git worktree add`는 기본적으로 submodule을 초기화하지 않는다(우리가 특별히 막은 게 아니라
Git의 기본 동작) — `test_submodule_is_not_initialized_by_default`가 `.gitmodules`는
존재하지만 서브모듈 디렉터리는 비어 있음을 확인한다. LFS는 `GIT_LFS_SKIP_SMUDGE=1`로 smudge를
막아 포인터 텍스트만 남긴다(`test_lfs_objects_are_not_smudged_by_default`, git-lfs가 설치된
환경에서만 실행).

## 비범위

Agent 실행, 실제 sandbox/network 차단(Phase 3.2), `WorkspaceLease`의 SQLite 영속화, submodule
/LFS 다운로드 승인 절차, `.git/config`까지 비신뢰로 봐야 하는 저장소를 위한 mirror 생성,
retention 정책에 따른 자동 cleanup 스위퍼는 전부 이후 Phase다.
