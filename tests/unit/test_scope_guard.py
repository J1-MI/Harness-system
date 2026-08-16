"""Tests for the deterministic scope-violation detector (Phase 13/M-01/M-02)."""

from __future__ import annotations

from agent_harness.execution.scope_guard import find_scope_violations, path_matches_glob
from agent_harness.execution.validation import ManifestDiff, ManifestEntry
from tests.factories import make_scope


def make_entry(path: str, size: int) -> ManifestEntry:
    return ManifestEntry(relative_path=path, file_type="file", mode=0o100644, size=size, sha256="x" * 64)


def test_path_matches_glob_basic_cases():
    assert path_matches_glob("src/foo.py", "src/**") is True
    assert path_matches_glob("srcfoo", "src/**") is False
    assert path_matches_glob("a/b/c.py", "**/*.py") is True
    assert path_matches_glob("c.txt", "**/*.py") is False


def test_no_violations_for_changes_within_scope():
    scope = make_scope(allowed_path_rules=["src/**", "tests/**"], forbidden_path_rules=[".git/**"])
    diff = ManifestDiff(added=["src/new.py"], modified=["tests/test_a.py"], deleted=[])
    assert find_scope_violations(diff, scope) == []


def test_forbidden_path_is_a_violation_even_if_also_allowed():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[".git/**"])
    diff = ManifestDiff(added=[], modified=[".git/config"], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert len(violations) == 1
    assert ".git/config" in violations[0]


def test_path_outside_allowed_rules_is_a_violation():
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])
    diff = ManifestDiff(added=["secrets/prod.key"], modified=[], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert len(violations) == 1
    assert "secrets/prod.key" in violations[0]


def test_empty_allowed_rules_means_nothing_is_allowed():
    scope = make_scope(allowed_path_rules=[], forbidden_path_rules=[])
    diff = ManifestDiff(added=["anything.py"], modified=[], deleted=[])
    assert len(find_scope_violations(diff, scope)) == 1


def test_new_files_rejected_when_allow_new_files_is_false():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], allow_new_files=False)
    diff = ManifestDiff(added=["new_file.py"], modified=[], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert any("allow_new_files" in v for v in violations)


def test_max_changed_files_ceiling_is_enforced():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_files=1)
    diff = ManifestDiff(added=["a.py", "b.py"], modified=[], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert any("max_changed_files" in v for v in violations)


# ---------------------------------------------------------------------------
# max_changed_bytes (Codex review M-02)
# ---------------------------------------------------------------------------


def test_max_changed_bytes_is_enforced_using_result_manifest():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_bytes=100)
    diff = ManifestDiff(added=["a.py"], modified=[], deleted=[])
    result_manifest = [make_entry("a.py", 200)]
    violations = find_scope_violations(diff, scope, result_manifest=result_manifest)
    assert any("max_changed_bytes" in v for v in violations)


def test_max_changed_bytes_uses_baseline_manifest_for_deleted_files():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_bytes=100)
    diff = ManifestDiff(added=[], modified=[], deleted=["big.bin"])
    baseline_manifest = [make_entry("big.bin", 500)]
    violations = find_scope_violations(diff, scope, baseline_manifest=baseline_manifest)
    assert any("max_changed_bytes" in v for v in violations)


def test_max_changed_bytes_within_budget_is_not_a_violation():
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_bytes=1000)
    diff = ManifestDiff(added=["a.py"], modified=[], deleted=[])
    result_manifest = [make_entry("a.py", 50)]
    assert find_scope_violations(diff, scope, result_manifest=result_manifest) == []


def test_max_changed_bytes_skipped_without_manifest_data():
    """Backward-compatible: callers that only pass a diff/scope (no
    manifest) never get a max_changed_bytes check they can't answer."""

    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_bytes=1)
    diff = ManifestDiff(added=["a.py"], modified=[], deleted=[])
    assert find_scope_violations(diff, scope) == []


# ---------------------------------------------------------------------------
# declared_generated_paths (Codex review M-02)
# ---------------------------------------------------------------------------


def test_declared_generated_path_exempt_from_max_changed_files():
    scope = make_scope(
        allowed_path_rules=["**"], forbidden_path_rules=[], max_changed_files=1,
        declared_generated_paths=["package-lock.json"],
    )
    diff = ManifestDiff(added=["src/a.py", "package-lock.json"], modified=[], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert violations == []  # only src/a.py counts, within the ceiling of 1


def test_declared_generated_path_exempt_from_allow_new_files():
    scope = make_scope(
        allowed_path_rules=["**"], forbidden_path_rules=[], allow_new_files=False,
        declared_generated_paths=["package-lock.json"],
    )
    diff = ManifestDiff(added=["package-lock.json"], modified=[], deleted=[])
    assert find_scope_violations(diff, scope) == []


def test_declared_generated_path_still_subject_to_forbidden_rules():
    """Being declared-generated never exempts a path from the
    forbidden/allowed membership check — only from the counting."""

    scope = make_scope(
        allowed_path_rules=["**"], forbidden_path_rules=[".git/**"],
        declared_generated_paths=[".git/config"],
    )
    diff = ManifestDiff(added=[], modified=[".git/config"], deleted=[])
    violations = find_scope_violations(diff, scope)
    assert any(".git/config" in v for v in violations)
