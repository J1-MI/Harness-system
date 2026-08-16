"""Unit tests for canonical JSON, digest, and path-normalization rules."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agent_harness.domain.digests import (
    Digest,
    IdentifierSlug,
    RelativePath,
    canonical_json_bytes,
    compute_digest,
    is_valid_digest,
    new_id,
    normalize_identifier_slug,
    normalize_relative_path,
)


def test_new_id_is_unique_and_uuid_shaped():
    a, b = new_id(), new_id()
    assert a != b
    assert len(a) == 36
    assert a.count("-") == 4


def test_canonical_json_is_key_order_independent():
    payload_a = {"b": 1, "a": 2}
    payload_b = {"a": 2, "b": 1}
    assert canonical_json_bytes(payload_a) == canonical_json_bytes(payload_b)


def test_compute_digest_format():
    digest = compute_digest(b"hello")
    assert is_valid_digest(digest)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_compute_digest_is_deterministic_for_same_bytes():
    assert compute_digest(b"same") == compute_digest(b"same")


def test_compute_digest_differs_for_different_bytes():
    assert compute_digest(b"one") != compute_digest(b"two")


# ---------------------------------------------------------------------------
# 3. absolute path / ".." / NUL rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "\\\\server\\share",
        "C:/Windows/System32",
        "../secrets.env",
        "src/../../../etc/passwd",
        "src//double-slash",
        "",
        "src/\x00null",
        "a/./b",
    ],
)
def test_normalize_relative_path_rejects_unsafe_paths(bad_path):
    with pytest.raises(ValueError):
        normalize_relative_path(bad_path)


@pytest.mark.parametrize(
    "good_path",
    ["src/main.py", "tests/unit/test_x.py", "README.md", "src/**/*.py"],
)
def test_normalize_relative_path_accepts_safe_paths(good_path):
    assert normalize_relative_path(good_path) == good_path


class _PathHolder(BaseModel):
    path: RelativePath


class _DigestHolder(BaseModel):
    digest: Digest


def test_relative_path_annotation_rejects_traversal_via_pydantic():
    with pytest.raises(ValidationError):
        _PathHolder(path="../escape")


def test_relative_path_annotation_accepts_valid_path_via_pydantic():
    holder = _PathHolder(path="src/main.py")
    assert holder.path == "src/main.py"


def test_digest_annotation_rejects_malformed_digest():
    with pytest.raises(ValidationError):
        _DigestHolder(digest="not-a-real-digest")


def test_digest_annotation_rejects_uppercase_hex():
    with pytest.raises(ValidationError):
        _DigestHolder(digest="sha256:" + "A" * 64)


class _SlugHolder(BaseModel):
    slug: IdentifierSlug


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../../etc",
        "..",
        ".",
        "",
        "a/b",
        "a\\b",
        "C:\\evil",
        "-leading-dash",
        "has spaces",
        "has\x00null",
        "x" * 129,
    ],
)
def test_normalize_identifier_slug_rejects_unsafe_values(bad_slug):
    """Codex review H-02: repository_id/run_id are used as raw path
    segments — a value like '../../etc' or a Windows-absolute segment
    like 'C:\\evil' must be rejected before it ever reaches a Path join."""

    with pytest.raises(ValueError):
        normalize_identifier_slug(bad_slug)


@pytest.mark.parametrize("good_slug", ["repo-1", "my_repo.v2", "a", "A1", "550e8400-e29b-41d4-a716-446655440000"])
def test_normalize_identifier_slug_accepts_safe_values(good_slug):
    assert normalize_identifier_slug(good_slug) == good_slug


def test_identifier_slug_annotation_rejects_traversal_via_pydantic():
    with pytest.raises(ValidationError):
        _SlugHolder(slug="../../etc")


def test_identifier_slug_annotation_accepts_valid_value_via_pydantic():
    holder = _SlugHolder(slug="repo-1")
    assert holder.slug == "repo-1"


def test_digest_annotation_accepts_valid_digest():
    valid = "sha256:" + "0" * 64
    holder = _DigestHolder(digest=valid)
    assert holder.digest == valid
