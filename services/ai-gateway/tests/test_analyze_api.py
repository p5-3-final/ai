import json
from pathlib import Path

from ai_gateway.prompts import PromptRegistry
from gateway_support import (
    CALLER,
    IMAGE_KEYS,
    SOURCE,
    FakeAdapter,
    analyze_body,
    gateway,
    output_example,
    post_analyze,
)


async def test_spec009_r1_returns_validated_output(contracts_dir: Path) -> None:
    good = output_example(contracts_dir)
    adapter = FakeAdapter(reply=json.dumps(good, ensure_ascii=False))
    version = PromptRegistry.load(contracts_dir).get("event_judge")
    assert version is not None

    async with gateway(contracts_dir, adapter=adapter) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 200
    body = response.json()
    assert body["taskType"] == "event_judge"
    assert body["modelId"] == "fake-vlm"
    assert body["promptVersion"] == version.version
    assert body["output"] == good
    # 모델에는 프로필의 모델로, 이미지를 요청 순서대로 보낸다.
    [call] = adapter.calls
    assert call.model == "fake-vlm"
    assert [i.data for i in call.images] == [b"jpeg-0", b"jpeg-1"]
    assert call.max_output_tokens == 800


async def test_spec009_r1_records_call(contracts_dir: Path) -> None:
    good = output_example(contracts_dir)
    adapter = FakeAdapter(reply=json.dumps(good))

    async with gateway(contracts_dir, adapter=adapter) as gw:
        response = await post_analyze(gw.client, analyze_body())

    [entry] = gw.call_log.entries
    assert str(entry.call_id) == response.json()["callId"]
    assert (entry.caller, entry.source_id, entry.task_type) == (CALLER, SOURCE, "event_judge")
    assert (entry.adapter, entry.model_id, entry.status) == ("fake", "fake-vlm", "success")
    assert entry.prompt_version == response.json()["promptVersion"]
    assert entry.image_keys == IMAGE_KEYS
    assert entry.output == good
    assert entry.raw_output == json.dumps(good)
    assert (entry.input_tokens, entry.output_tokens) == (100, 20)
    assert entry.latency_ms >= 0
