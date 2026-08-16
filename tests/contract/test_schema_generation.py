"""Schema drift test: generated schemas must match what's checked in."""

from __future__ import annotations

from agent_harness.schema_export import (
    DEFAULT_OUTPUT_DIR,
    EXPORTED_MODELS,
    generate_schemas,
    render_schema,
)


def test_checked_in_schemas_match_freshly_generated_schemas():
    generated = generate_schemas()
    mismatches = []

    for filename, text in generated.items():
        checked_in_path = DEFAULT_OUTPUT_DIR / filename
        if not checked_in_path.exists():
            mismatches.append(f"{filename}: missing from schemas/generated/")
            continue
        on_disk = checked_in_path.read_text(encoding="utf-8")
        if on_disk != text:
            mismatches.append(f"{filename}: on-disk content differs from regenerated content")

    assert not mismatches, (
        "Schema drift detected — run `python -m agent_harness.schema_export` "
        f"and commit the result: {mismatches}"
    )


def test_generation_is_deterministic_across_calls():
    first = generate_schemas()
    second = generate_schemas()
    assert first == second


def test_every_exported_model_declares_additional_properties_false():
    for filename, model in EXPORTED_MODELS:
        schema = model.model_json_schema(mode="validation")
        assert schema.get("additionalProperties") is False, (
            f"{filename} does not forbid additional properties"
        )


def test_every_exported_model_has_json_schema_dialect():
    for filename, text in generate_schemas().items():
        assert '"$schema": "https://json-schema.org/draft/2020-12/schema"' in text, (
            f"{filename} is missing the draft 2020-12 $schema declaration"
        )


def test_no_extra_or_missing_generated_files():
    on_disk_files = {p.name for p in DEFAULT_OUTPUT_DIR.glob("*.json")}
    expected_files = {filename for filename, _ in EXPORTED_MODELS}
    assert on_disk_files == expected_files
