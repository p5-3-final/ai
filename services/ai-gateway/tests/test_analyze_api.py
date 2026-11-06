import json
import logging
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY

import httpx
import pytest
from ai_gateway.adapters.base import ProviderFailedError, ProviderUnavailableError
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.profile import GatewayProfile, TaskProfile
from ai_gateway.prompts import PromptRegistry
from ai_gateway.rate_limit import RateLimitedError
from gateway_support import (
    CALLER,
    CANDIDATE,
    IMAGE_KEYS,
    SOURCE,
    FakeAdapter,
    MemoryBudget,
    MemoryCache,
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


PRICES = {"fake": {"fake-vlm": {"input_per_million_usd": 2, "output_per_million_usd": 8}}}


class BusyLimiter:
    """언제나 차례를 주지 않는 분당 한도."""

    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None:
        raise RateLimitedError("busy")


def _valid_reply(registry_dir: Path) -> str:
    return json.dumps(output_example(registry_dir), ensure_ascii=False)


async def test_spec016_r2_same_input_returns_cached_result(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    async with gateway(registry_dir, adapter=adapter) as gw:
        first = (await post_analyze(gw.client, analyze_body())).json()
        second = (await post_analyze(gw.client, analyze_body())).json()

    assert len(adapter.calls) == 1
    assert first["callId"] != second["callId"]
    assert {k: v for k, v in first.items() if k != "callId"} == {
        k: v for k, v in second.items() if k != "callId"
    }


async def test_spec016_r19_cache_hit_is_logged_without_raw_output(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    async with gateway(registry_dir, adapter=adapter) as gw:
        await post_analyze(gw.client, analyze_body())
        await post_analyze(gw.client, analyze_body())

    called, cached = gw.call_log.entries
    assert (called.cache_hit, called.status) == (False, "success")
    assert (cached.cache_hit, cached.status) == (True, "success")
    assert cached.raw_output is None
    assert cached.output == called.output
    assert (cached.reserved_cost_usd, cached.settled_cost_usd) == (Decimal(0), Decimal(0))


async def test_spec016_r4_same_path_with_new_image_content_calls_model(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    async with gateway(registry_dir, adapter=adapter) as gw:
        await post_analyze(gw.client, analyze_body())
        gw.storage.put_bytes(IMAGE_KEYS[0], b"jpeg-new", "image/jpeg")
        await post_analyze(gw.client, analyze_body())

    assert len(adapter.calls) == 2


async def test_spec016_r2_other_variables_call_model(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    async with gateway(registry_dir, adapter=adapter) as gw:
        await post_analyze(gw.client, analyze_body())
        await post_analyze(gw.client, analyze_body(variables={**CANDIDATE, "zoneName": "B동"}))

    assert len(adapter.calls) == 2


async def test_spec016_r2_model_change_calls_model(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    shared = MemoryCache()
    async with gateway(registry_dir, adapter=adapter, cache=shared) as gw:
        await post_analyze(gw.client, analyze_body())
    async with gateway(
        registry_dir, profile(model="other-vlm"), adapter=adapter, cache=shared
    ) as gw:
        await post_analyze(gw.client, analyze_body())

    assert len(adapter.calls) == 2


async def test_spec016_r3_invalid_output_is_not_cached(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply="판단할 수 없음")
    async with gateway(registry_dir, adapter=adapter) as gw:
        await post_analyze(gw.client, analyze_body())
        await post_analyze(gw.client, analyze_body())

    assert len(adapter.calls) == 2


async def test_spec016_r11_budget_exceeded_returns_unverified(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    exhausted = MemoryBudget(limits={"*": Decimal(0)})
    priced = profile(extra={"prices": PRICES})
    async with gateway(registry_dir, priced, adapter=adapter, budget=exhausted) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 200
    body = response.json()
    assert body["output"]["verificationStatus"] == "unverified"
    assert body["modelId"] is None
    assert adapter.calls == []
    [entry] = gw.call_log.entries
    assert entry.status == "budget_exceeded"


async def test_spec016_r19_called_result_logs_costs(registry_dir: Path) -> None:
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    priced = profile(extra={"prices": PRICES}, estimated_input_tokens=1000)
    async with gateway(registry_dir, priced, adapter=adapter) as gw:
        await post_analyze(gw.client, analyze_body())

    [entry] = gw.call_log.entries
    # 예상: (1000*2 + 800*8)/1e6, 실제(FakeAdapter 100·20토큰): (100*2 + 20*8)/1e6
    assert entry.reserved_cost_usd == Decimal("0.0084")
    assert entry.settled_cost_usd == Decimal("0.00036")
    assert gw.budget.spent["event-analyzer"] == Decimal("0.00036")


async def test_spec016_r16_rate_limited_is_429(registry_dir: Path) -> None:
    async with gateway(registry_dir, limiter=BusyLimiter()) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 429
    assert response.json()["code"] == "AI_RATE_LIMITED"
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == ("unavailable", "AI_RATE_LIMITED")
    assert gw.adapter.calls == []


async def test_spec020_r20_escalate_uses_its_profile_model_and_prompt(registry_dir: Path) -> None:
    good = output_example(registry_dir)
    adapter = FakeAdapter(reply=json.dumps(good, ensure_ascii=False))
    task = PromptRegistry.load(registry_dir).get("event_judge_escalate")
    assert task is not None
    base = profile()
    strong = base.tasks["event_judge"].model_copy(update={"model": "fake-strong-vlm"})
    escalating = base.model_copy(update={"tasks": {**base.tasks, "event_judge_escalate": strong}})

    async with gateway(registry_dir, escalating, adapter=adapter) as gw:
        response = await post_analyze(gw.client, analyze_body(taskType="event_judge_escalate"))

    assert response.status_code == 200
    body = response.json()
    assert (body["taskType"], body["modelId"]) == ("event_judge_escalate", "fake-strong-vlm")
    assert body["promptVersion"] == task.version
    assert body["output"] == good
    [call] = adapter.calls
    assert call.model == "fake-strong-vlm"
    assert call.message.task_type == "event_judge_escalate"


async def test_spec016_r23_unconfigured_model_reports_model_unavailable(
    registry_dir: Path,
) -> None:
    """[SPEC-016 R-23] 모델 미설정으로 unverified면 사유가 model_unavailable이다."""
    async with gateway(registry_dir, profile(adapter=None, model=None)) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.json()["unverifiedReason"] == "model_unavailable"


async def test_spec016_r23_budget_exceeded_reports_budget_exceeded(registry_dir: Path) -> None:
    """[SPEC-016 R-23] 예산 초과로 unverified면 사유가 budget_exceeded이다."""
    exhausted = MemoryBudget(limits={"*": Decimal(0)})
    priced = profile(extra={"prices": PRICES})
    async with gateway(registry_dir, priced, budget=exhausted) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.json()["unverifiedReason"] == "budget_exceeded"


@pytest.mark.parametrize("reply", ["valid", "판단할 수 없음"])
async def test_spec016_r23_other_results_have_no_reason(registry_dir: Path, reply: str) -> None:
    """[SPEC-016 R-23] unverified가 아닌 결과(확정·판단 불가·캐시 적중)에는 사유를 싣지 않는다."""
    text = _valid_reply(registry_dir) if reply == "valid" else reply
    async with gateway(registry_dir, adapter=FakeAdapter(reply=text)) as gw:
        first = (await post_analyze(gw.client, analyze_body())).json()
        second = (await post_analyze(gw.client, analyze_body())).json()

    assert "unverifiedReason" not in first
    assert "unverifiedReason" not in second
