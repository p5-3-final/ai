"""track-analyzer 입출력 이벤트와 결과 형식의 추가 규칙(SPEC-024).

이벤트 필드 일치는 test_event_contracts가 본다. 여기서는 결과 형식 2종의 필드·enum과 계약의
if/then 규칙을 본다.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from vlm_contracts import events
from vlm_contracts.events import EVENT_MODELS

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _examples(load_schema: SchemaLoader, name: str) -> list[JsonObject]:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"]))


def _result_model(name: str) -> type[BaseModel]:
    model = getattr(events, name, None)
    assert isinstance(model, type), f"{name} 모델이 없다"
    assert issubclass(model, BaseModel)
    return model


@pytest.mark.parametrize("topic", ["attribute.requested", "track.analyzed"])
def test_spec024_models_are_registered(topic: str) -> None:
    assert topic in EVENT_MODELS


@pytest.mark.parametrize(
    ("schema_name", "model_name"),
    [("_track-attribute", "TrackAttribute"), ("_object-answer", "ObjectAnswer")],
)
def test_spec024_r3_result_shapes_match_contract(
    load_schema: SchemaLoader, schema_name: str, model_name: str
) -> None:
    schema = load_schema(schema_name)
    model = _result_model(model_name)
    fields = {f.alias or n for n, f in model.model_fields.items()}
    required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}

    assert fields == set(cast(JsonObject, schema["properties"]))
    assert required == set(cast(list[str], schema["required"]))
    for example in cast(list[JsonObject], schema["examples"]):
        parsed = model.model_validate(example)
        assert json.loads(parsed.model_dump_json(by_alias=True)) == example


def test_spec024_r3_ppe_and_role_values_match_taxonomy(contracts_dir: Path) -> None:
    taxonomy = json.loads(
        (contracts_dir / "taxonomy/v2/taxonomy.schema.json").read_text(encoding="utf-8")
    )
    defs = cast(dict[str, JsonObject], taxonomy["$defs"])
    model = _result_model("TrackAttribute")
    props = cast(dict[str, JsonObject], model.model_json_schema(by_alias=True)["properties"])

    def values(prop: JsonObject) -> set[object]:
        found: set[object] = set()
        for option in cast(list[JsonObject], prop["anyOf"]):
            found |= set(cast(list[object], option.get("enum", [])))
        return found

    assert values(props["helmet"]) == set(cast(list[object], defs["ppeState"]["enum"]))
    assert values(props["vest"]) == set(cast(list[object], defs["ppeState"]["enum"]))
    assert values(props["role"]) == set(cast(list[object], defs["workerRole"]["enum"]))


def test_spec024_r3_equipment_must_be_unique(load_schema: SchemaLoader) -> None:
    attribute = _examples(load_schema, "_track-attribute")[0]

    with pytest.raises(ValidationError, match="equipment"):
        _result_model("TrackAttribute").model_validate(
            {**attribute, "equipment": ["ladder", "ladder"]}
        )


def _ready_click(load_schema: SchemaLoader) -> JsonObject:
    return _examples(load_schema, "track.analyzed")[0]


def _failed_question(load_schema: SchemaLoader) -> JsonObject:
    return _examples(load_schema, "track.analyzed")[1]


@pytest.mark.parametrize(
    "override",
    [
        {"attributes": None},
        {"answer": {"verificationStatus": "unknown", "answerable": None, "answer": None}},
    ],
)
def test_spec024_r3_ready_attribute_needs_attributes_and_no_answer(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    with pytest.raises(ValidationError, match="attributes"):
        EVENT_MODELS["track.analyzed"].model_validate({**_ready_click(load_schema), **override})


def test_spec024_r3_ready_question_needs_answer_and_no_attributes(
    load_schema: SchemaLoader,
) -> None:
    ready = _ready_click(load_schema)
    with pytest.raises(ValidationError, match="answer"):
        EVENT_MODELS["track.analyzed"].model_validate({**ready, "reason": "question"})


def test_spec024_r3_ready_question_round_trips(load_schema: SchemaLoader) -> None:
    ready = {
        **_ready_click(load_schema),
        "reason": "question",
        "attributes": None,
        "answer": {"verificationStatus": "confirmed", "answerable": True, "answer": "무전기"},
    }

    parsed = EVENT_MODELS["track.analyzed"].model_validate(ready)

    assert json.loads(parsed.model_dump_json(by_alias=True)) == ready


@pytest.mark.parametrize(
    "override",
    [
        {"errorCode": None},
        {
            "attributes": {
                "verificationStatus": "unknown",
                "helmet": None,
                "vest": None,
                "role": None,
                "equipment": [],
                "description": None,
            }
        },
    ],
)
def test_spec024_r4_failed_needs_error_code_and_no_result(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    with pytest.raises(ValidationError, match="FAILED"):
        EVENT_MODELS["track.analyzed"].model_validate({**_failed_question(load_schema), **override})


def _click(load_schema: SchemaLoader) -> JsonObject:
    return _examples(load_schema, "attribute.requested")[0]


def _question(load_schema: SchemaLoader) -> JsonObject:
    return _examples(load_schema, "attribute.requested")[1]


def test_spec024_r7_question_requires_question_text(load_schema: SchemaLoader) -> None:
    with pytest.raises(ValidationError, match="question"):
        EVENT_MODELS["attribute.requested"].model_validate(
            {**_question(load_schema), "question": None}
        )


@pytest.mark.parametrize(
    "override",
    [
        {"question": "무엇을 들고 있나요?"},
        {
            "evidenceFrameKeys": [
                "sources/0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f/jobs/x/blurred/cand-d2-0.jpg"
            ]
        },
    ],
)
def test_spec024_r7_click_has_no_question_or_scene_frames(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    with pytest.raises(ValidationError, match="question"):
        EVENT_MODELS["attribute.requested"].model_validate({**_click(load_schema), **override})


def test_spec024_r7_crops_are_one_to_three(load_schema: SchemaLoader) -> None:
    click = _click(load_schema)
    crop = cast(list[JsonObject], click["crops"])[0]

    for crops in ([], [crop] * 4):
        with pytest.raises(ValidationError, match="crops"):
            EVENT_MODELS["attribute.requested"].model_validate({**click, "crops": crops})


_OTHER_SOURCE = "0192f0d2-0000-7000-8000-000000000000"
_PREFIX = "sources/0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f/"


@pytest.mark.parametrize(
    "key",
    [
        # 다른 영상 폴더
        f"sources/{_OTHER_SOURCE}/jobs/j/blurred/a.jpg",
        # 상위 폴더로 나가 다른 영상을 가리킴
        f"{_PREFIX}../{_OTHER_SOURCE}/blurred/a.jpg",
        f"{_PREFIX}jobs/./blurred/a.jpg",
        f"{_PREFIX}jobs//blurred/a.jpg",
    ],
)
def test_spec024_r6_crop_outside_source_folder_is_rejected(
    load_schema: SchemaLoader, key: str
) -> None:
    click = _click(load_schema)
    cast(list[JsonObject], click["crops"])[0]["key"] = key

    with pytest.raises(ValidationError, match="source folder"):
        EVENT_MODELS["attribute.requested"].model_validate(click)


def test_spec024_r6_scene_frame_outside_source_folder_is_rejected(
    load_schema: SchemaLoader,
) -> None:
    question = _question(load_schema)
    question["evidenceFrameKeys"] = [f"sources/{_OTHER_SOURCE}/jobs/j/blurred/a.jpg"]

    with pytest.raises(ValidationError, match="source folder"):
        EVENT_MODELS["attribute.requested"].model_validate(question)
