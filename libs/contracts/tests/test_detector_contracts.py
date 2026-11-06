"""탐지기·규칙 후보 경로 계약 규칙(SPEC-023).

필드·필수·enum·예시 왕복은 test_event_contracts가 `EVENT_MODELS` 전체에 대해 본다. 여기서는
스키마 패턴만으로는 드러나지 않는 메시지 안 일관성 규칙을 본다.
"""

import copy
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.events import EVENT_MODELS, Event

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _example(load_schema: SchemaLoader, name: str, index: int = 0) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"])[index])


def _model(topic: str) -> type[Event]:
    # 등록되지 않았으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 실행 중에 찾는다.
    assert topic in EVENT_MODELS, f"{topic} 모델이 등록되지 않았다"
    return EVENT_MODELS[topic]


def _prefix(example: JsonObject) -> str:
    return (
        f"sources/{example['sourceId']}/jobs/{example['jobId']}/chunks/{example['chunkId']}"
        f"/executions/{example['executionId']}/"
    )


@pytest.mark.parametrize("topic", ["event.candidate", "event.manifest"])
def test_spec023_detector_events_are_registered(topic: str) -> None:
    assert _model(topic).model_fields["event_type"].default == topic


def test_spec023_r14_candidates_and_key_are_exclusive(load_schema: SchemaLoader) -> None:
    both = _example(load_schema, "event.candidate")
    both["candidatesKey"] = _prefix(both) + "candidates.json"
    neither = _example(load_schema, "event.candidate")
    del neither["candidates"]

    for message in (both, neither):
        with pytest.raises(ValidationError, match="exactly one"):
            _model("event.candidate").model_validate(message)


def test_spec023_r1_candidates_key_in_execution_folder_is_accepted(
    load_schema: SchemaLoader,
) -> None:
    message = _example(load_schema, "event.candidate")
    del message["candidates"]
    message["candidatesKey"] = _prefix(message) + "candidates.json"

    parsed = _model("event.candidate").model_validate(message)

    assert parsed.model_dump(by_alias=True, mode="json") == message


def test_spec023_r14_candidates_key_of_another_execution_is_rejected(
    load_schema: SchemaLoader,
) -> None:
    message = _example(load_schema, "event.candidate")
    del message["candidates"]
    other = {**message, "executionId": "0192f100-0000-7000-8000-0000000000ff"}
    message["candidatesKey"] = _prefix(other) + "candidates.json"

    with pytest.raises(ValidationError, match="candidatesKey"):
        _model("event.candidate").model_validate(message)


def test_spec023_d10_evidence_frame_outside_execution_folder_is_rejected(
    load_schema: SchemaLoader,
) -> None:
    """[SPEC-023 R-14, D-10] 규칙 후보는 ai-gateway를 거치지 않아 다른 실행의 프레임을 막는다."""
    message = _example(load_schema, "event.candidate")
    candidates = cast(list[JsonObject], message["candidates"])
    other = {**message, "chunkId": "0192f100-0000-7000-8000-0000000000ee"}
    candidates[1]["evidenceFrameKeys"] = [_prefix(other) + "blurred/x.jpg"]

    with pytest.raises(ValidationError, match="execution folder"):
        _model("event.candidate").model_validate(message)


def test_spec023_r2_inline_candidate_count_must_match(load_schema: SchemaLoader) -> None:
    message = _example(load_schema, "event.candidate")
    message["candidateCount"] = 3

    with pytest.raises(ValidationError, match="candidateCount"):
        _model("event.candidate").model_validate(message)


def test_spec023_r10_manifest_counts_must_add_up(load_schema: SchemaLoader) -> None:
    manifest = _example(load_schema, "event.manifest")
    manifest["mergedCount"] = 1

    with pytest.raises(ValidationError, match="analyzedCount"):
        _model("event.manifest").model_validate(manifest)


def test_spec023_r20_manifest_v51_counts_round_trip(load_schema: SchemaLoader) -> None:
    manifest = _example(load_schema, "event.manifest")
    manifest.update(jobType="STREAM", carriedCount=1, capReachedCount=0, budgetExceededCount=0)

    parsed = _model("event.manifest").model_validate(manifest)

    assert parsed.model_dump(by_alias=True, mode="json") == manifest
