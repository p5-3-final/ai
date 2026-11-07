"""텍스트 벡터 파일 모델이 docs 원본과 맞는지 검증한다(SPEC-025 R-6, R-7)."""

import copy
import importlib
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

JsonObject = dict[str, object]


def _load(contracts_dir: Path, path: str) -> JsonObject:
    loaded: JsonObject = json.loads((contracts_dir / path).read_text(encoding="utf-8"))
    return loaded


def _model(module: str, name: str) -> type[BaseModel]:
    # 모듈을 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    return cast(type[BaseModel], getattr(importlib.import_module(module), name))


def _text_vector_schema(contracts_dir: Path) -> JsonObject:
    return _load(contracts_dir, "vectors/v1/text-vector-file.schema.json")


def _fields(model: type[BaseModel]) -> set[str]:
    return {f.alias or n for n, f in model.model_fields.items()}


def _required(model: type[BaseModel]) -> set[str]:
    return {f.alias or n for n, f in model.model_fields.items() if f.is_required()}


def test_spec025_r6_text_vector_file_example_round_trips(contracts_dir: Path) -> None:
    schema = _text_vector_schema(contracts_dir)
    model = _model("vlm_contracts.vectors", "TextVectorFile")

    for example in cast(list[JsonObject], schema["examples"]):
        parsed = model.model_validate(example)
        assert json.loads(parsed.model_dump_json(by_alias=True)) == example


def test_spec025_r6_text_vector_file_fields_match(contracts_dir: Path) -> None:
    schema = _text_vector_schema(contracts_dir)
    model = _model("vlm_contracts.vectors", "TextVectorFile")

    assert _fields(model) == set(cast(JsonObject, schema["properties"]))
    assert _required(model) == set(cast(list[str], schema["required"])) - {"schemaVersion"}


def test_spec025_r7_metadata_fields_match(contracts_dir: Path) -> None:
    metadata = cast(
        JsonObject, cast(JsonObject, _text_vector_schema(contracts_dir)["properties"])["metadata"]
    )
    model = _model("vlm_contracts.vectors", "TextVectorMetadata")

    assert _fields(model) == set(cast(JsonObject, metadata["properties"]))
    assert _required(model) == set(cast(list[str], metadata["required"]))


def test_spec025_r7_metadata_omits_empty_filters() -> None:
    model = _model("vlm_contracts.vectors", "TextVectorMetadata")

    dumped = json.loads(model(start_ms=0, end_ms=10).model_dump_json(by_alias=True))

    assert dumped == {"startMs": 0, "endMs": 10}


@pytest.mark.parametrize(
    "override",
    [
        {"dimension": 3},
        {"text": ""},
        {"text": "가" * 2001},
        {"textFormat": "event"},
        {"metadata": {"startMs": 0, "endMs": 1, "severity": None}},
        {"metadata": {"startMs": 0, "endMs": 1, "extra": 1}},
    ],
)
def test_spec025_r6_text_vector_file_rules_are_enforced(
    contracts_dir: Path, override: JsonObject
) -> None:
    example = copy.deepcopy(
        cast(list[JsonObject], _text_vector_schema(contracts_dir)["examples"])[0]
    )
    model = _model("vlm_contracts.vectors", "TextVectorFile")

    with pytest.raises(ValidationError):
        model.model_validate({**example, **override})
