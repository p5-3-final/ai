"""frame-encoder 입출력 이벤트의 추가 규칙(SPEC-019). 필드 일치는 test_event_contracts가 본다."""

import copy
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.events import EVENT_MODELS

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _example(load_schema: SchemaLoader, name: str) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"])[0])


@pytest.mark.parametrize("topic", ["segment.ready", "segment.embedded", "event.candidate.vector"])
def test_spec019_models_are_registered(topic: str) -> None:
    assert topic in EVENT_MODELS


@pytest.mark.parametrize(
    "key",
    [
        # 다른 영상 폴더
        "sources/0192f0d2-0000-7000-8000-000000000000/jobs/0192f100-0000-7000-8000-000000000001"
        "/chunks/0192f100-0000-7000-8000-000000000002/executions/0192f100-0000-7000-8000-000000000003"
        "/blurred/a.jpg",
        # 같은 영상이지만 다른 실행
        "sources/0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f/jobs/0192f100-0000-7000-8000-000000000001"
        "/chunks/0192f100-0000-7000-8000-000000000002/executions/0192f100-0000-7000-8000-000000000009"
        "/blurred/a.jpg",
    ],
)
def test_spec019_r16_frame_outside_execution_folder_is_rejected(
    load_schema: SchemaLoader, key: str
) -> None:
    example = _example(load_schema, "segment.ready")
    segments = cast(list[JsonObject], example["segments"])
    frames = cast(list[JsonObject], segments[0]["frames"])
    frames[0]["key"] = key

    with pytest.raises(ValidationError, match="execution folder"):
        EVENT_MODELS["segment.ready"].model_validate(example)


def test_spec019_r16_crop_outside_execution_folder_is_rejected(load_schema: SchemaLoader) -> None:
    example = _example(load_schema, "segment.ready")
    crops = cast(list[JsonObject], example["crops"])
    crops[0]["key"] = cast(str, crops[0]["key"]).replace("blurred/", "original/")

    with pytest.raises(ValidationError):
        EVENT_MODELS["segment.ready"].model_validate(example)


def test_spec019_r16_segment_needs_four_frames(load_schema: SchemaLoader) -> None:
    example = _example(load_schema, "segment.ready")
    segments = cast(list[JsonObject], example["segments"])
    segments[0]["frames"] = cast(list[JsonObject], segments[0]["frames"])[:3]

    with pytest.raises(ValidationError):
        EVENT_MODELS["segment.ready"].model_validate(example)
