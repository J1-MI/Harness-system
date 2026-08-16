"""Path scope resolution: requested ∩ ceiling, never wider than either."""

from __future__ import annotations

from agent_harness.domain.models import ScopeRules

__all__ = ["intersect_scope"]


def _min_optional(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def intersect_scope(requested: ScopeRules, ceiling: ScopeRules) -> ScopeRules:
    """The effective scope: no wider than either ``requested`` or ``ceiling``.

    Allowed paths are the intersection (a path must be allowed by both to
    be granted). Forbidden paths are the union — either side's deny wins.
    Numeric ceilings take the stricter (smaller) of the two.
    """

    allowed = set(requested.allowed_path_rules) & set(ceiling.allowed_path_rules)
    forbidden = set(requested.forbidden_path_rules) | set(ceiling.forbidden_path_rules)

    return ScopeRules(
        allowed_path_rules=sorted(allowed),
        forbidden_path_rules=sorted(forbidden),
        allow_new_files=requested.allow_new_files and ceiling.allow_new_files,
        max_changed_files=_min_optional(requested.max_changed_files, ceiling.max_changed_files),
        max_changed_bytes=_min_optional(requested.max_changed_bytes, ceiling.max_changed_bytes),
        declared_generated_paths=sorted(
            set(requested.declared_generated_paths) & set(ceiling.declared_generated_paths)
        ),
    )
