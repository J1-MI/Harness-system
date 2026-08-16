"""Tests for the filesystem-manifest canonical change evidence (Phase 3.3).

Deliberately never touches Git — the whole point of ``build_file_manifest``
is that it does not care whether a file is tracked, staged, or ignored.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from agent_harness.execution.validation import (
    build_file_manifest,
    diff_manifests,
    find_test_mutations,
)


# ---------------------------------------------------------------------------
# Untracked files are covered (no git involved at all)
# ---------------------------------------------------------------------------


def test_manifest_captures_plain_files_regardless_of_git(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.txt").write_bytes(b"world\n")

    manifest = build_file_manifest(tmp_path)
    paths = {e.relative_path for e in manifest}
    assert paths == {"a.txt", "nested/b.txt"}


def test_manifest_records_correct_hash_and_size(tmp_path):
    content = b"the quick brown fox"
    (tmp_path / "f.txt").write_bytes(content)

    manifest = build_file_manifest(tmp_path)
    entry = manifest[0]
    assert entry.size == len(content)
    assert entry.sha256 == hashlib.sha256(content).hexdigest()
    assert entry.file_type == "file"


def test_manifest_ignores_the_git_directory(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    (tmp_path / "real.txt").write_bytes(b"content\n")

    manifest = build_file_manifest(tmp_path)
    paths = {e.relative_path for e in manifest}
    assert paths == {"real.txt"}


# ---------------------------------------------------------------------------
# Binary content is hashed byte-for-byte, not corrupted
# ---------------------------------------------------------------------------


def test_manifest_hashes_binary_content_correctly(tmp_path):
    binary_content = bytes(range(256)) * 4 + b"\x00\x00\xff\xfe"
    (tmp_path / "data.bin").write_bytes(binary_content)

    manifest = build_file_manifest(tmp_path)
    entry = manifest[0]
    assert entry.sha256 == hashlib.sha256(binary_content).hexdigest()
    assert entry.size == len(binary_content)


# ---------------------------------------------------------------------------
# Symlinks: recorded, not followed
# ---------------------------------------------------------------------------


def _symlinks_supported(tmp_path) -> bool:
    target = tmp_path / "_probe_target"
    link = tmp_path / "_probe_link"
    target.write_bytes(b"x")
    try:
        os.symlink(target, link)
    except OSError:
        return False
    finally:
        target.unlink(missing_ok=True)
        link.unlink(missing_ok=True)
    return True


def test_manifest_records_symlink_without_following_it(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation not permitted in this environment")

    target = tmp_path / "target.txt"
    target.write_bytes(b"target content, should not be hashed as the link")
    link = tmp_path / "link.txt"
    os.symlink(target, link)

    manifest = build_file_manifest(tmp_path)
    by_path = {e.relative_path: e for e in manifest}

    link_entry = by_path["link.txt"]
    assert link_entry.file_type == "symlink"
    assert link_entry.sha256 is None
    assert link_entry.symlink_target is not None

    target_entry = by_path["target.txt"]
    assert target_entry.file_type == "file"
    assert target_entry.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()


def test_manifest_symlink_to_outside_worktree_is_not_read(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation not permitted in this environment")

    outside_dir = tmp_path.parent / f"outside-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    outside_target = outside_dir / "secret.txt"
    outside_target.write_bytes(b"should never be read")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    link = worktree / "escape.txt"
    os.symlink(outside_target, link)

    manifest = build_file_manifest(worktree)
    entry = manifest[0]
    assert entry.file_type == "symlink"


def test_manifest_records_a_directory_symlink_as_its_own_entry(tmp_path):
    """Codex review H-03: os.walk(followlinks=False) never descends into a
    directory symlink, but it must still show up as an entry — otherwise
    it is completely invisible to the manifest and to scope/diff checks."""

    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation not permitted in this environment")

    outside_dir = tmp_path.parent / f"outside-dir-{tmp_path.name}"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.txt").write_bytes(b"should never be read or hashed")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    link = worktree / "escape_dir"
    os.symlink(outside_dir, link, target_is_directory=True)

    manifest = build_file_manifest(worktree)
    by_path = {e.relative_path: e for e in manifest}

    assert "escape_dir" in by_path
    entry = by_path["escape_dir"]
    assert entry.file_type == "symlink"
    assert entry.sha256 is None
    # Nothing from inside the linked-to directory was traversed or hashed.
    assert not any(path.startswith("escape_dir/") for path in by_path)
    assert entry.sha256 is None  # content of the escape target was never opened/hashed


# ---------------------------------------------------------------------------
# diff_manifests
# ---------------------------------------------------------------------------


def test_diff_manifests_categorizes_changes(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "unchanged.txt").write_bytes(b"same")
    (baseline_dir / "will_modify.txt").write_bytes(b"old content")
    (baseline_dir / "will_delete.txt").write_bytes(b"gone soon")
    baseline = build_file_manifest(baseline_dir)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "unchanged.txt").write_bytes(b"same")
    (result_dir / "will_modify.txt").write_bytes(b"new content")
    (result_dir / "added.txt").write_bytes(b"brand new")
    result = build_file_manifest(result_dir)

    diff = diff_manifests(baseline, result)
    assert diff.added == ["added.txt"]
    assert diff.modified == ["will_modify.txt"]
    assert diff.deleted == ["will_delete.txt"]
    assert diff.unchanged == ["unchanged.txt"]


# ---------------------------------------------------------------------------
# find_test_mutations
# ---------------------------------------------------------------------------


def test_find_test_mutations_flags_modified_test_files(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "tests").mkdir()
    (baseline_dir / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert False\n")
    (baseline_dir / "src.py").write_bytes(b"def f(): return 1\n")
    baseline = build_file_manifest(baseline_dir)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "tests").mkdir()
    # The worker "fixed" the failing test by weakening the assertion,
    # instead of fixing src.py.
    (result_dir / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert True\n")
    (result_dir / "src.py").write_bytes(b"def f(): return 1\n")
    result = build_file_manifest(result_dir)

    diff = diff_manifests(baseline, result)
    mutations = find_test_mutations(diff, test_path_patterns=["tests/**"])
    assert mutations == ["tests/test_thing.py"]


def test_find_test_mutations_ignores_changes_outside_test_paths(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "src.py").write_bytes(b"def f(): return 1\n")
    baseline = build_file_manifest(baseline_dir)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "src.py").write_bytes(b"def f(): return 2\n")
    result = build_file_manifest(result_dir)

    diff = diff_manifests(baseline, result)
    mutations = find_test_mutations(diff, test_path_patterns=["tests/**"])
    assert mutations == []


def test_find_test_mutations_flags_added_test_files(tmp_path):
    """Codex review M-03: a worker that adds a brand-new, trivially-passing
    test file instead of touching the original failing one must still be
    flagged — it never shows up as a "modification"."""

    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "tests").mkdir()
    (baseline_dir / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert False\n")
    baseline = build_file_manifest(baseline_dir)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    (result_dir / "tests").mkdir()
    (result_dir / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert False\n")
    # The original failing test is untouched; a new, always-passing test
    # is added alongside it.
    (result_dir / "tests" / "test_new.py").write_bytes(b"def test_always_passes(): assert True\n")
    result = build_file_manifest(result_dir)

    diff = diff_manifests(baseline, result)
    mutations = find_test_mutations(diff, test_path_patterns=["tests/**"])
    assert mutations == ["tests/test_new.py"]


def test_find_test_mutations_flags_deleted_test_files(tmp_path):
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    (baseline_dir / "tests").mkdir()
    (baseline_dir / "tests" / "test_thing.py").write_bytes(b"def test_x(): assert False\n")
    baseline = build_file_manifest(baseline_dir)

    result_dir = tmp_path / "result"
    result_dir.mkdir()
    result = build_file_manifest(result_dir)

    diff = diff_manifests(baseline, result)
    mutations = find_test_mutations(diff, test_path_patterns=["tests/**"])
    assert mutations == ["tests/test_thing.py"]
