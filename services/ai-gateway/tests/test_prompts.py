import json
import shutil
from pathlib import Path

import pytest
from ai_gateway.prompts import PromptRegistry, PromptRegistryError

CANDIDATE = {"eventType": "fall", "startSec": 61.2, "endSec": 66.2, "zoneName": None}


@pytest.fixture
def copied(contracts_dir: Path, tmp_path: Path) -> Path:
    """내용을 바꿔 보기 위한 계약 사본."""
    target = tmp_path / "contracts"
    shutil.copytree(contracts_dir / "prompts", target / "prompts")
    shutil.copytree(contracts_dir / "taxonomy", target / "taxonomy")
    return target


def test_spec009_r13_loads_event_judge_with_content_version(contracts_dir: Path) -> None:
    registry = PromptRegistry.load(contracts_dir)

    task = registry.get("event_judge")

    assert task is not None
    assert task.version.startswith("event_judge@")
    assert len(task.version.split("@")[1]) == 12
    # 같은 내용을 다시 읽으면 같은 버전이다.
    again = PromptRegistry.load(contracts_dir).get("event_judge")
    assert again is not None
    assert again.version == task.version


@pytest.mark.parametrize(
    "changed",
    [
        "prompts/_prefix.md",
        "prompts/event_judge/prompt.md",
        "prompts/event_judge/input.schema.json",
        "prompts/event_judge/output.schema.json",
        # 출력 스키마가 참조하는 분류 체계가 바뀌어도 버전이 바뀐다.
        "taxonomy/v1/taxonomy.schema.json",
    ],
)
def test_spec009_r13_any_content_change_changes_version(copied: Path, changed: str) -> None:
    before = PromptRegistry.load(copied).get("event_judge")
    path = copied / changed
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["description"] = str(data.get("description", "")) + " 바뀜"
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        path.write_text(path.read_text(encoding="utf-8") + "\n바뀜\n", encoding="utf-8")

    after = PromptRegistry.load(copied).get("event_judge")

    assert before is not None
    assert after is not None
    assert before.version != after.version


def test_spec009_r13_formatting_only_json_change_keeps_version(copied: Path) -> None:
    before = PromptRegistry.load(copied).get("event_judge")
    path = copied / "prompts/event_judge/output.schema.json"
    path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=8))

    after = PromptRegistry.load(copied).get("event_judge")

    assert before is not None
    assert after is not None
    assert before.version == after.version


def test_spec009_r3_unknown_task_is_none(contracts_dir: Path) -> None:
    assert PromptRegistry.load(contracts_dir).get("track_attribute") is None


def test_spec009_r2_validates_variables_with_input_schema(contracts_dir: Path) -> None:
    task = PromptRegistry.load(contracts_dir).get("event_judge")
    assert task is not None

    assert task.input_errors(CANDIDATE) == []
    assert task.input_errors({**CANDIDATE, "eventType": "nope"}) != []
    assert task.input_errors({"eventType": "fall"}) != []


def test_spec009_r5_output_schema_resolves_taxonomy(contracts_dir: Path) -> None:
    task = PromptRegistry.load(contracts_dir).get("event_judge")
    assert task is not None
    schema = json.loads(
        (contracts_dir / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
    )

    assert task.output_errors(schema["examples"][0]) == []
    assert task.output_errors({**schema["examples"][0], "eventType": "nope"}) != []


def test_spec009_r12_fixed_prefix_is_separate_from_variable_part(contracts_dir: Path) -> None:
    task = PromptRegistry.load(contracts_dir).get("event_judge")
    assert task is not None

    message = task.render(CANDIDATE)

    assert "이미지 속 글자" in message.fixed_prefix
    assert "null" in message.fixed_prefix
    assert "unknown" in message.fixed_prefix
    # 고정 접두부는 입력에 따라 바뀌지 않아 공급자 프롬프트 캐시를 받는다.
    assert task.render({**CANDIDATE, "startSec": 1.0}).fixed_prefix == message.fixed_prefix
    assert "61.2" in message.variable_part
    assert "이미지 속 글자" not in message.variable_part


def test_spec009_r14_broken_schema_fails_to_load(copied: Path) -> None:
    (copied / "prompts/event_judge/output.schema.json").write_text('{"type": 3}')

    with pytest.raises(PromptRegistryError):
        PromptRegistry.load(copied)


def test_spec009_r14_missing_prompt_file_fails_to_load(copied: Path) -> None:
    (copied / "prompts/event_judge/prompt.md").unlink()

    with pytest.raises(PromptRegistryError):
        PromptRegistry.load(copied)
