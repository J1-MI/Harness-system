"""Canonical JSON, path normalization, and digest rules.

Rules (see ``docs/architecture/contract-kernel.md``):

- digests are ``sha256:<64 lowercase hex>``
- canonical JSON uses sorted keys, compact separators, UTF-8
- root-relative paths use ``/`` separators; absolute paths, empty
  segments, ``.``/``..`` segments, and NUL bytes are rejected
- a digest field is always excluded from the payload used to compute
  itself
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, GetCoreSchemaHandler
from pydantic_core import core_schema

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def new_id() -> str:
    """Return a new random UUID (string form), the project-wide ID format."""

    return str(uuid.uuid4())


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize ``value`` into canonical JSON bytes.

    ``value`` must already be a JSON-compatible structure (e.g. the
    output of ``BaseModel.model_dump(mode="json")``).
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_digest(data: bytes) -> str:
    """Compute the canonical ``sha256:<hex>`` digest of raw bytes."""

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def is_valid_digest(value: str) -> bool:
    return bool(DIGEST_PATTERN.match(value))


def compute_model_digest(model: BaseModel, *, exclude_fields: set[str]) -> str:
    """Compute the canonical digest of a model, excluding self-referential fields.

    ``exclude_fields`` must name every field that stores (directly or
    inside a nested object) the digest being computed, so the digest
    does not depend on itself.
    """

    payload = model.model_dump(mode="json", exclude=exclude_fields)
    return compute_digest(canonical_json_bytes(payload))


_FORBIDDEN_PATH_SEGMENTS = {"", ".", ".."}


def normalize_relative_path(value: str) -> str:
    """Validate and return a root-relative POSIX-style path.

    Raises ``ValueError`` for absolute paths, empty paths, ``.``/``..``
    segments, empty segments (e.g. ``a//b``), backslashes, and NUL bytes.
    """

    if not isinstance(value, str):  # pragma: no cover - defensive
        raise TypeError("path must be a string")
    if "\x00" in value:
        raise ValueError("path must not contain NUL bytes")
    if value == "":
        raise ValueError("path must not be empty")
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError("path must be root-relative, not absolute")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("path must not include a drive letter")
    if "\\" in value:
        raise ValueError("path must use '/' separators, not '\\'")
    segments = value.split("/")
    for segment in segments:
        if segment in _FORBIDDEN_PATH_SEGMENTS:
            raise ValueError(
                f"path segment {segment!r} is forbidden in {value!r}"
            )
    return value


_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_identifier_slug(value: str) -> str:
    """Validate an identifier that will be used as a single path segment
    (e.g. ``repository_id`` in ``workspace_path_for``/``create_worktree``).

    Codex review H-02: ``repository_id`` had no validator at all before
    this — a value like ``"../../etc"`` or (on Windows) ``"C:\\evil"``
    joined via ``Path.__truediv__`` can escape the intended data root
    entirely (a Windows-absolute segment silently *replaces* the whole
    path rather than being appended). Restricting to a plain slug
    (letters, digits, ``.``/``_``/``-``, 1-128 chars, must start
    alphanumeric) makes an escaping value a validation error at the
    model boundary instead of a path-traversal bug at the filesystem
    boundary.
    """

    if not isinstance(value, str):  # pragma: no cover - defensive
        raise TypeError("identifier must be a string")
    if not _SLUG_PATTERN.match(value):
        raise ValueError(
            f"identifier {value!r} must be 1-128 characters, start with a letter or "
            "digit, and contain only letters, digits, '.', '_', '-' (this also "
            "rejects '.' and '..', which cannot start with an alphanumeric character)"
        )
    return value


class _DigestPydanticAnnotation:
    """Adds core-schema validation for ``sha256:<hex>`` strings."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.chain_schema(
            [
                core_schema.str_schema(),
                core_schema.no_info_plain_validator_function(cls._validate),
            ]
        )

    @staticmethod
    def _validate(value: str) -> str:
        if not is_valid_digest(value):
            raise ValueError(
                "digest must match 'sha256:<64 lowercase hex characters>'"
            )
        return value


Digest = Annotated[str, _DigestPydanticAnnotation]
RelativePath = Annotated[str, AfterValidator(normalize_relative_path)]
IdentifierSlug = Annotated[str, AfterValidator(normalize_identifier_slug)]
