"""grounding-worker 입출력 이벤트의 추가 규칙(SPEC-026).

필드·필수·enum 일치와 예시 왕복은 test_event_contracts가 EVENT_MODELS로 본다. 여기서는 계약의
패턴·if/then·폴더 규칙을 본다.
"""

import copy
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.events import EVENT_MODELS, Event

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]
_CLIP = (
    "sources/0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f/groundings/"
    "0192f300-0000-5000-8000-0000000000a1/m/clip.mp4"
)


def _model(topic: str) -> type[Event]:
    assert topic in EVENT_MODELS, f"{topic} 모델이 등록되지 않았다"
    return EVENT_MODELS[topic]


def _example(load_schema: SchemaLoader, topic: str, index: int = 0) -> JsonObject:
    examples = cast(list[JsonObject], load_schema(topic)["examples"])
    return copy.deepcopy(examples[index])


@pytest.mark.parametrize("topic", ["grounding.requested", "grounding.completed"])
def test_spec026_models_are_registered(topic: str) -> None:
    assert topic in EVENT_MODELS


def test_spec026_r15_normalized_key_must_be_the_folder_of_this_message(
    load_schema: SchemaLoader,
) -> None:
    """[SPEC-026 R-15] 다른 영상·정규화의 경로를 가리키면 검증 실패(DLQ)다."""
    message = _example(load_schema, "grounding.requested")
    other = "0192f0d4-0000-7000-8000-0000000000ff"
    source = message["sourceId"]
    message["normalizedKey"] = f"sources/{source}/normalizations/{other}/normalized.mp4"

    with pytest.raises(ValidationError, match="normalizedKey"):
        _model("grounding.requested").model_validate(message)


def test_spec026_r15_normalized_key_pattern_is_enforced(load_schema: SchemaLoader) -> None:
    message = _example(load_schema, "grounding.requested")
    message["normalizedKey"] = "sources/../normalized.mp4"

    with pytest.raises(ValidationError):
        _model("grounding.requested").model_validate(message)


@pytest.mark.parametrize(
    "targets",
    [[], ["forklift", "forklift"], ["Forklift"], ["a" * 81], ["a", "b", "c", "d"]],
)
def test_spec026_r15_targets_follow_the_contract(
    load_schema: SchemaLoader, targets: list[str]
) -> None:
    message = _example(load_schema, "grounding.requested")
    message["targets"] = targets

    with pytest.raises(ValidationError, match="targets"):
        _model("grounding.requested").model_validate(message)


def test_spec026_r10_end_before_start_is_accepted_for_failed_reply(
    load_schema: SchemaLoader,
) -> None:
    """[SPEC-026 R-10] 계약은 순서를 스키마로 막지 않는다. 워커가 FAILED로 알린다."""
    message = _example(load_schema, "grounding.requested")
    segments = cast(list[JsonObject], message["segments"])
    segments[0]["endMs"] = segments[0]["startMs"]

    parsed = _model("grounding.requested").model_validate(message)

    assert parsed.model_dump(by_alias=True)["segments"][0]["endMs"] == segments[0]["startMs"]


@pytest.mark.parametrize(
    "override",
    [{"clipKey": None}, {"resultKey": None}, {"frames": None}, {"errorCode": "GROUNDING_TIMEOUT"}],
)
def test_spec026_r7_ready_requires_clip_result_and_frames(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    ready = _example(load_schema, "grounding.completed", 0)

    with pytest.raises(ValidationError, match="READY"):
        _model("grounding.completed").model_validate({**ready, **override})


@pytest.mark.parametrize(
    "override",
    [
        {"errorCode": None},
        {"frames": []},
        {"clipKey": _CLIP},
    ],
)
def test_spec026_r10_failed_requires_error_code_and_null_results(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    failed = _example(load_schema, "grounding.completed", 1)

    with pytest.raises(ValidationError, match="FAILED"):
        _model("grounding.completed").model_validate({**failed, **override})


def test_spec026_r7_frame_counts_are_bounded(load_schema: SchemaLoader) -> None:
    ready = _example(load_schema, "grounding.completed", 0)
    frames = cast(list[JsonObject], ready["frames"])
    frames[0]["counts"] = {"forklift": 1001}

    with pytest.raises(ValidationError):
        _model("grounding.completed").model_validate(ready)


def test_spec026_r9_grounding_model_is_one_path_segment(load_schema: SchemaLoader) -> None:
    ready = _example(load_schema, "grounding.completed", 0)
    ready["groundingModel"] = "yoloe:v8/s"

    with pytest.raises(ValidationError, match="groundingModel"):
        _model("grounding.completed").model_validate(ready)
