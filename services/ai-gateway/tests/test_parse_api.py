import json
import logging
import re
import shutil
import statistics
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from ai_gateway.adapters.base import ProviderFailedError, ProviderUnavailableError
from ai_gateway.profile import GatewayProfile
from ai_gateway.rate_limit import RateLimitedError
from gateway_support import (
    CALLER,
    FakeAdapter,
    Gateway,
    MemoryBudget,
    gateway,
    parse_settings,
    post_parse,
    profile,
)

RULE_QUERY = "안전모 안 쓴 작업자 찾아줘"
# 사전에 맞는 표현이 없는 한국어 문장
OUT_OF_DICTIONARY = "사다리 위에 올라간"
LLM_REPLY = '{"targets": ["ladder", "person"], "eventTypes": []}'
TRANSIENT = ProviderUnavailableError("timeout")


def _ok(response: httpx.Response) -> dict[str, object]:
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code


def _without_call_id(body: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in body.items() if k != "callId"}


def _parse_profile(**changes: object) -> GatewayProfile:
    return profile(extra={"parse": parse_settings(**changes)})


async def _parse(registry_dir: Path, query: str, reply: str | Exception = "") -> dict[str, object]:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=reply)) as gw:
        return _ok(await post_parse(gw.client, query))


async def test_spec012_r1_rule_query_returns_canonical_query(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        body = _ok(await post_parse(gw.client, RULE_QUERY))

    UUID(str(body["callId"]))
    assert body["route"] == "video_retrieval"
    assert body["targets"] == ["person without a hard hat", "person"]
    assert body["eventTypes"] == ["ppe_missing"]
    assert body["parseMethod"] == "industrial-rule"
    assert re.fullmatch(r"v1@[0-9a-f]{12}", str(body["dictionaryVersion"]))
    assert body["taxonomyVersion"] == "v2"
    assert isinstance(body["canonicalQuery"], str)
    assert body["canonicalQuery"]


async def test_spec012_r16_visual_query_is_made_from_targets(registry_dir: Path) -> None:
    body = await _parse(registry_dir, RULE_QUERY)

    assert body["visualQuery"] == "a photo of person without a hard hat and person"


async def test_spec012_r17_rationale_names_method_and_phrases(registry_dir: Path) -> None:
    body = await _parse(registry_dir, RULE_QUERY)

    rationale = str(body["rationale"])
    assert "industrial-rule" in rationale
    assert "안전모 안" in rationale
    assert "작업자" in rationale


async def test_spec012_r19_prompt_version_is_null_without_llm(registry_dir: Path) -> None:
    body = await _parse(registry_dir, RULE_QUERY)

    assert body["promptVersion"] is None


async def test_spec012_r1_same_query_gives_same_result(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        first = _ok(await post_parse(gw.client, RULE_QUERY))
        second = _ok(await post_parse(gw.client, RULE_QUERY))

    assert first["callId"] != second["callId"]
    assert _without_call_id(first) == _without_call_id(second)


async def test_spec012_r18_same_parse_gives_same_canonical_query(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        first = _ok(await post_parse(gw.client, "지게차 찾아줘"))
        second = _ok(await post_parse(gw.client, "지게차 장면"))
        other = _ok(await post_parse(gw.client, "트럭 장면"))

    assert first["canonicalQuery"] == second["canonicalQuery"]
    assert first["canonicalQuery"] != other["canonicalQuery"]


async def test_spec012_r18_route_changes_canonical_query(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        scene = _ok(await post_parse(gw.client, "지게차"))
        temporal = _ok(await post_parse(gw.client, "지게차 이후"))

    assert scene["targets"] == temporal["targets"]
    assert scene["canonicalQuery"] != temporal["canonicalQuery"]


async def test_spec012_r18_dictionary_change_changes_canonical_query(
    registry_dir: Path, tmp_path: Path
) -> None:
    changed = tmp_path / "registry"
    shutil.copytree(registry_dir, changed)
    path = changed / "query" / "dictionary.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["entries"][8]["phrases"].append("리프트 트럭")
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    before = await _parse(registry_dir, "지게차")
    after = await _parse(changed, "지게차")

    assert before["targets"] == after["targets"]
    assert before["canonicalQuery"] != after["canonicalQuery"]


async def test_spec012_r18_llm_canonical_query_includes_prompt_version(
    registry_dir: Path,
) -> None:
    body = await _parse(registry_dir, OUT_OF_DICTIONARY, LLM_REPLY)

    assert str(body["promptVersion"]) in str(body["canonicalQuery"])


async def test_spec012_r8_english_query_uses_phrase(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        body = _ok(await post_parse(gw.client, "Find a forklift near the worker"))

    assert body["targets"] == ["a forklift near the worker"]
    assert body["parseMethod"] == "english-phrase"
    assert body["eventTypes"] == []
    assert gw.adapter.calls == []


async def test_spec012_r9_empty_english_phrase_falls_back_to_person(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        body = _ok(await post_parse(gw.client, "show me where"))

    assert body["targets"] == ["person"]
    assert body["parseMethod"] == "safe-fallback"
    assert gw.adapter.calls == []
    [entry] = gw.call_log.entries
    assert entry.status == "success"


async def test_spec012_r10_out_of_dictionary_query_asks_llm(registry_dir: Path) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=LLM_REPLY)) as gw:
        await post_parse(gw.client, OUT_OF_DICTIONARY)

    [call] = gw.adapter.calls
    assert call.model == "fake-llm"
    assert call.message.task_type == "query_parse"
    assert OUT_OF_DICTIONARY in call.message.variable_part
    assert call.images == []
    assert call.temperature == 0


async def test_spec012_r11_valid_llm_output_is_used(registry_dir: Path) -> None:
    body = await _parse(registry_dir, OUT_OF_DICTIONARY, LLM_REPLY)

    assert body["targets"] == ["ladder", "person"]
    assert body["eventTypes"] == []
    assert body["parseMethod"] == "llm"
    assert re.fullmatch(r"query_parse@[0-9a-f]{12}", str(body["promptVersion"]))
    assert body["visualQuery"] == "a photo of ladder and person"


async def test_spec012_r11_llm_output_in_code_block_is_used(registry_dir: Path) -> None:
    reply = '설명입니다.\n```json\n{"targets": ["ladder"], "eventTypes": ["fall"]}\n```'

    body = await _parse(registry_dir, OUT_OF_DICTIONARY, reply)

    assert (body["targets"], body["eventTypes"]) == (["ladder"], ["fall"])


async def test_spec012_r15_route_comes_from_dictionary_even_with_llm(
    registry_dir: Path,
) -> None:
    body = await _parse(registry_dir, "사다리 위에 계속 있는", LLM_REPLY)

    assert body["parseMethod"] == "llm"
    assert body["route"] == "temporal_video_rag"


async def test_spec012_r26_rule_parse_does_not_need_adapter(registry_dir: Path) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=TRANSIENT)) as gw:
        body = _ok(await post_parse(gw.client, RULE_QUERY))

    assert body["parseMethod"] == "industrial-rule"
    assert gw.adapter.calls == []


def _assert_safe_fallback(body: dict[str, object]) -> None:
    assert body["targets"] == ["person"]
    assert body["eventTypes"] == []
    assert body["parseMethod"] == "safe-fallback"
    assert body["promptVersion"] is None


async def test_spec012_r12_without_model_falls_back_without_calling(registry_dir: Path) -> None:
    settings = _parse_profile(adapter=None, model=None)
    async with gateway(registry_dir, settings) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    _assert_safe_fallback(body)
    assert gw.adapter.calls == []
    [entry] = gw.call_log.entries
    assert entry.status == "unavailable"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProviderUnavailableError("timeout"), "unavailable"),
        (ProviderFailedError("bad request"), "failed"),
    ],
)
async def test_spec012_r13_provider_error_falls_back(
    registry_dir: Path, error: Exception, status: str
) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=error)) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    _assert_safe_fallback(body)
    [entry] = gw.call_log.entries
    assert entry.status == status
    assert entry.error_code is not None


@pytest.mark.parametrize(
    "reply",
    [
        "대상을 고를 수 없습니다",
        '{"targets": [], "eventTypes": []}',
        '{"targets": ["사다리"], "eventTypes": []}',
        '{"targets": ["ladder"], "eventTypes": ["other"]}',
    ],
)
async def test_spec012_r14_invalid_llm_output_falls_back(registry_dir: Path, reply: str) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=reply)) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    _assert_safe_fallback(body)
    [entry] = gw.call_log.entries
    assert entry.status == "invalid_output"


@pytest.mark.parametrize("query", ["", "   ", "찾아줘", "장면 보여 줘"])
async def test_spec012_r20_empty_query_is_rejected(registry_dir: Path, query: str) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_parse(gw.client, query)

    _problem(response, 400, "AI_INVALID_REQUEST")
    assert gw.adapter.calls == []
    assert gw.call_log.entries == []


async def test_spec012_r21_too_long_query_is_rejected(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_parse(gw.client, "가" * 51)

    _problem(response, 400, "AI_INVALID_REQUEST")
    assert gw.adapter.calls == []
    assert gw.call_log.entries == []


@pytest.mark.parametrize("caller", [None, "unknown-service"])
async def test_spec012_r22_unknown_caller_is_rejected(
    registry_dir: Path, caller: str | None
) -> None:
    async with gateway(registry_dir) as gw:
        response = await post_parse(gw.client, OUT_OF_DICTIONARY, caller=caller)

    _problem(response, 403, "AI_CALLER_FORBIDDEN")
    assert gw.adapter.calls == []
    assert gw.call_log.entries == []


async def test_spec012_r27_rule_parse_is_logged_without_query(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        body = _ok(await post_parse(gw.client, RULE_QUERY))

    [entry] = gw.call_log.entries
    assert str(entry.call_id) == body["callId"]
    assert (entry.caller, entry.task_type, entry.status) == (CALLER, "query_parse", "success")
    assert (entry.source_id, entry.adapter, entry.model_id, entry.prompt_version) == (
        None,
        None,
        None,
        None,
    )
    assert entry.taxonomy_version == "v2"
    assert entry.output == {
        "parseMethod": "industrial-rule",
        "dictionaryVersion": body["dictionaryVersion"],
        "route": "video_retrieval",
        "eventTypes": ["ppe_missing"],
    }
    assert entry.raw_output is None
    assert entry.image_keys == []
    _assert_no_query_text(entry, RULE_QUERY)


async def test_spec012_r27_llm_parse_logs_model_and_tokens(registry_dir: Path) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=LLM_REPLY)) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    [entry] = gw.call_log.entries
    assert (entry.adapter, entry.model_id) == ("fake", "fake-llm")
    assert entry.prompt_version == body["promptVersion"]
    assert entry.input_tokens == 100
    assert entry.raw_output is None
    assert entry.output is not None
    assert entry.output["parseMethod"] == "llm"
    _assert_no_query_text(entry, OUT_OF_DICTIONARY)


def _assert_no_query_text(entry: object, query: str) -> None:
    dumped = json.dumps(entry, default=lambda o: getattr(o, "__dict__", str(o)))
    assert query not in dumped
    assert query.split()[0] not in repr(entry)


async def test_spec012_nfr3_logs_do_not_contain_query(
    registry_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with gateway(registry_dir, adapter=FakeAdapter(reply=LLM_REPLY)) as gw:
        await post_parse(gw.client, OUT_OF_DICTIONARY)
        await post_parse(gw.client, RULE_QUERY)

    completed = [r for r in caplog.records if r.getMessage() == "parse completed"]
    assert len(completed) == 2
    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert "사다리" not in text
        assert "안전모" not in text
        assert LLM_REPLY not in text


async def test_spec012_nfr2_llm_gets_profile_timeout(registry_dir: Path) -> None:
    settings = _parse_profile(timeout_s=3, max_retries=0)
    adapter = FakeAdapter(reply=LLM_REPLY)
    async with gateway(registry_dir, settings, adapter=adapter) as gw:
        await post_parse(gw.client, OUT_OF_DICTIONARY)

    [call] = gw.adapter.calls
    assert call.timeout_s == 3


async def test_spec012_nfr1_rule_parse_p95_is_under_30ms(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        await _timed(gw, RULE_QUERY)
        durations = [await _timed(gw, RULE_QUERY) for _ in range(200)]

    assert statistics.quantiles(durations, n=20)[18] < 0.030


async def _timed(gw: Gateway, query: str) -> float:
    started = time.perf_counter()
    _ok(await post_parse(gw.client, query))
    return time.perf_counter() - started


class BusyLimiter:
    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None:
        raise RateLimitedError("busy")


async def test_spec016_r2_llm_parse_is_cached_by_normalized_query(registry_dir: Path) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply=LLM_REPLY)) as gw:
        first = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))
        second = _ok(await post_parse(gw.client, f"{OUT_OF_DICTIONARY} 찾아줘"))

    assert len(gw.adapter.calls) == 1
    assert _without_call_id(first) == _without_call_id(second)
    called, cached = gw.call_log.entries
    assert (called.cache_hit, cached.cache_hit) == (False, True)
    assert cached.status == "success"
    assert cached.prompt_version == first["promptVersion"]


async def test_spec016_r3_invalid_llm_output_is_not_cached(registry_dir: Path) -> None:
    async with gateway(registry_dir, adapter=FakeAdapter(reply="모름")) as gw:
        await post_parse(gw.client, OUT_OF_DICTIONARY)
        await post_parse(gw.client, OUT_OF_DICTIONARY)

    assert len(gw.adapter.calls) == 2


async def test_spec016_r11_budget_exceeded_falls_back(registry_dir: Path) -> None:
    priced = profile(
        extra={
            "parse": parse_settings(),
            "prices": {
                "fake": {"fake-llm": {"input_per_million_usd": 1, "output_per_million_usd": 1}}
            },
        }
    )
    exhausted = MemoryBudget(limits={"*": Decimal(0)})
    async with gateway(
        registry_dir, priced, adapter=FakeAdapter(reply=LLM_REPLY), budget=exhausted
    ) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    _assert_safe_fallback(body)
    assert gw.adapter.calls == []
    [entry] = gw.call_log.entries
    assert entry.status == "budget_exceeded"


async def test_spec016_r16_rate_limited_falls_back(registry_dir: Path) -> None:
    async with gateway(
        registry_dir, adapter=FakeAdapter(reply=LLM_REPLY), limiter=BusyLimiter()
    ) as gw:
        body = _ok(await post_parse(gw.client, OUT_OF_DICTIONARY))

    _assert_safe_fallback(body)
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == ("unavailable", "AI_RATE_LIMITED")


async def test_spec016_r1_rule_parse_does_not_use_cache(registry_dir: Path) -> None:
    async with gateway(registry_dir) as gw:
        await post_parse(gw.client, RULE_QUERY)

    assert gw.cache.values == {}
    [entry] = gw.call_log.entries
    assert entry.cache_hit is None
