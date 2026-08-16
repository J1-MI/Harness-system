"""argv-only process execution with resource limits (Phase 3.2).

No shell is ever invoked here: ``subprocess.Popen`` always receives an
argv list with ``shell=False`` (H-04 — shell injection). This module
knows nothing about ``CommandSpec`` catalogs or sandbox profiles;
``execution/command_broker.py`` and ``execution/sandbox.py`` build on
top of it.

Whole-process-tree termination uses a Windows Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so that even grandchildren spawned
by the child (detached or not) die when we kill it — a plain
``Popen.kill()`` only kills the direct child and can leak descendants
(the "child leak" scenario this phase's tests target). POSIX uses a new
process group (``os.setsid`` + ``os.killpg``) for the same effect.
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ProcessLimits",
    "ProcessResult",
    "run_process",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: float
    max_output_bytes: int


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    output_cap_exceeded: bool
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    started_at: datetime
    completed_at: datetime


# ---------------------------------------------------------------------------
# Windows Job Object (process-tree kill)
# ---------------------------------------------------------------------------


class _WindowsJob:
    """Wraps a Job Object with KILL_ON_JOB_CLOSE. No-op stub off Windows."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    def __init__(self) -> None:
        self._handle: int | None = None
        if sys.platform != "win32":
            return

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _WindowsJob._JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _WindowsJob._IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job_handle,
            self._JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job_handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

        self._kernel32 = kernel32
        self._handle = job_handle

    def assign(self, process_handle: int) -> None:
        if self._handle is None:
            return
        ok = self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
        if not ok:
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def kill_all(self) -> None:
        """Terminate every process assigned to this job, then release it."""

        if self._handle is None:
            return
        self._kernel32.TerminateJobObject(self._handle, 1)
        self._kernel32.CloseHandle(self._handle)
        self._handle = None


def _kill_tree_posix(pid: int) -> None:
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Output draining with a hard cap
# ---------------------------------------------------------------------------


def _drain(stream, max_bytes: int, cap_event: threading.Event, result_queue: "queue.Queue") -> None:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if total < max_bytes:
                remaining = max_bytes - total
                chunks.append(chunk[:remaining])
                total += len(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    cap_event.set()
            else:
                truncated = True
                cap_event.set()
    finally:
        stream.close()
        result_queue.put((b"".join(chunks), truncated))


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: ProcessLimits,
) -> ProcessResult:
    started_at = _utc_now()
    start_monotonic = time.monotonic()

    popen_kwargs: dict = {}
    job: _WindowsJob | None = None
    if sys.platform == "win32":
        job = _WindowsJob()
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = os.setsid  # noqa: PLW1509 - intentional, POSIX only

    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        **popen_kwargs,
    )

    if job is not None:
        try:
            job.assign(process._handle)  # type: ignore[attr-defined]
        except OSError:
            # Process may have already exited between Popen() and assign();
            # fall back to best-effort single-process kill on timeout.
            job = None

    cap_event = threading.Event()
    stdout_q: "queue.Queue" = queue.Queue()
    stderr_q: "queue.Queue" = queue.Queue()
    stdout_thread = threading.Thread(
        target=_drain, args=(process.stdout, limits.max_output_bytes, cap_event, stdout_q), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain, args=(process.stderr, limits.max_output_bytes, cap_event, stderr_q), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    output_cap_exceeded = False
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            break
        elapsed = time.monotonic() - start_monotonic
        if elapsed >= limits.timeout_seconds:
            timed_out = True
            break
        if cap_event.is_set():
            output_cap_exceeded = True
            break
        time.sleep(0.02)

    if timed_out or output_cap_exceeded:
        if job is not None:
            job.kill_all()
        else:
            process.kill()
            if sys.platform != "win32":
                _kill_tree_posix(process.pid)
        process.wait(timeout=5)
    else:
        process.wait()
        if job is not None:
            # Even on a clean exit, kill anything the process left running
            # behind it (a detached grandchild) — nothing should outlive
            # this invocation.
            job.kill_all()
        else:
            _kill_tree_posix(process.pid)

    stdout_bytes, stdout_truncated = stdout_q.get(timeout=5)
    stderr_bytes, stderr_truncated = stderr_q.get(timeout=5)
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    completed_at = _utc_now()
    duration_ms = int((time.monotonic() - start_monotonic) * 1000)

    return ProcessResult(
        argv=argv,
        exit_code=process.returncode,
        timed_out=timed_out,
        output_cap_exceeded=output_cap_exceeded,
        duration_ms=duration_ms,
        stdout=stdout_bytes,
        stderr=stderr_bytes,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        started_at=started_at,
        completed_at=completed_at,
    )
