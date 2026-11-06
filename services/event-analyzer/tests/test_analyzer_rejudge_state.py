"""실시간 재판정 간격 상태(SPEC-023 R-18). Redis가 없으면 판정하도록 빈 상태를 돌려준다."""

from collections.abc import Iterator
from uuid import UUID

import pytest
from ea_support import SOURCE, judged
from event_analyzer.rejudge_state import NoRejudgeState, Prior, RedisRejudgeState
from redis.asyncio import Redis
from vlm_contracts.events import DetectableEventType, VLMAnalysis
from vlm_testing.containers import redis

# 열려 있지 않은 포트. 연결이 바로 거절된다.
DOWN = "redis://127.0.0.1:1/0"
PRIOR = Prior(
    start_ms=20_000,
    event_id=UUID(int=0xE001),
    analysis=VLMAnalysis.model_validate(judged("ppe_missing")),
)


async def test_spec023_r18_no_state_has_no_prior() -> None:
    state = NoRejudgeState()
    await state.write(SOURCE, ("t1", "ppe_missing"), PRIOR, 240)

    assert await state.read(SOURCE, [("t1", "ppe_missing")]) == {}


async def test_spec023_r18_unreachable_redis_reads_nothing_and_ignores_writes() -> None:
    state = RedisRejudgeState(DOWN)

    await state.write(SOURCE, ("t1", "ppe_missing"), PRIOR, 240)

    assert await state.read(SOURCE, [("t1", "ppe_missing")]) == {}


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with redis() as url:
        yield url


@pytest.mark.integration
async def test_spec023_r18_written_prior_is_read_back_with_ttl(redis_url: str) -> None:
    state = RedisRejudgeState(redis_url)
    key: tuple[str, DetectableEventType] = ("t1", "ppe_missing")

    await state.write(SOURCE, key, PRIOR, 240)

    assert await state.read(SOURCE, [key, ("t2", "ppe_missing")]) == {key: PRIOR}
    client = Redis.from_url(redis_url)
    try:
        ttl = await client.ttl(f"rejudge:{SOURCE}:t1:ppe_missing")
    finally:
        await client.aclose()
    assert 230 <= ttl <= 240


@pytest.mark.integration
async def test_spec023_r18_unreadable_value_is_ignored(redis_url: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        await client.set(f"rejudge:{SOURCE}:t3:ppe_missing", b"not json", ex=60)
    finally:
        await client.aclose()

    assert await RedisRejudgeState(redis_url).read(SOURCE, [("t3", "ppe_missing")]) == {}
