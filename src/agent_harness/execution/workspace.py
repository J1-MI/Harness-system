"""Worktree lifecycle: locked creation, no implicit deletion (Phase 3.1).

Policy, from architecture review section 3 ("Worktree 격리"):

- branch: ``refs/heads/harness/<repo-id>/<run-id>``
- path: outside the source repository, under the harness data directory
- base: a full commit SHA, never a symbolic ref
- creation: a short per-repository-identity lock, atomic
- cleanup: only ever explicit — a crashed/abandoned worktree is left in
  place for recovery, never silently deleted ("비정상 종료: 삭제하지 않고
  RECOVERY_REQUIRED로 격리")

No agent execution happens here — this module only prepares the workspace.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from agent_harness.domain.models import WorkspaceLease
from agent_harness.execution.git_client import GitClient

__all__ = [
    "WorkspaceLockTimeoutError",
    "RepoLock",
    "create_worktree",
    "cleanup_worktree",
    "list_worktrees",
]


class WorkspaceLockTimeoutError(TimeoutError):
    pass


class RepoLock:
    """A short-lived, atomic, per-repository-identity lock.

    Uses ``O_CREAT | O_EXCL`` for atomicity, which works identically on
    Windows and POSIX (unlike ``fcntl``/``msvcrt`` file locks). Held only
    for the duration of worktree creation, not for the run's lifetime —
    the run-lifetime reservation is ``WorkspaceLease``, tracked separately.
    """

    def __init__(self, data_root: Path, repository_id: str, *, timeout_seconds: float = 10.0):
        self._path = data_root / "locks" / f"{repository_id}.lock"
        self._timeout_seconds = timeout_seconds
        self._acquired = False

    def __enter__(self) -> "RepoLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                os.close(fd)
                self._acquired = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise WorkspaceLockTimeoutError(
                        f"could not acquire lock {self._path} within "
                        f"{self._timeout_seconds}s"
                    )
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            self._path.unlink(missing_ok=True)
            self._acquired = False


def _branch_name(repository_id: str, run_id: str) -> str:
    return f"harness/{repository_id}/{run_id}"


def worktree_path_for(data_root: Path, *, repository_id: str, run_id: str) -> Path:
    """The deterministic worktree path ``create_worktree`` would use for
    this Run — lets a later, separate process (e.g. the CLI's ``cleanup``
    command) locate a Run's worktree from just its persisted
    ``repository_id``/``run_id``, without a dedicated lease table.

    Every caller of this function goes through this one containment
    check (Codex review H-02): ``repository_id``/``run_id`` are validated
    as safe path-segment slugs at the domain-model boundary
    (``domain.digests.IdentifierSlug``) before they ever reach here, but
    this is defense-in-depth for any caller that built a Run object
    bypassing that validator (or an older persisted row from before the
    validator existed) — the *resolved* path is checked to actually be a
    descendant of ``data_root`` before being handed back, catching a
    Windows-absolute-segment override (``Path("a") / "C:\\evil"``
    silently replaces the whole path) or any other traversal a slug
    regex alone might miss.
    """

    base = (data_root / "workspaces").resolve()
    candidate = (data_root / "workspaces" / repository_id / run_id).resolve()
    if base not in candidate.parents:
        raise ValueError(
            f"resolved worktree path {candidate} escapes the data root {base} "
            f"(repository_id={repository_id!r}, run_id={run_id!r})"
        )
    return candidate


def branch_ref_for(*, repository_id: str, run_id: str) -> str:
    """The deterministic full ref ``create_worktree`` would create — see
    ``worktree_path_for``."""

    return f"refs/heads/{_branch_name(repository_id, run_id)}"


def create_worktree(
    git_client: GitClient,
    *,
    data_root: Path,
    repository_id: str,
    source_repo_path: Path,
    base_commit_sha: str,
    run_id: str,
    now: datetime,
    sandbox_profile: str = "trusted_local",
) -> WorkspaceLease:
    """Create a fresh worktree pinned to ``base_commit_sha`` and return its lease.

    Never includes uncommitted changes from ``source_repo_path`` — a Git
    worktree is checked out from a committed ref, so a dirty primary
    working tree cannot leak into it (verified by
    ``tests/unit/test_workspace.py::test_dirty_source_repo_does_not_leak_into_worktree``).
    Submodules are not initialized and LFS objects are not smudged; both
    are the caller's explicit, separately-approved follow-up step.
    """

    resolved_sha = git_client.rev_parse(base_commit_sha, cwd=source_repo_path)
    branch = _branch_name(repository_id, run_id)
    worktree_path = worktree_path_for(data_root, repository_id=repository_id, run_id=run_id)

    with RepoLock(data_root, repository_id):
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        git_client.run(
            ["worktree", "add", "-b", branch, str(worktree_path), resolved_sha],
            cwd=source_repo_path,
        )

    return WorkspaceLease(
        repository_id=repository_id,
        worktree_path_handle=str(worktree_path),
        base_commit_sha=resolved_sha,
        branch=f"refs/heads/{branch}",
        owner=run_id,
        heartbeat_at=now,
        sandbox_profile=sandbox_profile,
        created_at=now,
    )


def list_worktrees(git_client: GitClient, *, source_repo_path: Path) -> list[dict[str, str]]:
    return git_client.list_worktrees(cwd=source_repo_path)


def cleanup_worktree(
    git_client: GitClient,
    *,
    source_repo_path: Path,
    lease: WorkspaceLease,
    force: bool = False,
) -> None:
    """Explicitly remove a worktree and its branch.

    The only removal path in this module — nothing else here ever deletes
    a worktree. Callers are responsible for only invoking this after a Run
    reaches a terminal disposition (or an explicit, reviewed cleanup
    decision), never automatically on crash/recovery.
    """

    args = ["worktree", "remove", lease.worktree_path_handle]
    if force:
        args.append("--force")
    git_client.run(args, cwd=source_repo_path)

    branch_short = lease.branch.removeprefix("refs/heads/")
    git_client.run(["branch", "-D", branch_short], cwd=source_repo_path, check=False)
