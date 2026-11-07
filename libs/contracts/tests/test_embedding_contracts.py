"""embedding-worker 입출력 이벤트의 추가 규칙(SPEC-025).

이벤트 필드·필수·enum 일치와 예시 왕복은 test_event_contracts가 EVENT_MODELS 전체로 본다.
여기서는 등록 여부와 패턴처럼 그 검사가 보지 않는 규칙을 본다.
"""

import copy
from collections.abc import Callable
from typing import cast, get_args

import pytest
from pydantic import ValidationError
from vlm_contracts import events
from vlm_contracts.events import EVENT_MODELS

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _example(load_schema: SchemaLoader, name: str) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"])[0])


@pytest.mark.parametrize("topic", ["segment.annotated", "embedding.ready"])
def test_spec025_models_are_registered(topic: str) -> None:
    assert topic in EVENT_MODELS


def test_spec025_r8_item_kind_matches_contract(load_schema: SchemaLoader) -> None:
    props = cast(dict[str, JsonObject], load_schema("embedding.ready")["properties"])
    item_kind = getattr(events, "ItemKind", None)
    assert item_kind is not None, "ItemKind가 없다"

    assert set(get_args(item_kind)) == set(cast(list[str], props["itemKind"]["enum"]))


@pytest.mark.parametrize(
    "override",
    [
        {"vectorKey": "sources/x/jobs/y/text-vectors/event/z.json"},
        {"textFormat": "Event_V1"},
        {"embeddingModel": ""},
        {"dimension": 0},
    ],
)
def test_spec025_r8_embedding_ready_patterns_are_enforced(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    example = _example(load_schema, "embedding.ready")

    with pytest.raises(ValidationError):
        EVENT_MODELS["embedding.ready"].model_validate({**example, **override})


@pytest.mark.parametrize("override", [{"text": ""}, {"text": "가" * 501}, {"score": 1.5}])
def test_spec025_r3_segment_annotated_limits_are_enforced(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    example = _example(load_schema, "segment.annotated")

    with pytest.raises(ValidationError):
        EVENT_MODELS["segment.annotated"].model_validate({**example, **override})
