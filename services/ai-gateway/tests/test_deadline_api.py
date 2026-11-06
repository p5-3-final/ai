"""`X-Request-Deadline` 처리(SPEC-021 R-1~R-5). 세 operation 모두 같은 규칙이다."""

import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from ai_gateway.adapters.base import ProviderUnavailableError
from gateway_support import (
    FakeAdapter,
    FakeEmbedder,
    Gateway,
    MemoryBudget,
    MemoryCache,
    analyze_body,
    embed_settings,
    gateway,
    output_example,
    parse_settings,
    post_analyze,
    post_embed,
    post_parse,
    profile,
)

HEADER = "X-Request-Deadline"
# LLM 해석 경로로 가는 질의(사전·영어 구문에 맞지 않는다)
LLM_QUERY = "사다리 위에 올라간"
PRICES = {"fake": {"fake-vlm": {"input_per_million_usd": 2, "output_per_million_usd": 8}}}

type Post = Callable[[Gateway, dict[str, str]], Awaitable[httpx.Response]]


def _in(seconds: float) -> dict[str, str]:
    return {HEADER: str(int((time.time() + seconds) * 1000))}


async def _analyze(gw: Gateway, headers: dict[str, str]) -> httpx.Response:
    return await post_analyze(gw.client, analyze_body(), headers=headers)


async def _embed(gw: Gateway, headers: dict[str, str]) -> httpx.Response:
    return await post_embed(gw.client, {"taskType": "embed_text", "texts": ["a"]}, headers=headers)


async def _parse(gw: Gateway, headers: dict[str, str]) -> httpx.Response:
    return await post_parse(gw.client, LLM_QUERY, headers=headers)


_POSTS: dict[str, Post] = {"analyze": _analyze, "embed": _embed, "parse": _parse}


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, object] = response.json()
    assert body["code"] == code
    return body


@pytest.mark.parametrize("operation", sorted(_POSTS))
@pytest.mark.parametrize("value", ["", "-1", "1.5", "12345678901234567", "soon", " 1"])
async def test_spec021_r4_malformed_deadline_is_400(
    registry_dir: Path, operation: str, value: str
) -> None:
    """[SPEC-021 R-4] 0 이상의 정수(epoch ms)가 아니면 400이고 오류 필드는 헤더 이름이다."""
    async with gateway(registry_dir, profile(embed_settings())) as gw:
        response = await _POSTS[operation](gw, {HEADER: value})

    body = _problem(response, 400, "AI_INVALID_REQUEST")
    assert body["errors"] == [{"field": HEADER, "message": "must be epoch milliseconds"}]


@pytest.mark.parametrize("operation", sorted(_POSTS))
async def test_spec021_r2_passed_deadline_is_504_without_any_work(
    registry_dir: Path, operation: str
) -> None:
    """[SPEC-021 R-2] 지난 마감이면 저장소·캐시·예산·외부 모델·호출 로그를 부르지 않고 504다."""
    embedder = FakeEmbedder()
    async with gateway(registry_dir, profile(embed_settings()), embedders={"fake": embedder}) as gw:
        response = await _POSTS[operation](gw, _in(-1))

    _problem(response, 504, "AI_DEADLINE_EXCEEDED")
    assert gw.storage.reads == 0
    assert gw.cache.calls == 0
    assert gw.budget.reserves == 0
    assert gw.adapter.calls == []
    assert embedder.calls == []
    assert gw.call_log.entries == []


async def test_spec021_r2_zero_deadline_is_passed(registry_dir: Path) -> None:
    """[SPEC-021 R-2] 0은 형식상 맞지만 이미 지난 마감이다."""
    async with gateway(registry_dir) as gw:
        response = await _analyze(gw, {HEADER: "0"})

    _problem(response, 504, "AI_DEADLINE_EXCEEDED")


def _valid_reply(registry_dir: Path) -> str:
    return json.dumps(output_example(registry_dir), ensure_ascii=False)


async def test_spec021_r5_without_header_adapters_get_profile_timeout(registry_dir: Path) -> None:
    """[SPEC-021 R-5] 헤더가 없으면 프로필 제한 시간을 그대로 쓴다(analyze, embed_text, parse)."""
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    embedder = FakeEmbedder()
    gateway_profile = profile({**embed_settings(), "parse": parse_settings(timeout_s=7)})
    async with gateway(
        registry_dir, gateway_profile, adapter=adapter, embedders={"fake": embedder}
    ) as gw:
        for post in _POSTS.values():
            assert (await post(gw, {})).status_code == 200

    analyze_call, parse_call = adapter.calls
    assert analyze_call.timeout_s == 5
    assert parse_call.timeout_s == 7
    [embed_call] = embedder.calls
    assert embed_call.timeout_s == 5


@pytest.mark.parametrize(("seconds", "expected_max"), [(2, 2.0), (60, 5.0)])
async def test_spec021_r1_analyze_adapter_timeout_is_shorter_of_remaining_and_profile(
    registry_dir: Path, seconds: float, expected_max: float
) -> None:
    """[SPEC-021 R-1] Adapter 제한 시간 = min(호출 시점의 남은 시간, 프로필 5초)."""
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await _analyze(gw, _in(seconds))

    assert response.status_code == 200
    [call] = adapter.calls
    assert expected_max - 0.5 < call.timeout_s <= expected_max


async def test_spec021_r1_embed_text_and_parse_llm_use_remaining_time(registry_dir: Path) -> None:
    """[SPEC-021 R-1] embed_text·parse LLM 경로도 같은 규칙이다."""
    adapter = FakeAdapter(reply='{"targets": ["ladder"], "eventTypes": ["fall"]}')
    embedder = FakeEmbedder()
    async with gateway(
        registry_dir, profile(embed_settings()), adapter=adapter, embedders={"fake": embedder}
    ) as gw:
        assert (await _embed(gw, _in(2))).status_code == 200
        assert (await _parse(gw, _in(2))).status_code == 200

    [embed_call] = embedder.calls
    [parse_call] = adapter.calls
    assert 1.5 < embed_call.timeout_s <= 2
    assert 1.5 < parse_call.timeout_s <= 2


async def test_spec021_r3_slow_analyze_is_504_logged_and_settled(registry_dir: Path) -> None:
    """[SPEC-021 R-3] 남은 시간 안에 끝나지 않으면 504, 호출 로그 unavailable, 예약액 정산."""
    adapter = FakeAdapter(reply=_valid_reply(registry_dir), delay_s=5)
    priced = profile(extra={"prices": PRICES})
    async with gateway(registry_dir, priced, adapter=adapter) as gw:
        response = await _analyze(gw, _in(0.3))

    _problem(response, 504, "AI_DEADLINE_EXCEEDED")
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == ("unavailable", "AI_DEADLINE_EXCEEDED")
    assert entry.reserved_cost_usd is not None
    assert entry.settled_cost_usd == entry.reserved_cost_usd
    assert gw.budget.spent["event-analyzer"] == entry.reserved_cost_usd


async def test_spec021_r3_profile_timeout_first_stays_503(registry_dir: Path) -> None:
    """[SPEC-021 R-3] 마감이 남았는데 공급자 시간 초과로 끝나면 지금처럼 503이다."""
    adapter = FakeAdapter(reply=ProviderUnavailableError("timeout"))
    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await _analyze(gw, _in(60))

    _problem(response, 503, "AI_PROVIDER_UNAVAILABLE")


async def test_spec021_r3_slow_embed_text_is_504(registry_dir: Path) -> None:
    """[SPEC-021 R-3] embed_text도 504다."""
    embedder = FakeEmbedder(delay_s=5)
    async with gateway(registry_dir, profile(embed_settings()), embedders={"fake": embedder}) as gw:
        response = await _embed(gw, _in(0.3))

    _problem(response, 504, "AI_DEADLINE_EXCEEDED")
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == ("unavailable", "AI_DEADLINE_EXCEEDED")


async def test_spec021_r3_slow_parse_llm_is_504_instead_of_fallback(registry_dir: Path) -> None:
    """[SPEC-021 R-3] parse LLM 경로는 safe-fallback 대신 504이고 호출 로그는 남긴다."""
    adapter = FakeAdapter(reply='{"targets": ["ladder"], "eventTypes": []}', delay_s=5)
    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await _parse(gw, _in(0.3))

    _problem(response, 504, "AI_DEADLINE_EXCEEDED")
    [entry] = gw.call_log.entries
    assert (entry.status, entry.error_code) == ("unavailable", "AI_DEADLINE_EXCEEDED")


async def test_spec021_r3_parse_provider_error_with_time_left_keeps_fallback(
    registry_dir: Path,
) -> None:
    """[SPEC-021 R-3] 마감이 남은 공급자 오류는 지금처럼 safe-fallback 200이다(SPEC-012)."""
    adapter = FakeAdapter(reply=ProviderUnavailableError("down"))
    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await _parse(gw, _in(60))

    assert response.status_code == 200
    assert response.json()["parseMethod"] == "safe-fallback"


async def test_spec021_r1_cached_analyze_within_deadline_is_200(registry_dir: Path) -> None:
    """[SPEC-021 R-1] 캐시 적중은 외부 모델을 부르지 않아 남은 시간과 상관없이 결과를 돌려준다."""
    adapter = FakeAdapter(reply=_valid_reply(registry_dir))
    cache = MemoryCache()
    budget = MemoryBudget(limits={"*": Decimal(1)})
    async with gateway(registry_dir, adapter=adapter, cache=cache, budget=budget) as gw:
        await _analyze(gw, {})
        response = await _analyze(gw, _in(1))

    assert response.status_code == 200
    assert len(adapter.calls) == 1
