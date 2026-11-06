"""벡터 파일 모델과 분류 체계 유형 목록이 docs 원본과 맞는지 검증한다(SPEC-019 R-3, R-15)."""

import importlib
import json
from pathlib import Path
from typing import cast, get_args

from pydantic import BaseModel
from vlm_contracts.events import DetectableEventType

JsonObject = dict[str, object]


def _load(contracts_dir: Path, path: str) -> JsonObject:
    loaded: JsonObject = json.loads((contracts_dir / path).read_text(encoding="utf-8"))
    return loaded


def _vector_file_model() -> type[BaseModel]:
    # 모듈을 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    return cast(type[BaseModel], importlib.import_module("vlm_contracts.vectors").VectorFile)


def test_spec019_r3_vector_file_example_round_trips(contracts_dir: Path) -> None:
    schema = _load(contracts_dir, "vectors/v1/vector-file.schema.json")
    model = _vector_file_model()

    for example in cast(list[JsonObject], schema["examples"]):
        parsed = model.model_validate(example)
        assert parsed.model_dump(mode="json", by_alias=True) == example


def test_spec019_r3_vector_file_fields_match(contracts_dir: Path) -> None:
    schema = _load(contracts_dir, "vectors/v1/vector-file.schema.json")
    model = _vector_file_model()
    model_schema = cast(JsonObject, model.model_json_schema(by_alias=True))

    assert set(cast(JsonObject, model_schema["properties"])) == set(
        cast(JsonObject, schema["properties"])
    )


def test_spec019_r15_detectable_event_types_match_taxonomy(contracts_dir: Path) -> None:
    taxonomy = _load(contracts_dir, "taxonomy/v2/taxonomy.schema.json")
    event_type = cast(JsonObject, cast(JsonObject, taxonomy["$defs"])["eventType"])

    expected = set(cast(list[str], event_type["enum"])) - {"other"}
    assert set(get_args(DetectableEventType.__value__)) == expected
