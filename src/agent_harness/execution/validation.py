"""Canonical change evidence: a full filesystem manifest, not a git diff.

Architecture review M-05: "diff를 canonical 변경 증거로 사용한다 → untracked,
ignored, binary, LFS, 파일 모드 및 symlink 변화가 누락될 수 있다." This
module walks the worktree directly instead, so nothing depends on the
file being tracked, staged, or even known to Git at all. ``git diff`` is
still useful as a human-readable *display* artifact, not as the thing
correctness decisions are made from — see ``execution/evidence.py``.

No agent execution and no Codex verifier here — this module only produces
evidence for something else to judge.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ManifestEntry",
    "ManifestDiff",
    "build_file_manifest",
    "diff_manifests",
    "find_test_mutations",
]

_IGNORED_TOP_LEVEL_DIRS = frozenset({".git"})


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    file_type: str  # "file" | "symlink"
    mode: int
    size: int
    sha256: str | None  # None for symlinks — see symlink_target instead
    symlink_target: str | None = None


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_reparse_point(lstat_result: os.stat_result) -> bool:
    """True for POSIX symlinks *and* Windows junctions/reparse points.

    A Windows junction is a distinct reparse-point type from a symlink —
    it does not set the bit ``stat.S_ISLNK`` checks for, so relying on
    ``S_ISLNK`` alone (as this function used to) misses junctions
    entirely (Codex review H-03). ``st_file_attributes`` is
    Windows-only; POSIX platforms fall back to the ``S_ISLNK`` check.
    """

    if stat.S_ISLNK(lstat_result.st_mode):
        return True
    file_attributes = getattr(lstat_result, "st_file_attributes", None)
    if file_attributes is not None:
        return bool(file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _symlink_entry(full_path: Path, root: Path, lstat_result: os.stat_result) -> ManifestEntry:
    relative_path = full_path.relative_to(root).as_posix()
    try:
        target = os.readlink(full_path)
    except OSError:
        # Some reparse-point types (certain Windows junctions/mount
        # points) are not readable via os.readlink — still record the
        # entry so its *existence* is never invisible to the manifest,
        # just without a resolvable target.
        target = None
    return ManifestEntry(
        relative_path=relative_path,
        file_type="symlink",
        mode=lstat_result.st_mode,
        size=lstat_result.st_size,
        sha256=None,
        symlink_target=target,
    )


def build_file_manifest(root: Path) -> list[ManifestEntry]:
    """Walk ``root`` and record every file, symlink, and directory-type
    reparse point/junction, hashed (or target-recorded) with no-follow.

    Symlinks/junctions are never followed — one pointing outside the
    worktree (or at something huge/blocking, like a device file) must
    never cause us to read through it. Directories that are *not*
    themselves a reparse point are not recorded as entries; membership is
    implied by the files/symlinks within them. A directory-type symlink
    or junction *is* recorded as its own entry — ``os.walk(followlinks=
    False)`` never descends into one, but it also never surfaces it on
    its own, so without this it would be completely invisible to the
    manifest (Codex review H-03).
    """

    entries: list[ManifestEntry] = []
    root = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        if current_dir == root:
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_TOP_LEVEL_DIRS]

        for name in dirnames:
            full_path = current_dir / name
            lstat_result = full_path.lstat()
            if _is_reparse_point(lstat_result):
                entries.append(_symlink_entry(full_path, root, lstat_result))

        for name in filenames:
            full_path = current_dir / name
            lstat_result = full_path.lstat()

            if _is_reparse_point(lstat_result):
                entries.append(_symlink_entry(full_path, root, lstat_result))
            else:
                entries.append(
                    ManifestEntry(
                        relative_path=full_path.relative_to(root).as_posix(),
                        file_type="file",
                        mode=lstat_result.st_mode,
                        size=lstat_result.st_size,
                        sha256=_sha256_file(full_path),
                    )
                )

    return sorted(entries, key=lambda e: e.relative_path)


@dataclass(frozen=True)
class ManifestDiff:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def _entry_identity(entry: ManifestEntry) -> tuple:
    """What makes two entries at the same path "the same" content-wise."""

    return (entry.file_type, entry.sha256, entry.symlink_target, entry.mode)


def diff_manifests(baseline: list[ManifestEntry], result: list[ManifestEntry]) -> ManifestDiff:
    baseline_by_path = {e.relative_path: e for e in baseline}
    result_by_path = {e.relative_path: e for e in result}

    added = sorted(set(result_by_path) - set(baseline_by_path))
    deleted = sorted(set(baseline_by_path) - set(result_by_path))
    modified: list[str] = []
    unchanged: list[str] = []
    for path in sorted(set(baseline_by_path) & set(result_by_path)):
        if _entry_identity(baseline_by_path[path]) == _entry_identity(result_by_path[path]):
            unchanged.append(path)
        else:
            modified.append(path)

    return ManifestDiff(added=added, modified=modified, deleted=deleted, unchanged=unchanged)


def find_test_mutations(diff: ManifestDiff, test_path_patterns: list[str]) -> list[str]:
    """Paths matching ``test_path_patterns`` that were added, modified, or deleted.

    A worker editing or deleting the very tests meant to check its own
    work is a red flag the Verifier must see, regardless of what its
    ``WorkerResult.reported_tests`` claims — this is why acceptance
    criteria backed by ``COMMAND`` verification must run on a frozen,
    host-observed snapshot rather than trust worker-reported results.

    ``diff.added`` is included too (Codex review M-03): a worker that
    swaps a genuinely failing test for a brand-new, trivially-passing one
    never shows up as a "modification" of the original file — it is a
    new path. That is exactly the same self-grading risk as editing an
    existing test in place, so it must surface the same way. This
    function only flags for visibility; it does not itself decide
    whether a new test file is legitimate work or gaming the result —
    that judgment still belongs to the Verifier/Harness looking at the
    evidence.
    """

    changed = diff.added + diff.modified + diff.deleted
    flagged: list[str] = []
    for path in changed:
        if any(fnmatch.fnmatch(path, pattern) for pattern in test_path_patterns):
            flagged.append(path)
    return sorted(flagged)
