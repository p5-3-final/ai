"""이벤트 모델이 docs 레포의 JSON Schema 원본과 맞는지 검증한다."""

import json
from collections.abc import Callable
from typing import cast

import pytest
from pydantic import ValidationError
from vlm_contracts.events import EVENT_MODELS, Event, VideoUploaded

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _properties(schema: JsonObject) -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], schema.get("properties", {}))


def _required(schema: JsonObject) -> set[str]:
    return set(cast(list[str], schema.get("required", [])))


def _allowed_values(prop: JsonObject) -> set[object] | None:
    if "const" in prop:
        return {prop["const"]}
    if "enum" in prop:
        return set(cast(list[object], prop["enum"]))
    return None


@pytest.fixture(scope="module")
def envelope(load_schema: SchemaLoader) -> JsonObject:
    return load_schema("_envelope")


@pytest.mark.parametrize(("topic", "model"), sorted(EVENT_MODELS.items()))
class TestEventContract:
    def test_examples_round_trip(
        self, load_schema: SchemaLoader, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(topic)
        examples = cast(list[JsonObject], schema["examples"])
        assert examples, "계약에 예시가 있어야 왕복 검증을 할 수 있다"

        for example in examples:
            parsed = model.model_validate(example)
            assert json.loads(parsed.model_dump_json(by_alias=True)) == example

    def test_field_names_match(
        self, load_schema: SchemaLoader, envelope: JsonObject, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(topic)
        expected = set(_properties(envelope)) | set(_properties(schema))

        actual = {field.alias or name for name, field in model.model_fields.items()}

        assert actual == expected

    def test_required_fields_are_required(
        self, load_schema: SchemaLoader, envelope: JsonObject, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(topic)
        contract_required = _required(envelope) | _required(schema)
        # 계약의 const 필드(eventType, schemaVersion)는 모델이 기본값으로 채운다.
        consts = {k for k, v in _properties(schema).items() if "const" in v}

        model_required = {
            field.alias or name for name, field in model.model_fields.items() if field.is_required()
        }

        assert model_required == contract_required - consts

    def test_enum_and_const_values_match(
        self, load_schema: SchemaLoader, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(topic)
        model_props = _properties(cast(JsonObject, model.model_json_schema(by_alias=True)))

        for name, prop in _properties(schema).items():
            expected = _allowed_values(prop)
            if expected is not None:
                assert _allowed_values(model_props[name]) == expected, name


def test_unknown_fields_are_ignored_for_forward_compatibility(load_schema: SchemaLoader) -> None:
    # 계약은 필드 추가만 허용하므로 소비자는 모르는 필드가 와도 실패하지 않아야 한다.
    example = cast(list[JsonObject], load_schema("video.uploaded")["examples"])[0]

    parsed = VideoUploaded.model_validate({**example, "addedLater": 1})

    assert parsed.storage_key == example["storageKey"]


def test_naive_timestamp_is_rejected(load_schema: SchemaLoader) -> None:
    example = cast(list[JsonObject], load_schema("video.uploaded")["examples"])[0]

    with pytest.raises(ValidationError, match="occurredAt"):
        VideoUploaded.model_validate({**example, "occurredAt": "2026-11-02T09:31:12"})


def test_events_are_registered_by_topic() -> None:
    assert {"video.uploaded", "video.deleted"} <= set(EVENT_MODELS)
    for topic, model in EVENT_MODELS.items():
        assert model.model_fields["event_type"].default == topic
