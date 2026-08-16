"""Tests for the Claude Worker tool-call path guard (Codex review B-01).

``_check_tool_path`` is the piece that closes the gap the review flagged:
``can_use_tool`` used to check only the tool *name* against
``allowed_tool_ids`` and never inspected *where* a Read/Write/Edit/Glob/
Grep call actually pointed — an absolute or ``..``-relative path let
Claude touch any host file this process could reach, regardless of
``PolicyGrants.path_rules``.
"""

from __future__ import annotations

from pathlib import Path

from agent_harness.providers.claude import _check_tool_path
from tests.factories import make_scope


def test_read_within_worktree_is_allowed(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    assert _check_tool_path(
        "Read", {"file_path": str(workspace / "src" / "main.py")}, workspace_root=workspace, scope=scope
    ) is None


def test_read_outside_worktree_via_absolute_path_is_denied(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    outside = tmp_path / "outside" / "secret.txt"
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[])

    denial = _check_tool_path("Read", {"file_path": str(outside)}, workspace_root=workspace, scope=scope)
    assert denial is not None
    assert "outside the workspace" in denial


def test_write_outside_worktree_via_parent_traversal_is_denied(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[])

    denial = _check_tool_path(
        "Write", {"file_path": "../../etc/passwd"}, workspace_root=workspace, scope=scope
    )
    assert denial is not None
    assert "outside the workspace" in denial


def test_write_within_worktree_but_outside_scope_is_denied(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    denial = _check_tool_path(
        "Write", {"file_path": str(workspace / "secrets" / "prod.key")}, workspace_root=workspace, scope=scope
    )
    assert denial is not None
    assert "allowed_path_rules" in denial


def test_write_within_worktree_and_scope_is_allowed(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    assert _check_tool_path(
        "Write", {"file_path": str(workspace / "src" / "new.py")}, workspace_root=workspace, scope=scope
    ) is None


def test_write_matching_forbidden_pattern_is_denied_even_if_also_allowed(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["**"], forbidden_path_rules=[".git/**"])

    denial = _check_tool_path(
        "Write", {"file_path": str(workspace / ".git" / "config")}, workspace_root=workspace, scope=scope
    )
    assert denial is not None
    assert "forbidden_path_rules" in denial


def test_read_outside_scope_but_inside_worktree_is_allowed(tmp_path):
    """Reads are only worktree-contained, never scope-restricted — a
    Worker may legitimately need to read a file it isn't allowed to
    write to."""

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    assert _check_tool_path(
        "Read", {"file_path": str(workspace / "README.md")}, workspace_root=workspace, scope=scope
    ) is None


def test_relative_path_is_resolved_against_workspace_root(tmp_path):
    workspace = tmp_path / "worktree"
    (workspace / "src").mkdir(parents=True)
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    assert _check_tool_path(
        "Edit", {"file_path": "src/main.py"}, workspace_root=workspace, scope=scope
    ) is None


def test_tool_without_a_recognized_path_key_is_never_checked(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=[], forbidden_path_rules=[])

    assert _check_tool_path("Bash", {"command": "rm -rf /"}, workspace_root=workspace, scope=scope) is None


def test_missing_path_argument_is_never_checked(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    scope = make_scope(allowed_path_rules=["src/**"], forbidden_path_rules=[])

    assert _check_tool_path("Glob", {"pattern": "**/*.py"}, workspace_root=workspace, scope=scope) is None
