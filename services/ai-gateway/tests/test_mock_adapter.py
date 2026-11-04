from pathlib import Path

from ai_gateway.adapters.base import ImageInput
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.output import parse_output
from ai_gateway.prompts import PromptRegistry


async def _complete(registry: PromptRegistry, task_type: str, variables: dict[str, object]) -> str:
    task = registry.get(task_type)
    assert task is not None
    completion = await MockAdapter(registry).complete(
        model="mock",
        message=task.render(variables),
        images=[ImageInput(b"x", "image/jpeg")],
        max_output_tokens=10,
        timeout_s=1,
        max_retries=0,
        traceparent=None,
    )

    return completion.text


async def test_spec009_r24_mock_returns_output_example_with_input_fields(
    registry_dir: Path,
) -> None:
    """[SPEC-009 R-24] mock은 출력 스키마 예시를 쓰고, 입력과 이름이 같은 필드는 입력값으로 둔다."""
    registry = PromptRegistry.load(registry_dir)
    task = registry.get("event_judge")
    assert task is not None
    variables: dict[str, object] = {"eventType": "ppe_missing", "startSec": 3.0, "endSec": 8.0}

    parsed = parse_output(task, await _complete(registry, "event_judge", variables), variables)

    assert parsed.valid, parsed.errors
    assert parsed.output["eventType"] == "ppe_missing"
    assert parsed.output == task.output_example | {"eventType": "ppe_missing"}


async def test_spec009_r24_mock_answers_task_added_only_to_registry(
    extended_registry_dir: Path,
) -> None:
    """[SPEC-009 R-24] Registry에만 더한 taskType도 mock이 스키마에 맞는 출력을 낸다."""
    registry = PromptRegistry.load(extended_registry_dir)
    task = registry.get("sample_check")
    assert task is not None

    text = await _complete(registry, "sample_check", {"zoneName": "C"})

    parsed = parse_output(task, text, {"zoneName": "C"})
    assert parsed.valid, parsed.errors
    assert parsed.output == {"zoneName": "C", "present": True, "verificationStatus": "confirmed"}
