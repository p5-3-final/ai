"""grounding 결과 파일 모델이 docs 원본과 맞는지 검증한다(SPEC-026 R-6, R-17)."""

import copy
import importlib
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

JsonObject = dict[str, object]
_SCHEMA = "groundings/v1/grounding-result.schema.json"


def _load(contracts_dir: Path) -> JsonObject:
    loaded: JsonObject = json.loads((contracts_dir / _SCHEMA).read_text(encoding="utf-8"))
    return loaded


def _model() -> type[BaseModel]:
    # 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    module = importlib.import_module("vlm_contracts.groundings")
    return cast(type[BaseModel], module.GroundingResultFile)


def _example(contracts_dir: Path) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], _load(contracts_dir)["examples"])[0])


def test_spec026_r6_result_example_round_trips(contracts_dir: Path) -> None:
    for example in cast(list[JsonObject], _load(contracts_dir)["examples"]):
        parsed = _model().model_validate(example)
        assert json.loads(parsed.model_dump_json(by_alias=True)) == example


def _props(schema: JsonObject) -> JsonObject:
    return cast(JsonObject, schema["properties"])


def test_spec026_r6_result_fields_and_required_match(contracts_dir: Path) -> None:
    schema = _load(contracts_dir)
    model_schema = cast(JsonObject, _model().model_json_schema(by_alias=True))

    assert set(_props(model_schema)) == set(_props(schema))
    # 상수(schemaVersion)는 모델이 기본값으로 채운다.
    assert set(cast(list[str], model_schema["required"])) == set(
        cast(list[str], schema["required"])
    ) - {"schemaVersion"}
    clip = cast(JsonObject, _props(schema)["clip"])
    defs = cast(dict[str, JsonObject], model_schema["$defs"])
    clip_model = next(d for d in defs.values() if d.get("title") == "GroundingClip")
    assert set(_props(clip_model)) == set(_props(clip))


def test_spec026_r17_score_threshold_and_model_are_recorded(contracts_dir: Path) -> None:
    """[SPEC-026 R-17] 결과 파일은 탐지기 버전과 기준 신뢰도를 담는다."""
    parsed = _model().model_validate(_example(contracts_dir))
    body = parsed.model_dump(by_alias=True)

    assert body["groundingModel"] == "yoloe-v8s-seg-v1"
    assert body["scoreThreshold"] == 0.25


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("scoreThreshold",), 1.5),
        (("sampleFps",), 6),
        (("colors",), {"forklift": "#D0571E"}),
        (("clip", "codec"), "hevc"),
        (("clip", "key"), "sources/x/clip.mp4"),
        (("frames",), []),
    ],
)
def test_spec026_r6_result_rejects_values_outside_contract(
    contracts_dir: Path, path: tuple[str, ...], value: object
) -> None:
    example = _example(contracts_dir)
    target = example
    for part in path[:-1]:
        target = cast(JsonObject, target[part])
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        _model().model_validate(example)


def test_spec026_r6_detection_box_is_normalized(contracts_dir: Path) -> None:
    example = _example(contracts_dir)
    frames = cast(list[JsonObject], example["frames"])
    detections = cast(list[JsonObject], frames[0]["detections"])
    cast(JsonObject, detections[0]["box"])["x"] = 1.2

    with pytest.raises(ValidationError):
        _model().model_validate(example)
