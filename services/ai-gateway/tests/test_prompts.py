import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from ai_gateway.prompts import FallbackStatus, PromptRegistry, PromptRegistryError

CANDIDATE = {"eventType": "fall", "startSec": 61.2, "endSec": 66.2, "zoneName": None}


@pytest.fixture
def copied(registry_dir: Path, tmp_path: Path) -> Path:
    """내용을 바꿔 보기 위한 계약 사본."""
    target = tmp_path / "contracts"
    shutil.copytree(registry_dir / "prompts", target / "prompts")
    shutil.copytree(registry_dir / "taxonomy", target / "taxonomy")
    return target


def test_spec009_r13_loads_event_judge_with_content_version(registry_dir: Path) -> None:
    registry = PromptRegistry.load(registry_dir)

    task = registry.get("event_judge")

    assert task is not None
    assert task.version.startswith("event_judge@")
    assert len(task.version.split("@")[1]) == 12
    # 같은 내용을 다시 읽으면 같은 버전이다.
    again = PromptRegistry.load(registry_dir).get("event_judge")
    assert again is not None
    assert again.version == task.version


@pytest.mark.parametrize(
    "changed",
    [
        "prompts/_prefix.md",
        "prompts/event_judge/prompt.md",
        "prompts/event_judge/input.schema.json",
        "prompts/event_judge/fallback.json",
        "prompts/event_judge/output.schema.json",
        # 출력 스키마가 참조하는 분류 체계가 바뀌어도 버전이 바뀐다.
        "taxonomy",
    ],
)
def test_spec009_r13_any_content_change_changes_version(copied: Path, changed: str) -> None:
    before = PromptRegistry.load(copied).get("event_judge")
    path = copied / (_referenced_taxonomy(copied) if changed == "taxonomy" else changed)
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
    data = json.loads(path.read_text(encoding="utf-8"))
    # 들여쓰기와 키 순서만 바꾼다.
    path.write_text(json.dumps(dict(reversed(list(data.items()))), indent=8))

    after = PromptRegistry.load(copied).get("event_judge")

    assert before is not None
    assert after is not None
    assert before.version == after.version


def test_spec009_r3_unknown_task_is_none(registry_dir: Path) -> None:
    assert PromptRegistry.load(registry_dir).get("track_attribute") is None


def test_spec009_r2_validates_variables_with_input_schema(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None

    assert task.input_errors(CANDIDATE) == []
    assert task.input_errors({**CANDIDATE, "eventType": "nope"}) != []
    assert task.input_errors({"eventType": "fall"}) != []


def test_spec009_r5_output_schema_resolves_taxonomy(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None
    schema = json.loads(
        (registry_dir / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
    )

    assert task.output_errors(schema["examples"][0]) == []
    assert task.output_errors({**schema["examples"][0], "eventType": "nope"}) != []


def test_spec009_r12_fixed_prefix_is_separate_from_variable_part(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")
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


def test_issue17_prompt_includes_output_schema_with_taxonomy(registry_dir: Path) -> None:
    """[#17] 모델이 필수 필드와 허용 값을 알 수 있게 출력 스키마를 함께 보낸다."""
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None

    part = task.render(CANDIDATE).variable_part

    for field in ("eventType", "verificationStatus", "riskType", "riskReason", "evidenceEnd"):
        assert f'"{field}"' in part
    # 분류 체계 $ref를 풀어 이벤트 유형 값이 보인다.
    assert "barrier_not_restored" in part
    assert "$ref" not in part
    # 호출마다 같은 지시·스키마가 입력보다 앞에 와야 공급자 프롬프트 캐시를 받는다.
    assert part.index("riskReason") < part.index("61.2")


def _referenced_taxonomy(contracts: Path) -> str:
    # 분류 체계 버전 폴더는 계약과 함께 바뀌므로 경로를 고정하지 않고 출력 스키마에서 찾는다.
    schema = (contracts / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
    found = re.search(r'"\.\./\.\./(taxonomy/[^"#]+)#', schema)
    assert found is not None, "출력 스키마가 분류 체계를 참조하지 않는다"
    return found.group(1)


def _edit_json(path: Path, edit: Callable[[dict[str, object]], object]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("status", ["unknown", "unverified"])
def test_spec009_r25_fallback_fills_input_variables_and_status(
    registry_dir: Path, status: FallbackStatus
) -> None:
    """[SPEC-009 R-25] 대체 결과는 Registry 정의에 입력 변수와 상태만 채운다."""
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None

    output = task.fallback(CANDIDATE, status)

    assert output["eventType"] == "fall"
    assert output["verificationStatus"] == status
    assert output["eventDetected"] is None
    assert task.output_errors(output) == []


def test_spec009_r14_missing_fallback_fails_to_load(copied: Path) -> None:
    """[SPEC-009 R-14] 대체 결과 정의가 없는 taskType은 기동에 실패한다."""
    (copied / "prompts/event_judge/fallback.json").unlink()

    with pytest.raises(PromptRegistryError, match="fallback"):
        PromptRegistry.load(copied)


def test_spec009_r14_fallback_with_unknown_input_fails_to_load(copied: Path) -> None:
    """[SPEC-009 R-14] 입력 스키마에 없는 변수를 넣는 대체 결과는 기동에 실패한다."""
    _edit_json(
        copied / "prompts/event_judge/fallback.json",
        lambda d: d.update({"description": "$input.nope"}),
    )

    with pytest.raises(PromptRegistryError, match="nope"):
        PromptRegistry.load(copied)


def test_spec009_r14_fallback_not_matching_output_schema_fails_to_load(copied: Path) -> None:
    """[SPEC-009 R-14] 입력 예시로 만든 대체 결과가 출력 스키마와 맞지 않으면 기동에 실패한다."""
    _edit_json(
        copied / "prompts/event_judge/fallback.json",
        lambda d: d.update({"eventDetected": "yes"}),
    )

    with pytest.raises(PromptRegistryError, match="fallback"):
        PromptRegistry.load(copied)


@pytest.mark.parametrize("schema", ["input", "output"])
def test_spec009_r14_example_not_matching_schema_fails_to_load(copied: Path, schema: str) -> None:
    """[SPEC-009 R-14] 입력·출력 예시는 자기 스키마와 맞아야 한다."""
    _edit_json(
        copied / f"prompts/event_judge/{schema}.schema.json",
        lambda d: d.update({"examples": [{"eventType": "not-a-type"}]}),
    )

    with pytest.raises(PromptRegistryError, match="example"):
        PromptRegistry.load(copied)


@pytest.mark.parametrize("schema", ["input", "output"])
def test_spec009_r14_missing_examples_fails_to_load(copied: Path, schema: str) -> None:
    """[SPEC-009 R-14] 대체 결과 검증과 mock 출력에 쓰는 예시가 없으면 기동에 실패한다."""
    _edit_json(copied / f"prompts/event_judge/{schema}.schema.json", lambda d: d.pop("examples"))

    with pytest.raises(PromptRegistryError, match="example"):
        PromptRegistry.load(copied)


def test_spec009_r29_task_records_referenced_taxonomy_version(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")

    assert task is not None
    # 경로 `taxonomy/<버전>/…`의 버전 폴더 이름이다.
    assert task.taxonomy_version == _referenced_taxonomy(registry_dir).split("/")[1]


def test_spec009_r29_task_without_taxonomy_has_no_version(extended_registry_dir: Path) -> None:
    task = PromptRegistry.load(extended_registry_dir).get("sample_check")

    assert task is not None
    assert task.taxonomy_version is None


def test_spec009_r30_mixed_taxonomy_versions_fail_to_load(copied: Path) -> None:
    current = _referenced_taxonomy(copied)
    version = current.split("/")[1]
    shutil.copytree(copied / "taxonomy" / version, copied / "taxonomy" / "v99")
    # 같은 `$id`가 두 번 등록되지 않도록 사본의 `$id`도 바꾼다.
    for path in (copied / "taxonomy" / "v99").glob("*.schema.json"):
        _edit_json(path, lambda d: d.update({"$id": str(d["$id"]).replace(version, "v99")}))
    input_path = copied / "prompts/event_judge/input.schema.json"
    input_path.write_text(
        input_path.read_text(encoding="utf-8").replace(f"taxonomy/{version}/", "taxonomy/v99/"),
        encoding="utf-8",
    )

    with pytest.raises(PromptRegistryError, match="taxonomy versions"):
        PromptRegistry.load(copied)
