"""Deterministic JSON Schema export.

Pydantic models are the single source of truth; this module only turns
them into ``schemas/generated/*.json``. Nothing here should encode schema
knowledge that isn't already on the models themselves — a schema drift
test (``tests/contract/test_schema_generation.py``) fails the build if
regenerating produces different bytes than what's checked in.

Usage: ``python -m agent_harness.schema_export [output_dir]``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from agent_harness.domain.models import (
    EvidenceRecord,
    PolicyDecision,
    ReworkContract,
    TaskContract,
    VerificationResult,
    WorkerResult,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# Ordered so output is deterministic regardless of dict/import ordering.
EXPORTED_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("task_contract.json", TaskContract),
    ("worker_result.json", WorkerResult),
    ("verification_result.json", VerificationResult),
    ("rework_contract.json", ReworkContract),
    ("policy_decision.json", PolicyDecision),
    ("evidence_record.json", EvidenceRecord),
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "schemas" / "generated"


def build_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema(mode="validation")
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def render_schema(model: type[BaseModel]) -> str:
    """Canonical on-disk form: sorted keys, 2-space indent, trailing newline."""

    return json.dumps(build_schema(model), indent=2, sort_keys=True) + "\n"


def generate_schemas() -> dict[str, str]:
    """Return ``{filename: rendered_json_text}`` for every exported model."""

    return {
        filename: render_schema(model) for filename, model in EXPORTED_MODELS
    }


def write_schemas(output_dir: Path = DEFAULT_OUTPUT_DIR) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in generate_schemas().items():
        path = output_dir / filename
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output_dir = Path(args[0]) if args else DEFAULT_OUTPUT_DIR
    written = write_schemas(output_dir)
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
