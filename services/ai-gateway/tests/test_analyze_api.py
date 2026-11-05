import json
import logging
from pathlib import Path
from unittest.mock import ANY

import httpx
import pytest
from ai_gateway.adapters.base import ProviderFailedError, ProviderUnavailableError
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.profile import GatewayProfile, TaskProfile
from ai_gateway.prompts import PromptRegistry
from gateway_support import (
    CALLER,
    CANDIDATE,
    IMAGE_KEYS,
    SOURCE,
    FakeAdapter,
    analyze_body,
    gateway,
    output_example,
    post_analyze,
    profile,
)


async def test_spec009_r1_returns_validated_output(registry_dir: Path) -> None:
    good = output_example(registry_dir)
    adapter = FakeAdapter(reply=json.dumps(good, ensure_ascii=False))
    version = PromptRegistry.load(registry_dir).get("event_judge")
    assert version is not None

    async with gateway(registry_dir, adapter=adapter) as gw:
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


async def test_spec009_r1_records_call(registry_dir: Path) -> None:
    good = output_example(registry_dir)
    adapter = FakeAdapter(reply=json.dumps(good))

    async with gateway(registry_dir, adapter=adapter) as gw:
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


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, object] = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["type"] == "about:blank"
    assert isinstance(body["title"], str)
    return body


@pytest.mark.parametrize(
    "body",
    [
        analyze_body(images=None),
        analyze_body(sourceId="not-a-uuid"),
        analyze_body(images=[{"storageKey": ""}]),
        analyze_body(extra=1),
        {"taskType": "event_judge"},
    ],
)
async def test_spec009_r2_malformed_request_is_rejected(
    registry_dir: Path, body: dict[str, object]
) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_analyze(gw.client, body)

    problem = _problem(response, 400, "AI_INVALID_REQUEST")
    assert problem["errors"]
    assert gw.adapter.calls == []
    assert gw.call_log.entries == []


async def test_spec009_r2_variables_must_match_input_schema(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_analyze(
            gw.client, analyze_body(variables={**CANDIDATE, "eventType": "nope"})
        )

    problem = _problem(response, 400, "AI_INVALID_REQUEST")
    assert problem["errors"] == [
        {"field": "variables/eventType", "message": ANY},
    ]
    assert gw.adapter.calls == []


async def test_spec009_r3_unknown_task_type_is_rejected(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_analyze(gw.client, analyze_body(taskType="track_attribute"))

    _problem(response, 400, "AI_UNKNOWN_TASK_TYPE")
    assert gw.adapter.calls == []


async def test_spec009_r4_too_many_images_is_rejected(registry_dir: Path) -> None:
    async with gateway(registry_dir, profile(max_images=1)) as gw:
        response = await post_analyze(gw.client, analyze_body())

    _problem(response, 400, "AI_TOO_MANY_IMAGES")
    assert gw.adapter.calls == []


async def test_spec009_r11_missing_image_is_rejected(registry_dir: Path) -> None:
    missing = f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/404.jpg"
    async with gateway(registry_dir) as gw:
        response = await post_analyze(
            gw.client, analyze_body(images=[{"storageKey": missing, "ptsSec": None}])
        )

    _problem(response, 400, "AI_IMAGE_NOT_FOUND")
    assert gw.adapter.calls == []


async def test_spec009_r6_invalid_output_returns_unknown_and_keeps_raw(
    registry_dir: Path,
) -> None:
    adapter = FakeAdapter(reply="이미지가 너무 어두워 판단할 수 없습니다.")

    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 200
    output = response.json()["output"]
    assert output["verificationStatus"] == "unknown"
    assert output["eventType"] == "fall"
    [entry] = gw.call_log.entries
    assert entry.status == "invalid_output"
    assert entry.raw_output == "이미지가 너무 어두워 판단할 수 없습니다."
    assert entry.output == output


async def test_spec009_r8_unconfigured_model_returns_unverified_without_call(
    registry_dir: Path,
) -> None:
    async with gateway(registry_dir, profile(adapter=None, model=None)) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 200
    body = response.json()
    assert body["modelId"] is None
    assert body["output"]["verificationStatus"] == "unverified"
    assert body["output"]["eventType"] == "fall"
    assert gw.adapter.calls == []
    [entry] = gw.call_log.entries
    assert (entry.status, entry.adapter, entry.model_id, entry.raw_output) == (
        "unavailable",
        None,
        None,
        None,
    )


async def test_spec009_r9_original_path_is_forbidden(registry_dir: Path) -> None:
    original = f"sources/{SOURCE}/original"

    async with gateway(registry_dir) as gw:
        response = await post_analyze(
            gw.client, analyze_body(images=[{"storageKey": original, "ptsSec": None}])
        )

    _problem(response, 403, "AI_IMAGE_PATH_FORBIDDEN")
    assert gw.adapter.calls == []


async def test_spec009_r10_other_source_path_is_forbidden(registry_dir: Path) -> None:
    other = "sources/0192f0d2-0000-7000-8000-000000000000/chunks/0/blurred/000.jpg"

    async with gateway(registry_dir) as gw:
        response = await post_analyze(
            gw.client, analyze_body(images=[{"storageKey": other, "ptsSec": None}])
        )

    _problem(response, 403, "AI_IMAGE_PATH_FORBIDDEN")


@pytest.mark.parametrize("caller", [None, "search-service", ""])
async def test_spec009_r16_unknown_or_missing_caller_is_forbidden(
    registry_dir: Path, caller: str | None
) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_analyze(gw.client, analyze_body(), caller=caller)

    _problem(response, 403, "AI_CALLER_FORBIDDEN")
    assert gw.adapter.calls == []
    assert gw.call_log.entries == []


@pytest.mark.parametrize(
    ("error", "status", "code", "log_status"),
    [
        (ProviderUnavailableError("timeout"), 503, "AI_PROVIDER_UNAVAILABLE", "unavailable"),
        (ProviderFailedError("401"), 502, "AI_PROVIDER_FAILED", "failed"),
    ],
)
async def test_spec009_r18_r19_provider_errors_are_reported_and_logged(
    registry_dir: Path, error: Exception, status: int, code: str, log_status: str
) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=error)) as gw:
        response = await post_analyze(gw.client, analyze_body())

    _problem(response, status, code)
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == (log_status, code)
    assert (entry.model_id, entry.output, entry.raw_output) == ("fake-vlm", None, None)


async def test_spec009_nfr5_traceparent_reaches_adapter_and_call_log(registry_dir: Path) -> None:
    trace = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    adapter = FakeAdapter(reply=json.dumps(output_example(registry_dir)))

    async with gateway(registry_dir, adapter=adapter) as gw:
        await gw.client.post(
            "/v1/analyze", json=analyze_body(), headers={"X-Caller": CALLER, "traceparent": trace}
        )

    assert adapter.calls[0].traceparent == trace
    assert gw.call_log.entries[0].trace_id == "0123456789abcdef0123456789abcdef"


async def test_spec009_nfr5_malformed_traceparent_is_ignored(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=json.dumps(output_example(registry_dir)))

    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await gw.client.post(
            "/v1/analyze", json=analyze_body(), headers={"X-Caller": CALLER, "traceparent": "x"}
        )

    assert response.status_code == 200
    assert adapter.calls[0].traceparent is None
    assert gw.call_log.entries[0].trace_id is None


async def test_spec009_nfr3_logs_have_no_images_or_raw_output(
    registry_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raw = "비밀스러운 모델 원문 응답"
    adapter = FakeAdapter(reply=raw)

    with caplog.at_level(logging.DEBUG):
        async with gateway(registry_dir, adapter=adapter) as gw:
            await post_analyze(gw.client, analyze_body())

    completed = [r for r in caplog.records if r.getMessage() == "analyze completed"]
    assert len(completed) == 1
    assert vars(completed[0])["status"] == "invalid_output"
    assert vars(completed[0])["call_id"] == str(gw.call_log.entries[0].call_id)
    for record in caplog.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        assert raw not in rendered
        assert "jpeg-0" not in rendered
        assert "A동 적재 구역" not in rendered


def _sample_check_profile(**task: object) -> GatewayProfile:
    settings: dict[str, object] = {
        "adapter": "fake",
        "model": "fake-vlm",
        "max_output_tokens": 200,
        "max_images": 4,
        "timeout_s": 5,
        "max_retries": 0,
    } | task
    base = profile()
    return base.model_copy(
        update={"tasks": base.tasks | {"sample_check": TaskProfile.model_validate(settings)}}
    )


async def test_spec009_r24_task_added_only_to_registry_is_analyzed(
    extended_registry_dir: Path,
) -> None:
    """[SPEC-009 R-24] Registry 폴더와 프로필 설정만 더한 taskType을 코드 변경 없이 분석한다."""
    prompts = PromptRegistry.load(extended_registry_dir)
    body = analyze_body(taskType="sample_check", variables={"zoneName": "C"})

    async with gateway(
        extended_registry_dir, _sample_check_profile(), adapters={"fake": MockAdapter(prompts)}
    ) as gw:
        response = await post_analyze(gw.client, body)

    assert response.status_code == 200, response.text
    assert response.json()["output"] == {
        "zoneName": "C",
        "present": True,
        "verificationStatus": "confirmed",
    }
    assert response.json()["promptVersion"].startswith("sample_check@")


async def test_spec009_r25_task_added_only_to_registry_gets_its_fallback(
    extended_registry_dir: Path,
) -> None:
    """[SPEC-009 R-25] 모델이 없으면 그 taskType의 Registry 대체 결과를 unverified로 돌려준다."""
    body = analyze_body(taskType="sample_check", variables={"zoneName": "C"})

    async with gateway(
        extended_registry_dir, _sample_check_profile(adapter=None, model=None)
    ) as gw:
        response = await post_analyze(gw.client, body)

    assert response.status_code == 200, response.text
    assert response.json()["output"] == {
        "zoneName": "C",
        "present": None,
        "verificationStatus": "unverified",
    }


async def test_spec009_r29_result_and_call_log_carry_taxonomy_version(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None
    adapter = FakeAdapter(reply=json.dumps(output_example(registry_dir)))

    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.json()["taxonomyVersion"] == task.taxonomy_version
    [entry] = gw.call_log.entries
    assert entry.taxonomy_version == task.taxonomy_version


async def test_spec009_r29_fallback_carries_taxonomy_version(registry_dir: Path) -> None:
    task = PromptRegistry.load(registry_dir).get("event_judge")
    assert task is not None

    async with gateway(registry_dir, profile(adapter=None, model=None)) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.json()["taxonomyVersion"] == task.taxonomy_version
    [entry] = gw.call_log.entries
    assert entry.taxonomy_version == task.taxonomy_version


async def test_spec009_r29_task_without_taxonomy_returns_null_version(
    extended_registry_dir: Path,
) -> None:
    body = analyze_body(taskType="sample_check", variables={"zoneName": "C"})

    async with gateway(
        extended_registry_dir, _sample_check_profile(adapter=None, model=None)
    ) as gw:
        response = await post_analyze(gw.client, body)

    assert response.status_code == 200, response.text
    assert response.json()["taxonomyVersion"] is None
    [entry] = gw.call_log.entries
    assert entry.taxonomy_version is None


async def test_spec012_r10_query_parse_is_not_an_analyze_task(registry_dir: Path) -> None:
    """[SPEC-012 R-10] query_parse는 /v1/parse 전용이라 /v1/analyze로 부를 수 없다."""
    async with gateway(registry_dir) as gw:
        response = await post_analyze(
            gw.client, analyze_body(taskType="query_parse", variables={"query": "x"})
        )

    _problem(response, 400, "AI_UNKNOWN_TASK_TYPE")
    assert gw.adapter.calls == []
