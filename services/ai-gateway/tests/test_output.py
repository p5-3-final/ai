import json
from pathlib import Path

import pytest
from ai_gateway.output import FallbackStatus, fallback_output, parse_output
from ai_gateway.prompts import PromptRegistry, PromptTask

CANDIDATE: dict[str, object] = {"eventType": "fall", "startSec": 61.2, "endSec": 66.2}


@pytest.fixture(scope="module")
def task(contracts_dir: Path) -> PromptTask:
    loaded = PromptRegistry.load(contracts_dir).get("event_judge")
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def good(contracts_dir: Path) -> dict[str, object]:
    schema = json.loads(
        (contracts_dir / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
    )
    example: dict[str, object] = schema["examples"][0]
    return example


@pytest.mark.parametrize(
    "wrap",
    [
        "{}",
        "```json\n{}\n```",
        "판정 결과입니다.\n```\n{}\n```\n이상입니다.",
        "결과: {} 끝",
    ],
)
def test_spec009_r5_extracts_json_object(
    task: PromptTask, good: dict[str, object], wrap: str
) -> None:
    raw = wrap.replace("{}", json.dumps(good, ensure_ascii=False))

    result = parse_output(task, raw, CANDIDATE)

    assert result.valid
    assert result.output == good


def test_spec009_r5_reports_schema_errors(task: PromptTask, good: dict[str, object]) -> None:
    result = parse_output(task, json.dumps({**good, "severity": "urgent"}), CANDIDATE)

    assert not result.valid
    assert any("severity" in e for e in result.errors)


@pytest.mark.parametrize(
    "raw",
    ["", "판단할 수 없습니다.", "[1, 2]", '{"eventType": "fall"', "```json\nnot json\n```"],
)
def test_spec009_r6_no_json_object_gives_unknown(task: PromptTask, raw: str) -> None:
    result = parse_output(task, raw, CANDIDATE)

    assert not result.valid
    assert result.output["verificationStatus"] == "unknown"
    # 판정 결과를 지어내지 않고 후보의 유형만 이어 받는다.
    assert result.output["eventType"] == "fall"
    assert result.output["eventDetected"] is None
    assert task.output_errors(result.output) == []


def test_spec009_r6_schema_mismatch_gives_unknown(
    task: PromptTask, good: dict[str, object]
) -> None:
    result = parse_output(task, json.dumps({**good, "extra": 1}), CANDIDATE)

    assert not result.valid
    assert result.output["verificationStatus"] == "unknown"


def test_spec009_r7_risk_without_type_or_reason_is_invalid(
    task: PromptTask, good: dict[str, object]
) -> None:
    for missing in ("riskType", "riskReason"):
        result = parse_output(task, json.dumps({**good, missing: None}), CANDIDATE)

        assert not result.valid, missing
        assert result.output["verificationStatus"] == "unknown"


@pytest.mark.parametrize("status", ["unknown", "unverified"])
def test_spec009_r6_r8_fallback_matches_output_schema(
    task: PromptTask, status: FallbackStatus
) -> None:
    output = fallback_output("event_judge", CANDIDATE, status)

    assert task.output_errors(output) == []
    assert output["verificationStatus"] == status
