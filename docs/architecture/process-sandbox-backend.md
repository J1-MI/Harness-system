# Process and Sandbox Backend — Phase 3.2

`execution/process.py`(argv 러너 + Job Object 트리 종료), `execution/sandbox.py`
(SandboxBackend 추상화), `execution/command_broker.py`(Command Catalog)의 설계 결정.
로드맵 범위: "argv runner, env scrub, timeout, Job Object/process group, capability probe".
Provider SDK 연동은 비범위. Handoff 프롬프트 없이 로드맵 행 + 섹션 3/8을 스펙으로 설계했다.

## 이 Phase가 주는 것과 주지 않는 것

`TrustedLocalSandbox`는 **격리를 제공하지 않는다.** 그냥 native subprocess다. 주는 것은
timeout, 출력 상한, 프로세스 트리 강제 종료, 명시적으로 정제된 환경변수뿐이다. 파일시스템도
네트워크도 전혀 막지 않는다 — B-01을 다시 상기: "Git worktree는 변경 격리 수단이지 보안
샌드박스가 아니다"와 같은 이유로, 지금 이 커맨드 실행기도 보안 경계가 아니다. `IsolationBackend`
enum에 `WSL2`/`CONTAINER`를 이름만 올려두고 `probe_capabilities()`는 항상 `TRUSTED_LOCAL`만
반환한다 — 이 머신에 WSL2와 Docker가 실제로 설치되어 있어도, 그 경로를 구현·테스트하지 않은
채로 "지원한다"고 주장하지 않는다(리뷰: "요구 capability가 없으면 downgrade하지 말고
CAPABILITY_MISMATCH로 fail closed"). `get_sandbox_backend()`에 `WSL2`/`CONTAINER`를 요청하면
`UnavailableSandboxError`로 fail closed한다. 실제 컨테이너/WSL2 백엔드는 로드맵 Phase 13
(Hardening and Recovery)에서 다룬다.

## Windows Job Object로 프로세스 트리를 통째로 죽인다

`Popen.kill()`은 직계 자식만 죽인다. 자식이 만든 손자 프로세스(특히 `close_fds=True`로 분리된
프로세스)는 그대로 남는다 — 이게 "child leak"이다. `_WindowsJob`은 `ctypes`로 `kernel32`의
Job Object API를 직접 호출한다(pywin32 등 새 의존성을 추가하지 않기 위해): `CreateJobObjectW`
→ `SetInformationJobObject`로 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 설정 → 자식 프로세스를
`AssignProcessToJobObject`. Windows는 기본적으로 Job에 속한 프로세스가 만드는 모든 자손을
자동으로 같은 Job에 포함시키므로(명시적 breakaway가 없는 한), `TerminateJobObject` 한 번으로
전체 트리가 죽는다. `tests/unit/test_process.py`가 실제로 조부모→부모→자식 3단 프로세스를
띄우고 heartbeat 파일 갱신이 멈추는지로 이를 증명한다(프로세스 목록 파싱 대신 파일 타임스탬프로
확인 — `psutil` 등 추가 의존성 없이 신뢰성 있게 검증 가능).

**타임아웃/출력초과로 죽였을 때뿐 아니라, 부모가 스스로 exit 0으로 깨끗하게 끝났을 때도 Job을
정리한다.** 분리된 손자 프로세스를 남겨두고 부모만 "성공적으로" 종료하는 것도 leak이기 때문이다
— `test_run_process_kills_grandchildren_on_clean_exit`가 이 경로를 별도로 검증한다.

POSIX는 `os.setsid`로 새 process group을 만들고 `os.killpg`로 그룹 전체를 죽이는, 동일한 원리의
더 단순한 구현을 쓴다(실제로는 이 코드베이스 개발이 Windows에서만 이루어져 POSIX 경로는 코드
리뷰 수준으로만 검증했고 CI에서 실행되지 않았다 — 실행 환경이 POSIX인 곳에서 실제로 검증이
필요하다).

## 출력 상한: 자르고 죽인다

"log flood" 방어는 두 단계다. 리더 스레드가 스트림당 `max_output_bytes`까지만 보관하고, 그
이상은 버리면서 계속 읽어서(파이프가 가득 차 자식이 블록되는 걸 방지) `cap_event`를 세팅한다.
메인 루프가 그 이벤트를 보면 **즉시 프로세스 트리를 죽인다** — 상한을 넘긴 프로세스를 계속
살려두고 자르기만 하지 않는다(섹션 10: "조용히 잘라내고 PASS해서는 안 된다"의 process 계층
버전). `test_run_process_kills_on_output_cap_and_truncates`가 무한히 stdout을 쏟아내는
프로세스가 상한을 넘자마자(타임아웃 30초를 기다리지 않고) 죽는 것을 확인한다.

## Command Broker: 등록된 ID만, argv는 토큰 단위로만 채운다

`CommandCatalog`에 없는 `command_id`는 `UnknownCommandError`로 즉시 거부한다 — "raw command
탐색 결과를 그대로 실행"하는 경로는 없다. `resolve_argv()`는 `argv_template`의 각 토큰에
`str.format_map()`을 토큰 단위로 적용한다: 파라미터 값은 어떤 토큰의 리터럴 내용으로만
들어가고, **그 값 자체가 다시 포맷 문법이나 쉘 문법으로 해석되지 않는다.**
`test_shell_metacharacters_in_parameters_are_never_interpreted`가 `; rm -rf / #`,
`$(whoami)`, `` `whoami` ``, `hello && echo injected` 같은 값들을 실제로 자식 프로세스에
전달해서 `sys.argv[1:]`가 정확히 한 개짜리 리스트로 그 문자열을 그대로 담고 있음을 증명한다
(H-04).

`execute_command()`의 "env scrub"은 `available_env`(호출자가 제공 가능한 전체 환경, 보통
`os.environ`)에서 `CommandSpec.env_allowlist`에 있는 키만 골라 자식에게 넘긴다 — 기본값은
"아무것도 안 준다"이지, "전부 준다"가 아니다.

## 비범위

WSL2/Docker 백엔드 구현(Phase 13), `CommandSpec.network_class` 실제 강제(trusted_local은
네트워크를 전혀 막지 못한다 — 강제하려면 real sandbox backend가 필요), `command_catalog.yaml`
설정 로딩(현재 `CommandCatalog`는 순수 in-memory 등록만 지원), Provider SDK/helper process
연동, ActiveProcessLimit 등 세밀한 리소스 상한.
