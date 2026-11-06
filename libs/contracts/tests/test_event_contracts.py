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
    if "anyOf" in prop:
        # Pydantic은 `Literal[...] | None`을 anyOf로, 계약은 null을 넣은 enum으로 쓴다.
        values: set[object] = set()
        for option in cast(list[JsonObject], prop["anyOf"]):
            if option.get("type") == "null":
                values.add(None)
                continue
            allowed = _allowed_values(option)
            if allowed is None:
                return None
            values |= allowed
        return values
    return None


def _schema_name(topic: str, model: type[Event]) -> str:
    """버전별 원본(`<topic>.v<N>`)이 있으면 그 파일, 없으면 `<topic>`이다.

    여러 버전을 함께 두는 토픽은 `<topic>.schema.json`이 버전들의 합집합이라 필드를 비교할 수 없다.
    """
    version = model.model_fields["schema_version"].default
    return f"{topic}.v{version}" if version > 1 else topic


@pytest.fixture(scope="module")
def envelope(load_schema: SchemaLoader) -> JsonObject:
    return load_schema("_envelope")


@pytest.mark.parametrize(("topic", "model"), sorted(EVENT_MODELS.items()))
class TestEventContract:
    def test_examples_round_trip(
        self, load_schema: SchemaLoader, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(_schema_name(topic, model))
        examples = cast(list[JsonObject], schema["examples"])
        assert examples, "계약에 예시가 있어야 왕복 검증을 할 수 있다"

        for example in examples:
            parsed = model.model_validate(example)
            assert json.loads(parsed.model_dump_json(by_alias=True)) == example

    def test_field_names_match(
        self, load_schema: SchemaLoader, envelope: JsonObject, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(_schema_name(topic, model))
        expected = set(_properties(envelope)) | set(_properties(schema))

        actual = {field.alias or name for name, field in model.model_fields.items()}

        assert actual == expected

    def test_required_fields_are_required(
        self, load_schema: SchemaLoader, envelope: JsonObject, topic: str, model: type[Event]
    ) -> None:
        schema = load_schema(_schema_name(topic, model))
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
        schema = load_schema(_schema_name(topic, model))
        model_props = _properties(cast(JsonObject, model.model_json_schema(by_alias=True)))

        required = _required(schema)
        for name, prop in _properties(schema).items():
            expected = _allowed_values(prop)
            if expected is None:
                continue
            actual = _allowed_values(model_props[name])
            # 계약에서 빠질 수 있는 필드는 모델이 None으로 나타내고 JSON에서는 뺀다.
            # 계약이 null을 값으로 허용하는 필드(`unverifiedReason`)는 None도 비교한다.
            if name not in required and actual is not None and None not in expected:
                actual.discard(None)
            assert actual == expected, name


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


def test_normalized_events_are_registered() -> None:
    assert {"video.normalizing", "video.normalized"} <= set(EVENT_MODELS)


@pytest.mark.parametrize(
    "override",
    [
        {"rejectReason": "TOO_LONG"},
        {"normalizedKey": None},
        {"durationMs": None},
    ],
)
def test_ready_result_requires_media_fields_and_no_reason(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    ready = cast(list[JsonObject], load_schema("video.normalized.v2")["examples"])[0]
    model = EVENT_MODELS["video.normalized"]

    with pytest.raises(ValidationError, match="READY"):
        model.model_validate({**ready, **override})


@pytest.mark.parametrize(
    "override",
    [{"rejectReason": None}, {"normalizedKey": "sources/x/normalized/video.mp4"}, {"fps": 30}],
)
def test_rejected_result_requires_reason_and_no_media_fields(
    load_schema: SchemaLoader, override: JsonObject
) -> None:
    rejected = cast(list[JsonObject], load_schema("video.normalized.v2")["examples"])[1]
    model = EVENT_MODELS["video.normalized"]

    with pytest.raises(ValidationError, match="REJECTED"):
        model.model_validate({**rejected, **override})


def test_v2_is_what_normalize_publishes() -> None:
    assert EVENT_MODELS["video.normalized"].model_fields["schema_version"].default == 2


def test_ready_duration_seconds_must_equal_milliseconds(load_schema: SchemaLoader) -> None:
    ready = cast(list[JsonObject], load_schema("video.normalized.v2")["examples"])[0]
    model = EVENT_MODELS["video.normalized"]

    with pytest.raises(ValidationError, match="durationSec"):
        model.model_validate({**ready, "durationSec": 125.0})


def test_normalization_version_pattern_is_enforced(load_schema: SchemaLoader) -> None:
    ready = cast(list[JsonObject], load_schema("video.normalized.v2")["examples"])[0]
    model = EVENT_MODELS["video.normalized"]

    with pytest.raises(ValidationError, match="normalizationVersion"):
        model.model_validate({**ready, "normalizationVersion": "bad version!"})


def _analyzed_examples(load_schema: SchemaLoader) -> tuple[JsonObject, JsonObject]:
    vector, rule = cast(list[JsonObject], load_schema("event.analyzed")["examples"])
    return vector, rule


def test_rule_method_requires_detector_candidate(load_schema: SchemaLoader) -> None:
    _, rule = _analyzed_examples(load_schema)
    without = {k: v for k, v in rule.items() if k != "detectorCandidate"}

    with pytest.raises(ValidationError, match="detectorCandidate"):
        EVENT_MODELS["event.analyzed"].model_validate(without)


@pytest.mark.parametrize("method", ["rule", "rule+carry"])
def test_rule_methods_without_vlm_have_no_judgments(load_schema: SchemaLoader, method: str) -> None:
    vector, rule = _analyzed_examples(load_schema)
    judgments = cast(list[JsonObject], vector["judgments"])

    with pytest.raises(ValidationError, match="judgments"):
        EVENT_MODELS["event.analyzed"].model_validate(
            {**rule, "judgmentMethod": method, "judgments": judgments[:1]}
        )


@pytest.mark.parametrize("method", ["rule+vlm", "vector+vlm"])
def test_vlm_methods_require_a_judgment(load_schema: SchemaLoader, method: str) -> None:
    vector, rule = _analyzed_examples(load_schema)
    base = rule if method.startswith("rule") else vector

    with pytest.raises(ValidationError, match="judgments"):
        EVENT_MODELS["event.analyzed"].model_validate(
            {**base, "judgmentMethod": method, "judgments": []}
        )


def test_unverified_reason_and_job_type_round_trip(load_schema: SchemaLoader) -> None:
    vector, _ = _analyzed_examples(load_schema)
    message = {**vector, "jobType": "STREAM", "unverifiedReason": "call_cap_reached"}

    parsed = EVENT_MODELS["event.analyzed"].model_validate(message)

    assert json.loads(parsed.model_dump_json(by_alias=True)) == message
