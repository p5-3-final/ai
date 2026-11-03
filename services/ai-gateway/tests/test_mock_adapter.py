from pathlib import Path

from ai_gateway.adapters.base import ImageInput
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.output import parse_output
from ai_gateway.prompts import PromptRegistry


async def test_mock_returns_schema_valid_event_judge_output(contracts_dir: Path) -> None:
    task = PromptRegistry.load(contracts_dir).get("event_judge")
    assert task is not None
    variables: dict[str, object] = {"eventType": "ppe_missing", "startSec": 3.0, "endSec": 8.0}

    completion = await MockAdapter().complete(
        model="mock",
        message=task.render(variables),
        images=[ImageInput(b"x", "image/jpeg")],
        max_output_tokens=10,
        timeout_s=1,
        max_retries=0,
        traceparent=None,
    )

    parsed = parse_output(task, completion.text, variables)
    assert parsed.valid, parsed.errors
    assert parsed.output["eventType"] == "ppe_missing"
    assert parsed.output["evidenceStart"] == 3.0
