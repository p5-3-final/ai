"""처리 단계 모델이 docs 레포의 계약 원본과 맞는지 검증한다(SPEC-013)."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel
from vlm_contracts.stages import (
    STAGE_IDS,
    StageSubject,
    StageTiming,
    StageTimingBatch,
    StageVersions,
)

type JsonObject = dict[str, object]


def _stages(contracts_dir: Path, name: str) -> JsonObject:
    loaded: JsonObject = json.loads(
        (contracts_dir / "stages" / "v1" / f"{name}.schema.json").read_text(encoding="utf-8")
    )
    return loaded


def test_spec013_r10_stage_ids_match_contract_list(contracts_dir: Path) -> None:
    stage_id = cast(dict[str, JsonObject], _stages(contracts_dir, "stages")["$defs"])["stageId"]
    listed = {option["const"] for option in cast(list[JsonObject], stage_id["oneOf"])}

    assert set(STAGE_IDS) == listed


def _event_schema(contracts_dir: Path) -> JsonObject:
    loaded: JsonObject = json.loads(
        (contracts_dir / "events" / "stage.timing.schema.json").read_text(encoding="utf-8")
    )
    return loaded


def _fields(model: type[BaseModel]) -> set[str]:
    return {field.alias or name for name, field in model.model_fields.items()}


def _required_fields(model: type[BaseModel]) -> set[str]:
    return {
        field.alias or name for name, field in model.model_fields.items() if field.is_required()
    }


@pytest.mark.parametrize(
    ("model", "schema"),
    [
        (StageTiming, "timing"),
        (StageTimingBatch, "batch"),
        (StageSubject, "subject"),
        (StageVersions, "versions"),
    ],
)
def test_spec013_nfr1_models_have_exactly_contract_fields(
    contracts_dir: Path, model: type[BaseModel], schema: str
) -> None:
    timing = _stages(contracts_dir, "stage-timing")
    props = cast(dict[str, JsonObject], timing["properties"])
    contract = {
        "timing": timing,
        "batch": _event_schema(contracts_dir),
        "subject": props["subject"],
        "versions": props["versions"],
    }[schema]

    assert _fields(model) == set(cast(dict[str, object], contract["properties"]))
    assert _required_fields(model) == set(cast(list[str], contract.get("required", [])))


def test_spec013_batch_example_round_trips(contracts_dir: Path) -> None:
    for example in cast(list[JsonObject], _event_schema(contracts_dir)["examples"]):
        parsed = StageTimingBatch.model_validate(example)
        dumped = json.loads(parsed.model_dump_json(by_alias=True))
        # 시각의 소수 자릿수·정수 표기는 달라질 수 있어 값으로 비교한다.
        assert StageTimingBatch.model_validate(dumped) == parsed
        assert dumped.keys() == example.keys()
        for got, want in zip(
            cast(list[JsonObject], dumped["timings"]),
            cast(list[JsonObject], example["timings"]),
            strict=True,
        ):
            assert got.keys() == want.keys()
