"""모델 manifest 모델이 docs 레포의 JSON Schema 원본과 맞는지 검증한다(ADR-0010 ②)."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.models import ModelManifest

JsonObject = dict[str, object]

ENCODER: JsonObject = {
    "kind": "image_text_encoder",
    "modelId": "open_clip:MobileCLIP2-S0:dfndr2b",
    "version": "dfndr2b",
    "files": [{"name": "open_clip_model.safetensors", "sha256": "a" * 64, "size": 10}],
    "dimension": 512,
    "source": {"type": "public", "origin": "huggingface:timm/MobileCLIP2-S0-OpenCLIP"},
    "license": "Apple AMLR",
}


@pytest.fixture(scope="module")
def schema(contracts_dir: Path) -> JsonObject:
    path = contracts_dir / "models" / "manifest.schema.json"
    loaded: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _properties(schema: JsonObject) -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], schema["properties"])


def _defs(schema: JsonObject) -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], schema["$defs"])


def test_field_names_and_required_match(schema: JsonObject) -> None:
    generated = ModelManifest.model_json_schema(by_alias=True)

    assert set(_properties(generated)) == set(_properties(schema))
    assert set(cast(list[str], generated["required"])) == set(cast(list[str], schema["required"]))


def test_kinds_match(schema: JsonObject) -> None:
    kinds = set(cast(list[str], _properties(schema)["kind"]["enum"]))

    for kind in kinds:
        extra: JsonObject = {"dimension": 8} if kind == "image_text_encoder" else {}
        extra |= {"classes": ["person"]} if kind == "detector" else {}
        payload = {k: v for k, v in ENCODER.items() if k not in ("dimension", "classes")}
        assert ModelManifest.model_validate(payload | {"kind": kind} | extra).kind == kind
    with pytest.raises(ValidationError):
        ModelManifest.model_validate(ENCODER | {"kind": "tokenizer"})


def test_file_and_source_fields_match(schema: JsonObject) -> None:
    file_schema = _defs(schema)["file"]
    file_model = ModelManifest.model_json_schema(by_alias=True)["$defs"]["ManifestFile"]
    assert set(_properties(file_model)) == set(_properties(file_schema))

    variants = cast(list[JsonObject], _defs(schema)["source"]["oneOf"])
    for variant in variants:
        props = _properties(variant)
        payload = {name: "x" for name in cast(list[str], variant["required"])}
        payload["type"] = cast(str, props["type"]["const"])
        parsed = ModelManifest.model_validate(ENCODER | {"source": payload})
        assert parsed.source.type == payload["type"]


def test_example_round_trips() -> None:
    parsed = ModelManifest.model_validate(ENCODER)

    assert json.loads(parsed.model_dump_json(by_alias=True, exclude_none=True)) == ENCODER


@pytest.mark.parametrize(
    ("kind", "missing"), [("image_text_encoder", "dimension"), ("detector", "classes")]
)
def test_kind_specific_field_is_required(kind: str, missing: str) -> None:
    payload = {k: v for k, v in ENCODER.items() if k not in ("dimension", "classes")}

    with pytest.raises(ValidationError, match=missing):
        ModelManifest.model_validate(payload | {"kind": kind})


def test_rejects_file_name_with_folder() -> None:
    files = [{"name": "../x.safetensors", "sha256": "a" * 64, "size": 1}]

    with pytest.raises(ValidationError):
        ModelManifest.model_validate(ENCODER | {"files": files})


def test_class_names_follow_taxonomy_pattern(contracts_dir: Path) -> None:
    taxonomy = json.loads(
        (contracts_dir / "taxonomy" / "v2" / "taxonomy.schema.json").read_text(encoding="utf-8")
    )
    pattern = taxonomy["$defs"]["className"]["pattern"]
    detector = ENCODER | {"kind": "detector", "classes": ["person", "no_helmet"]}
    assert pattern == r"^[a-z][a-z0-9_]*$"

    assert ModelManifest.model_validate(detector).classes == ["person", "no_helmet"]
    with pytest.raises(ValidationError):
        ModelManifest.model_validate(detector | {"classes": ["Forklift"]})
