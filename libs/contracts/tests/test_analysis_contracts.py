"""event-analyzer 출력 이벤트와 판정 결과(VLMAnalysis)의 계약 규칙(SPEC-020).

이벤트 필드 일치는 test_event_contracts가 본다. 여기서는 `analysis`의 공용 스키마와 조건부
필드를 본다.
"""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError
from vlm_contracts.events import EVENT_MODELS

JsonObject = dict[str, object]
SchemaLoader = Callable[[str], JsonObject]


def _example(load_schema: SchemaLoader, name: str, index: int = 0) -> JsonObject:
    return copy.deepcopy(cast(list[JsonObject], load_schema(name)["examples"])[index])


def _analysis_model() -> type[BaseModel]:
    model = EVENT_MODELS["event.analyzed"].model_fields["analysis"].annotation
    assert isinstance(model, type)
    assert issubclass(model, BaseModel)
    return model


def _enum(prop: JsonObject, defs: dict[str, JsonObject]) -> set[object]:
    """모델 JSON Schema 속성의 허용 값. 타입 별칭은 `$defs` 참조라 따라간다."""
    if "$ref" in prop:
        return _enum(defs[cast(str, prop["$ref"]).rsplit("/", 1)[-1]], defs)
    if "enum" in prop:
        return set(cast(list[object], prop["enum"]))
    if "const" in prop:
        return {prop["const"]}
    values: set[object] = set()
    for option in cast(list[JsonObject], prop.get("anyOf", [])):
        values |= {None} if option.get("type") == "null" else _enum(option, defs)
    return values


def test_spec020_r10_event_analyzed_is_registered() -> None:
    assert "event.analyzed" in EVENT_MODELS


def test_spec020_r10_vector_method_requires_vector_candidate(load_schema: SchemaLoader) -> None:
    example = _example(load_schema, "event.analyzed")
    del example["vectorCandidate"]

    with pytest.raises(ValidationError, match="vectorCandidate"):
        EVENT_MODELS["event.analyzed"].model_validate(example)


def test_spec020_r10_analysis_fields_match_contract(load_schema: SchemaLoader) -> None:
    schema = load_schema("_vlm-analysis")
    model = _analysis_model()

    fields = {f.alias or n for n, f in model.model_fields.items()}
    required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}

    assert fields == set(cast(JsonObject, schema["properties"]))
    assert required == set(cast(list[str], schema["required"]))


@pytest.mark.parametrize("index", [0, 1])
def test_spec020_r10_analysis_examples_round_trip(load_schema: SchemaLoader, index: int) -> None:
    example = _example(load_schema, "_vlm-analysis", index)

    parsed = _analysis_model().model_validate(example)

    assert json.loads(parsed.model_dump_json(by_alias=True)) == example


def test_spec020_r10_analysis_enums_match_taxonomy(contracts_dir: Path) -> None:
    taxonomy = json.loads(
        (contracts_dir / "taxonomy" / "v2" / "taxonomy.schema.json").read_text(encoding="utf-8")
    )
    defs = cast(dict[str, JsonObject], taxonomy["$defs"])
    model_schema = _analysis_model().model_json_schema(by_alias=True)
    props = cast(dict[str, JsonObject], model_schema["properties"])
    model_defs = cast(dict[str, JsonObject], model_schema.get("$defs", {}))

    assert _enum(props["eventType"], model_defs) == set(
        cast(list[object], defs["eventType"]["enum"])
    )
    assert _enum(props["severity"], model_defs) == {
        *cast(list[object], defs["severity"]["enum"]),
        None,
    }


def test_spec020_r10_risk_detected_requires_risk_type(load_schema: SchemaLoader) -> None:
    # 계약의 if/then: riskDetected가 true면 riskType·riskReason이 문자열이다.
    example = _example(load_schema, "_vlm-analysis")
    example["riskType"] = None

    with pytest.raises(ValidationError, match="riskType"):
        _analysis_model().model_validate(example)
