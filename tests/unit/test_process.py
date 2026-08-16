"""Tests for the argv-only process runner (Phase 3.2).

Uses the current Python interpreter to spawn well-controlled child/
grandchild processes — no reliance on any external binary being on PATH.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from agent_harness.execution.process import ProcessLimits, run_process


def _env() -> dict[str, str]:
    return dict(os.environ)


def test_run_process_captures_stdout_and_exit_code(tmp_path):
    result = run_process(
        [sys.executable, "-c", "print('hello')"],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=10, max_output_bytes=10_000),
    )
    assert result.exit_code == 0
    assert b"hello" in result.stdout
    assert result.timed_out is False
    assert result.output_cap_exceeded is False


def test_run_process_captures_nonzero_exit_code(tmp_path):
    result = run_process(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=10, max_output_bytes=10_000),
    )
    assert result.exit_code == 7


def test_run_process_never_uses_a_shell(tmp_path):
    # A shell-only builtin like "echo" with redirection syntax must be
    # treated as a literal, nonexistent executable name — never
    # interpreted by cmd.exe/sh.
    with pytest.raises((FileNotFoundError, OSError)):
        run_process(
            ["echo hello && echo injected"],
            cwd=tmp_path,
            env=_env(),
            limits=ProcessLimits(timeout_seconds=5, max_output_bytes=1000),
        )


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_run_process_enforces_timeout(tmp_path):
    start = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=1, max_output_bytes=10_000),
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert elapsed < 10  # nowhere near the 30s sleep


# ---------------------------------------------------------------------------
# Output cap ("log flood")
# ---------------------------------------------------------------------------


def test_run_process_kills_on_output_cap_and_truncates(tmp_path):
    flooder = (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('x' * 1000)\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.01)\n"
    )
    start = time.monotonic()
    result = run_process(
        [sys.executable, "-c", flooder],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=30, max_output_bytes=200),
    )
    elapsed = time.monotonic() - start

    assert result.output_cap_exceeded is True
    assert result.timed_out is False
    assert len(result.stdout) <= 200
    assert result.stdout_truncated is True
    # Killed promptly on cap breach, nowhere near the 30s timeout ceiling.
    assert elapsed < 10


# ---------------------------------------------------------------------------
# Child leak: killing the runner must kill grandchildren too
# ---------------------------------------------------------------------------


def test_run_process_kills_grandchildren_on_timeout(tmp_path):
    heartbeat = tmp_path / "heartbeat.txt"
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import time\n"
        f"p = {str(heartbeat)!r}\n"
        "while True:\n"
        "    with open(p, 'w') as f:\n"
        "        f.write(str(time.time()))\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}], close_fds=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    result = run_process(
        [sys.executable, str(parent_script)],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=1, max_output_bytes=10_000),
    )
    assert result.timed_out is True

    # Give the (hopefully dead) grandchild a moment it would use to write
    # another heartbeat if it were still alive.
    time.sleep(0.3)
    assert heartbeat.exists()
    reading_1 = heartbeat.read_text()
    time.sleep(0.5)
    reading_2 = heartbeat.read_text()
    assert reading_1 == reading_2, "grandchild process leaked past the parent's termination"


def test_run_process_kills_grandchildren_on_clean_exit(tmp_path):
    """Even a parent that exits 0 must not leave a detached grandchild running."""

    heartbeat = tmp_path / "heartbeat.txt"
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import time\n"
        f"p = {str(heartbeat)!r}\n"
        "while True:\n"
        "    with open(p, 'w') as f:\n"
        "        f.write(str(time.time()))\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}], close_fds=True)\n"
        # Give the grandchild a head start so it writes at least one
        # heartbeat before the parent exits cleanly — otherwise this test
        # races the grandchild's Python interpreter startup time.
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )

    result = run_process(
        [sys.executable, str(parent_script)],
        cwd=tmp_path,
        env=_env(),
        limits=ProcessLimits(timeout_seconds=10, max_output_bytes=10_000),
    )
    assert result.exit_code == 0
    assert result.timed_out is False

    time.sleep(0.3)
    assert heartbeat.exists()
    reading_1 = heartbeat.read_text()
    time.sleep(0.5)
    reading_2 = heartbeat.read_text()
    assert reading_1 == reading_2, "grandchild survived the parent's clean exit"


# ---------------------------------------------------------------------------
# Env scrub (exercised at this layer via explicit env passthrough)
# ---------------------------------------------------------------------------


def test_run_process_only_sees_the_env_it_is_given(tmp_path):
    result = run_process(
        [sys.executable, "-c", "import os; print(os.environ.get('SECRET_TOKEN', 'MISSING'))"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        limits=ProcessLimits(timeout_seconds=10, max_output_bytes=10_000),
    )
    assert b"MISSING" in result.stdout
