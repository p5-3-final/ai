"""후보 파일 모델이 docs 원본과 맞는지 검증한다(SPEC-023 R-1)."""

import copy
import importlib
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from vlm_contracts.events import DetectorCandidate

JsonObject = dict[str, object]
_PATH = "candidates/v1/candidate-file.schema.json"


def _schema(contracts_dir: Path) -> JsonObject:
    loaded: JsonObject = json.loads((contracts_dir / _PATH).read_text(encoding="utf-8"))
    return loaded


def _candidate_file_model() -> type[BaseModel]:
    # 모듈을 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    return cast(type[BaseModel], importlib.import_module("vlm_contracts.candidates").CandidateFile)


def _aliases(model: type[BaseModel]) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}


def _required_aliases(model: type[BaseModel]) -> set[str]:
    return {f.alias or n for n, f in model.model_fields.items() if f.is_required()}


def test_spec023_r1_candidate_file_example_round_trips(contracts_dir: Path) -> None:
    model = _candidate_file_model()

    for example in cast(list[JsonObject], _schema(contracts_dir)["examples"]):
        assert model.model_validate(example).model_dump(mode="json", by_alias=True) == example


def test_spec023_r1_candidate_file_fields_match(contracts_dir: Path) -> None:
    schema = _schema(contracts_dir)
    model = _candidate_file_model()

    assert _aliases(model) == set(cast(JsonObject, schema["properties"]))
    # schemaVersion은 const라 모델이 기본값으로 채운다.
    assert _required_aliases(model) == set(cast(list[str], schema["required"])) - {"schemaVersion"}


def test_spec023_r1_candidate_item_fields_match(contracts_dir: Path) -> None:
    item = cast(JsonObject, cast(JsonObject, _schema(contracts_dir)["$defs"])["candidate"])

    assert _aliases(DetectorCandidate) == set(cast(JsonObject, item["properties"]))
    assert _required_aliases(DetectorCandidate) == set(cast(list[str], item["required"]))


def test_spec023_r2_unknown_field_in_file_is_rejected(contracts_dir: Path) -> None:
    example = copy.deepcopy(cast(list[JsonObject], _schema(contracts_dir)["examples"])[0])
    example["extra"] = 1

    with pytest.raises(ValidationError):
        _candidate_file_model().model_validate(example)
