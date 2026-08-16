"""Tests for the harness Git client and worktree lifecycle (Phase 3.1).

Uses real local git repositories under ``tmp_path`` — no network, no
fixtures beyond what ``git init`` + a couple of commits provide.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_harness.execution.git_client import GitClient, GitCommandError
from agent_harness.execution.workspace import (
    RepoLock,
    WorkspaceLockTimeoutError,
    cleanup_worktree,
    create_worktree,
    list_worktrees,
    worktree_path_for,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not available"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def make_repo(tmp_path: Path, name: str = "repo", content: bytes = b"hello\n") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "tester@example.com", cwd=repo)
    _git("config", "user.name", "Tester", cwd=repo)
    _git("config", "core.autocrlf", "false", cwd=repo)
    # write_bytes, not write_text: on Windows, text-mode writes translate
    # "\n" to "\r\n", which makes working-tree bytes disagree with the
    # committed (LF) blob depending on the host's core.autocrlf setting —
    # exactly the kind of host-dependent nondeterminism this harness's git
    # wrapper exists to avoid.
    (repo / "a.txt").write_bytes(content)
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture()
def repo(tmp_path) -> Path:
    return make_repo(tmp_path, "source-repo")


@pytest.fixture()
def data_root(tmp_path) -> Path:
    return tmp_path / "data-root"


@pytest.fixture()
def git_client(data_root) -> GitClient:
    return GitClient(data_root / "empty-hooks")


# ---------------------------------------------------------------------------
# GitClient basics
# ---------------------------------------------------------------------------


def test_rev_parse_resolves_head_to_full_sha(git_client, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_rev_parse_raises_for_unknown_ref(git_client, repo):
    with pytest.raises(GitCommandError):
        git_client.rev_parse("refs/heads/does-not-exist", cwd=repo)


def test_is_working_tree_dirty_reflects_uncommitted_changes(git_client, repo):
    assert git_client.is_working_tree_dirty(cwd=repo) is False
    (repo / "a.txt").write_bytes(b"changed\n")
    assert git_client.is_working_tree_dirty(cwd=repo) is True


def test_repository_fingerprint_is_stable_and_repo_specific(git_client, tmp_path, repo):
    fp_a = git_client.compute_repository_fingerprint(cwd=repo)
    fp_b = git_client.compute_repository_fingerprint(cwd=repo)
    assert fp_a == fp_b

    other_repo = make_repo(tmp_path, "other-repo", content=b"a completely different tree\n")
    fp_other = git_client.compute_repository_fingerprint(cwd=other_repo)
    assert fp_other != fp_a


def test_verify_repository_fingerprint_rejects_mismatch(git_client, repo):
    with pytest.raises(ValueError):
        git_client.verify_repository_fingerprint(
            cwd=repo, expected_fingerprint="sha256:" + "0" * 64
        )


def test_git_client_uses_no_shell_and_disables_hooks(data_root, repo):
    """A post-checkout hook must never fire through GitClient."""

    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    marker = repo / "hook-ran.marker"
    hook_path = hooks_dir / "post-checkout"
    hook_path.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook_path.chmod(0o755)

    client = GitClient(data_root / "empty-hooks")
    client.run(["checkout", "-q", "main"], cwd=repo)

    assert not marker.exists(), "repository-provided hook ran despite core.hooksPath override"


# ---------------------------------------------------------------------------
# worktree_path_for containment (Codex review H-02)
# ---------------------------------------------------------------------------


def test_worktree_path_for_rejects_a_windows_absolute_repository_id(data_root):
    """A Windows-absolute path segment silently *replaces* the whole
    path when joined via Path.__truediv__ (Path("a") / "C:\\evil" ==
    "C:\\evil") — this must be caught, not silently followed. Calling
    worktree_path_for directly (bypassing the domain-model IdentifierSlug
    validator) proves this containment check holds even if some future
    caller builds a Run/lease outside Pydantic validation."""

    with pytest.raises(ValueError, match="escapes the data root"):
        worktree_path_for(data_root, repository_id="C:\\evil", run_id="run-1")


def test_worktree_path_for_rejects_parent_traversal(data_root):
    with pytest.raises(ValueError, match="escapes the data root"):
        worktree_path_for(data_root, repository_id="../../escape", run_id="run-1")


def test_worktree_path_for_accepts_a_normal_repository_id(data_root):
    path = worktree_path_for(data_root, repository_id="repo-1", run_id="run-1")
    assert (data_root / "workspaces").resolve() in path.parents


# ---------------------------------------------------------------------------
# create_worktree / cleanup_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_checks_out_pinned_commit(git_client, data_root, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-1",
        now=_utc_now(),
    )

    worktree_path = Path(lease.worktree_path_handle)
    assert (worktree_path / "a.txt").read_text() == "hello\n"
    assert lease.base_commit_sha == sha
    assert lease.branch == "refs/heads/harness/repo-1/run-1"


def test_create_worktree_branch_matches_naming_policy(git_client, data_root, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="my-repo",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-abc-123",
        now=_utc_now(),
    )
    assert lease.branch == "refs/heads/harness/my-repo/run-abc-123"


def test_dirty_source_repo_does_not_leak_into_worktree(git_client, data_root, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    (repo / "a.txt").write_bytes(b"DIRTY UNCOMMITTED CHANGE\n")
    assert git_client.is_working_tree_dirty(cwd=repo) is True

    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-1",
        now=_utc_now(),
    )

    worktree_file = Path(lease.worktree_path_handle) / "a.txt"
    assert worktree_file.read_text() == "hello\n"


def test_cleanup_worktree_removes_directory_and_branch(git_client, data_root, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-1",
        now=_utc_now(),
    )
    cleanup_worktree(git_client, source_repo_path=repo, lease=lease)

    assert not Path(lease.worktree_path_handle).exists()
    remaining_branches = _git("branch", "--list", cwd=repo).stdout
    assert "harness/repo-1/run-1" not in remaining_branches


# ---------------------------------------------------------------------------
# Crash recovery: nothing implicit ever deletes a worktree
# ---------------------------------------------------------------------------


def test_abandoned_worktree_survives_and_is_still_discoverable(git_client, data_root, repo):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="crashed-run",
        now=_utc_now(),
    )
    # Simulate a crash: no cleanup_worktree call, fresh GitClient (new "process").
    fresh_client = GitClient(data_root / "empty-hooks")
    worktrees = list_worktrees(fresh_client, source_repo_path=repo)
    paths = {Path(w["worktree"]) for w in worktrees}
    assert Path(lease.worktree_path_handle) in paths
    assert Path(lease.worktree_path_handle).exists()


def test_lock_is_released_even_though_worktree_was_never_cleaned_up(
    git_client, data_root, repo
):
    sha = git_client.rev_parse("HEAD", cwd=repo)
    create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="crashed-run",
        now=_utc_now(),
    )
    # The lock is short-lived (creation only); a second, independent run
    # against the same repository must not be blocked by the first run's
    # abandoned worktree.
    lease_2 = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-1",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="second-run",
        now=_utc_now(),
    )
    assert Path(lease_2.worktree_path_handle).exists()


# ---------------------------------------------------------------------------
# Concurrent worktree creation (lock serialization)
# ---------------------------------------------------------------------------


def test_repo_lock_serializes_concurrent_acquirers(data_root):
    repository_id = "concurrent-repo"
    events: list[str] = []
    lock_held = threading.Event()
    proceed = threading.Event()

    def holder():
        with RepoLock(data_root, repository_id, timeout_seconds=5.0):
            events.append("holder-acquired")
            lock_held.set()
            proceed.wait(timeout=5.0)
            events.append("holder-released")

    def waiter():
        lock_held.wait(timeout=5.0)
        events.append("waiter-attempting")
        with RepoLock(data_root, repository_id, timeout_seconds=5.0):
            events.append("waiter-acquired")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=waiter)
    t1.start()
    t1.join(timeout=0)  # let holder start without blocking this thread
    time.sleep(0.05)
    t2.start()
    time.sleep(0.1)
    proceed.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert events.index("waiter-attempting") < events.index("holder-released")
    assert events.index("waiter-acquired") > events.index("holder-released")


def test_repo_lock_times_out_if_never_released(data_root):
    repository_id = "stuck-repo"
    with RepoLock(data_root, repository_id, timeout_seconds=5.0):
        with pytest.raises(WorkspaceLockTimeoutError):
            with RepoLock(data_root, repository_id, timeout_seconds=0.1):
                pass


# ---------------------------------------------------------------------------
# Submodules not initialized by default
# ---------------------------------------------------------------------------


def test_submodule_is_not_initialized_by_default(git_client, data_root, tmp_path, repo):
    submodule_repo = make_repo(tmp_path, "submodule-source")
    _git(
        "-c", "protocol.file.allow=always",
        "submodule", "add", str(submodule_repo), "vendor/sub",
        cwd=repo,
    )
    _git("commit", "-q", "-m", "add submodule", cwd=repo)

    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-with-submodule",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-1",
        now=_utc_now(),
    )

    submodule_dir = Path(lease.worktree_path_handle) / "vendor" / "sub"
    assert (Path(lease.worktree_path_handle) / ".gitmodules").exists()
    # The submodule directory exists (as an empty placeholder) but its
    # contents were never checked out/initialized.
    assert list(submodule_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# LFS smudge is skipped by default
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs not installed")
def test_lfs_objects_are_not_smudged_by_default(git_client, data_root, repo):
    _git("lfs", "install", "--local", cwd=repo)
    (repo / ".gitattributes").write_bytes(b"*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _git("add", ".gitattributes", cwd=repo)
    (repo / "payload.bin").write_bytes(b"not actually large but tracked via lfs")
    _git("add", "payload.bin", cwd=repo)
    _git("commit", "-q", "-m", "add lfs file", cwd=repo)

    sha = git_client.rev_parse("HEAD", cwd=repo)
    lease = create_worktree(
        git_client,
        data_root=data_root,
        repository_id="repo-with-lfs",
        source_repo_path=repo,
        base_commit_sha=sha,
        run_id="run-1",
        now=_utc_now(),
    )

    checked_out = (Path(lease.worktree_path_handle) / "payload.bin").read_bytes()
    assert checked_out.startswith(b"version https://git-lfs.github.com/spec/v1")
    assert b"not actually large but tracked via lfs" not in checked_out
