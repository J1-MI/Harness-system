"""A harness-controlled ``git`` subprocess wrapper.

Every invocation runs with hooks, pager, editor, external diff, fsmonitor,
and credential helper disabled, and with LFS smudge/fetch and submodule
recursion off by default — matching architecture review section 3
("Git hooks, pager, editor, external diff, fsmonitor, credential helper를
비활성화한 하네스 전용 Git 실행 환경") and section 3's worktree policy
("submodule은 기본적으로 초기화하지 않는다", "Git LFS는 기본적으로 pointer만
체크아웃하고 smudge/fetch를 차단한다").

``shell=False`` always — argv lists only, never a shell string (H-04).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent_harness.domain.digests import compute_digest
from agent_harness.domain.models import validate_commit_sha

__all__ = [
    "GitCommandError",
    "GitClient",
]


class GitCommandError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stdout: str, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(argv)} exited {returncode}: {stderr.strip() or stdout.strip()}"
        )


# Overrides applied to every invocation, ahead of the caller's own argv.
# No shell string is ever built — this is always a plain list.
_HARNESS_CONFIG_ARGS: tuple[str, ...] = (
    "-c", "core.pager=cat",
    "-c", "core.editor=true",
    "-c", "advice.detachedHead=false",
    "-c", "diff.external=",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "protocol.file.allow=user",
    # Force a fixed line-ending policy so worktree checkouts are byte-for-
    # -byte reproducible regardless of the host machine's global/system
    # core.autocrlf — "재현 가능한 worktree" is this phase's stated goal.
    "-c", "core.autocrlf=false",
)

_HARNESS_ENV_OVERRIDES: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
}


class GitClient:
    """Runs ``git`` with a fixed, harness-controlled configuration.

    ``empty_hooks_dir`` is pointed to by ``core.hooksPath`` on every call so
    no repository-provided hook script (post-checkout, pre-commit, ...) can
    ever run through this client, regardless of what the repository's own
    ``.git/hooks`` contains.
    """

    def __init__(self, empty_hooks_dir: Path) -> None:
        empty_hooks_dir.mkdir(parents=True, exist_ok=True)
        self._empty_hooks_dir = empty_hooks_dir

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "git",
            *_HARNESS_CONFIG_ARGS,
            "-c", f"core.hooksPath={self._empty_hooks_dir}",
            *args,
        ]
        env = dict(os.environ)
        env.update(_HARNESS_ENV_OVERRIDES)
        if extra_env:
            env.update(extra_env)

        result = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitCommandError(args, result.returncode, result.stdout, result.stderr)
        return result

    def rev_parse(self, ref: str, *, cwd: Path) -> str:
        """Resolve ``ref`` to a full commit SHA (never a symbolic ref)."""

        result = self.run(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
        sha = result.stdout.strip()
        return validate_commit_sha(sha)

    def is_working_tree_dirty(self, *, cwd: Path) -> bool:
        result = self.run(["status", "--porcelain"], cwd=cwd)
        return bool(result.stdout.strip())

    def root_commit_shas(self, *, cwd: Path) -> list[str]:
        """All commits with no parent — the repository's "birth" commit(s)."""

        result = self.run(["rev-list", "--max-parents=0", "HEAD"], cwd=cwd)
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def compute_repository_fingerprint(self, *, cwd: Path) -> str:
        """A digest identifying repository *identity*, independent of clone location.

        Derived from the sorted root commit SHA(s) — clones, mirrors, and
        worktrees of the same repository share the same root commit(s), but
        an unrelated repository will not.
        """

        roots = self.root_commit_shas(cwd=cwd)
        return compute_digest("\n".join(roots).encode("utf-8"))

    def verify_repository_fingerprint(self, *, cwd: Path, expected_fingerprint: str) -> None:
        actual = self.compute_repository_fingerprint(cwd=cwd)
        if actual != expected_fingerprint:
            raise ValueError(
                f"repository fingerprint mismatch: expected "
                f"{expected_fingerprint!r}, computed {actual!r} at {cwd}"
            )

    def list_worktrees(self, *, cwd: Path) -> list[dict[str, str]]:
        result = self.run(["worktree", "list", "--porcelain"], cwd=cwd)
        entries: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        if current:
            entries.append(current)
        return entries
