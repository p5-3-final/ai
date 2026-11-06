"""aggregator가 소비·발행하는 이벤트의 계약 규칙(SPEC-027).

필드 이름·필수·enum·예시 왕복은 test_event_contracts가 `EVENT_MODELS` 전체로 본다. 여기서는 등록과
조건부 규칙을 본다.
"""

import copy
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.events import EVENT_MODELS

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _example(load_schema: SchemaLoader, name: str, index: int = 0) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"])[index])


@pytest.mark.parametrize(
    "topic",
    ["analysis.completed", "source.completed", "state.updated", "track.detected", "chunk.manifest"],
)
def test_spec027_r1_aggregator_topics_are_registered(topic: str) -> None:
    """[SPEC-027 R-1·R-29·R-30] aggregator가 다루는 토픽은 계약 테스트 대상이다."""
    assert topic in EVENT_MODELS


def test_spec027_r1_analysis_completed_without_session(load_schema: SchemaLoader) -> None:
    """[SPEC-027 R-1] 세션에 속하지 않은 영상은 session이 null이다."""
    example = _example(load_schema, "analysis.completed", 1)

    parsed = EVENT_MODELS["analysis.completed"].model_validate(example)

    assert parsed.model_dump(by_alias=True)["session"] is None


def test_spec027_r30_analysis_completed_accepts_stream_chunk_scope(
    load_schema: SchemaLoader,
) -> None:
    """[SPEC-027 R-30] 실시간 창 완료는 jobType STREAM·scope chunk·chunkId다."""
    example = _example(load_schema, "analysis.completed")
    example.update(
        jobType="STREAM",
        scope="chunk",
        chunkId="0192fb00-0000-5000-8000-00000000000f",
        partial=True,
        session={
            "sessionId": "0192f0e0-5a6b-7c8d-9e0f-1a2b3c4d5e6f",
            "role": "ROTATION",
            "previousSourceId": None,
        },
    )

    parsed = EVENT_MODELS["analysis.completed"].model_validate(example)

    body = parsed.model_dump(by_alias=True, mode="json")
    assert (body["scope"], body["partial"]) == ("chunk", True)


def test_spec027_r30_chunk_scope_requires_chunk_id(load_schema: SchemaLoader) -> None:
    """[SPEC-027 R-30] 창 완료에는 창 ID가 있어야 대기 집합을 찾는다."""
    example = _example(load_schema, "analysis.completed")
    example.update(jobType="STREAM", scope="chunk")

    with pytest.raises(ValidationError, match="chunkId"):
        EVENT_MODELS["analysis.completed"].model_validate(example)


def test_spec027_r4_source_completed_partial_fields(load_schema: SchemaLoader) -> None:
    """[SPEC-027 R-4·R-28] revision·partial·missingCounts는 선택 필드로 왕복한다."""
    example = _example(load_schema, "source.completed")
    example.update(revision=2, partial=True, missingCounts={"events": 1, "tracks": 0})

    parsed = EVENT_MODELS["source.completed"].model_validate(example)

    body = parsed.model_dump(by_alias=True, mode="json")
    assert (body["revision"], body["missingCounts"]) == (2, {"events": 1, "tracks": 0})


def test_spec027_r29_track_detected_origin_needs_its_fields(load_schema: SchemaLoader) -> None:
    """[SPEC-027 R-29] origin chunk면 tracksKey가 있어야 한다(계약 oneOf)."""
    example = _example(load_schema, "track.detected")
    del example["tracksKey"]

    with pytest.raises(ValidationError, match="tracksKey"):
        EVENT_MODELS["track.detected"].model_validate(example)
