"""결과 파일 모델이 docs 원본 `aggregates/v1/source-result`와 맞는지 검증한다(SPEC-027 R-20)."""

import importlib
import json
from pathlib import Path
from typing import cast

from pydantic import BaseModel

JsonObject = dict[str, object]


def _schema(contracts_dir: Path) -> JsonObject:
    loaded: JsonObject = json.loads(
        (contracts_dir / "aggregates/v1/source-result.schema.json").read_text(encoding="utf-8")
    )
    return loaded


def _model() -> type[BaseModel]:
    # 모듈을 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    return cast(type[BaseModel], importlib.import_module("vlm_contracts.aggregates").SourceResult)


def _fields(model: type[BaseModel]) -> set[str]:
    return {f.alias or n for n, f in model.model_fields.items()}


def _contract_required(schema: JsonObject) -> set[str]:
    # const 필드(schemaVersion, eventType 등)는 모델이 기본값으로 채운다.
    props = cast(dict[str, JsonObject], schema["properties"])
    consts = {k for k, v in props.items() if "const" in v}
    return set(cast(list[str], schema["required"])) - consts


def test_spec027_r20_source_result_example_round_trips(contracts_dir: Path) -> None:
    """[SPEC-027 R-20] SPEC-027 범위의 예시(첫 번째)는 같은 JSON으로 돌아온다."""
    example = cast(list[JsonObject], _schema(contracts_dir)["examples"])[0]

    parsed = _model().model_validate(example)

    assert parsed.model_dump(mode="json", by_alias=True) == example


def test_spec027_r20_source_result_fields_match(contracts_dir: Path) -> None:
    schema = _schema(contracts_dir)
    model = _model()
    required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}

    assert _fields(model) == set(cast(JsonObject, schema["properties"]))
    assert required == _contract_required(schema)


def _items(schema: JsonObject, name: str) -> JsonObject:
    prop = cast(JsonObject, cast(JsonObject, schema["properties"])[name])
    return cast(JsonObject, prop["items"])


def test_spec027_r20_nested_items_match(contracts_dir: Path) -> None:
    """[SPEC-027 R-20] 목록 항목 모델도 계약 필드·필수와 같다."""
    schema = _schema(contracts_dir)
    module = importlib.import_module("vlm_contracts.aggregates")
    pairs = {
        "mergeGroups": module.MergeGroup,
        "ruleEvents": importlib.import_module("vlm_contracts.events").RuleEvent,
        "snapshots": module.StateSnapshot,
        "risks": module.Risk,
        "timeline": module.TimelineItem,
    }
    for name, model in pairs.items():
        items = _items(schema, name)
        required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}
        assert _fields(model) == set(cast(JsonObject, items["properties"])), name
        assert required == _contract_required(items), name


def test_spec028_r13_source_result_inventory_example_round_trips(contracts_dir: Path) -> None:
    """[SPEC-028 R-13] 재고·변화·재고 이벤트·예측이 든 예시(두 번째)도 같은 JSON으로 돌아온다."""
    example = cast(list[JsonObject], _schema(contracts_dir)["examples"])[1]

    parsed = _model().model_validate(example)

    assert parsed.model_dump(mode="json", by_alias=True) == example


def test_spec028_r13_inventory_items_match(contracts_dir: Path) -> None:
    """[SPEC-028 R-13] 재고·변화·재고 이벤트·예측 항목 모델은 계약 필드·필수와 같다."""
    schema = _schema(contracts_dir)
    module = importlib.import_module("vlm_contracts.aggregates")
    pairs = {
        "inventory": module.InventoryObject,
        "inventoryChanges": module.InventoryChange,
        "inventoryEvents": module.InventoryEvent,
        "forecasts": module.Forecast,
    }
    for name, model in pairs.items():
        items = _items(schema, name)
        required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}
        assert _fields(model) == set(cast(JsonObject, items["properties"])), name
        assert required == _contract_required(items), name


def test_spec028_r1_chunk_inventory_matches_contract(contracts_dir: Path) -> None:
    """[SPEC-028 R-1] 청크 재고 파일 모델은 계약 필드·필수와 같고 예시가 왕복한다."""
    schema: JsonObject = json.loads(
        (contracts_dir / "aggregates/v1/chunk-inventory.schema.json").read_text(encoding="utf-8")
    )
    model = cast(
        type[BaseModel], importlib.import_module("vlm_contracts.aggregates").ChunkInventory
    )
    required = {f.alias or n for n, f in model.model_fields.items() if f.is_required()}

    assert _fields(model) == set(cast(JsonObject, schema["properties"]))
    assert required == _contract_required(schema)
    for example in cast(list[JsonObject], schema["examples"]):
        assert model.model_validate(example).model_dump(mode="json", by_alias=True) == example


def test_spec028_r16_risk_forecast_matches_contract(contracts_dir: Path) -> None:
    """[SPEC-028 R-15·R-16] 예측 출력 모델은 `_risk-forecast` 필드와 같고 빈 horizons를 받는다."""
    schema: JsonObject = json.loads(
        (contracts_dir / "events/_risk-forecast.schema.json").read_text(encoding="utf-8")
    )
    model = cast(type[BaseModel], importlib.import_module("vlm_contracts.aggregates").RiskForecast)

    assert _fields(model) == set(cast(JsonObject, schema["properties"]))
    for example in cast(list[JsonObject], schema["examples"]):
        assert model.model_validate(example).model_dump(mode="json", by_alias=True) == example
    empty = {"method": "rule", "horizons": [], "features": {}}
    assert model.model_validate(empty).model_dump(mode="json", by_alias=True) == empty
