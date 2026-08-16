"""Deterministic scope-violation detection.

Attack surface #9 in the review's table ("Claude가 scope 밖 수정") lists
"baseline/result/test 전후 manifest 비교" as the detection control. Phase
3.3 already builds that comparison (``ManifestDiff``); this module is the
piece that was missing until the Codex implementation review (finding
M-01) flagged it: actually checking that diff against the TaskContract's
own ``ScopeRules``, deterministically, rather than leaving it entirely to
the Codex Verifier's free-text judgment (``VerificationResult
.scope_violations``) — the same class of gap Phase 8 closed for evidence
citations via ``find_missing_evidence_violations``.

As of the Codex review rework pass, ``execution.evidence.freeze_and_validate``
calls this directly and folds the result into ``FrozenValidationResult
.scope_violations``, and ``application.verification.run_verification``
downgrades a model-claimed PASS to ``MANUAL_REVIEW`` if that list is
non-empty (mirroring the evidence-ref gate exactly).
"""

from __future__ import annotations

import re

from agent_harness.domain.models import ScopeRules
from agent_harness.execution.validation import ManifestDiff, ManifestEntry

__all__ = ["path_matches_glob", "find_scope_violations"]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a gitignore-style glob into a regex matching a root-relative,
    ``/``-separated path.

    ``**/`` matches zero or more whole path segments; a trailing/standalone
    ``**`` matches anything (including ``/``); a single ``*`` matches
    within one segment; ``?`` matches one non-separator character. Any
    other character is matched literally.
    """

    regex_parts: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            regex_parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            regex_parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            regex_parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            regex_parts.append("[^/]")
            i += 1
        else:
            regex_parts.append(re.escape(pattern[i]))
            i += 1
    regex_parts.append("$")
    return re.compile("".join(regex_parts))


def path_matches_glob(path: str, pattern: str) -> bool:
    return _glob_to_regex(pattern).match(path) is not None


def find_scope_violations(
    diff: ManifestDiff,
    scope: ScopeRules,
    *,
    baseline_manifest: list[ManifestEntry] | None = None,
    result_manifest: list[ManifestEntry] | None = None,
) -> list[str]:
    """Every added/modified/deleted path in ``diff`` that ``scope`` does
    not actually allow.

    Fail-closed on both ends: a path matching any ``forbidden_path_rules``
    pattern is always a violation regardless of ``allowed_path_rules``,
    and a path matching *none* of the (possibly empty)
    ``allowed_path_rules`` patterns is a violation too — an empty
    ``allowed_path_rules`` means "nothing is allowed", never "everything
    is allowed". ``declared_generated_paths`` never exempts a path from
    the forbidden/allowed membership check above (a generated path
    pointed at something forbidden is still forbidden) — it only exempts
    that path from the ``allow_new_files``/``max_changed_files``/
    ``max_changed_bytes`` *counting* below, since an anticipated generated
    artifact (a lockfile, a build output) is not the kind of change a
    human-reviewed change budget is meant to police.

    ``max_changed_bytes`` needs each changed path's size, which
    ``ManifestDiff`` itself does not carry — pass ``result_manifest``
    (for added/modified paths) and/or ``baseline_manifest`` (for deleted
    paths' pre-deletion size) to enable that check; it is silently
    skipped if neither is given, matching this function's original
    manifest-free signature for callers that only care about path scope.
    """

    violations: list[str] = []
    changed_paths = [*diff.added, *diff.modified, *diff.deleted]

    for path in changed_paths:
        if any(path_matches_glob(path, pattern) for pattern in scope.forbidden_path_rules):
            violations.append(f"{path!r} matches a forbidden_path_rules pattern")
            continue
        if not any(path_matches_glob(path, pattern) for pattern in scope.allowed_path_rules):
            violations.append(f"{path!r} does not match any allowed_path_rules pattern")

    def _is_declared_generated(path: str) -> bool:
        return any(path_matches_glob(path, pattern) for pattern in scope.declared_generated_paths)

    countable_paths = [p for p in changed_paths if not _is_declared_generated(p)]
    countable_added = [p for p in diff.added if not _is_declared_generated(p)]

    if not scope.allow_new_files and countable_added:
        violations.append(f"{len(countable_added)} new file(s) added but allow_new_files is False")

    if scope.max_changed_files is not None and len(countable_paths) > scope.max_changed_files:
        violations.append(
            f"{len(countable_paths)} changed files exceeds max_changed_files={scope.max_changed_files}"
        )

    if scope.max_changed_bytes is not None and (baseline_manifest is not None or result_manifest is not None):
        result_by_path = {e.relative_path: e for e in (result_manifest or [])}
        baseline_by_path = {e.relative_path: e for e in (baseline_manifest or [])}
        total_bytes = 0
        for path in countable_paths:
            entry = result_by_path.get(path) or baseline_by_path.get(path)
            if entry is not None:
                total_bytes += entry.size
        if total_bytes > scope.max_changed_bytes:
            violations.append(
                f"{total_bytes} changed bytes exceeds max_changed_bytes={scope.max_changed_bytes}"
            )

    return violations
