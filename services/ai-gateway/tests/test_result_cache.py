import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from ai_gateway.db.migrate import upgrade
from ai_gateway.profile import CacheProfile
from ai_gateway.result_cache import FrontUnavailableError, ResultCache
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

SETTINGS = CacheProfile(retention_days=7, front_ttl_s=600)
# 아무도 듣지 않는 포트. 연결 거부로 Redis 장애를 흉내 낸다.
DOWN = "redis://127.0.0.1:1/0"
VALUE: dict[str, object] = {"output": {"eventType": "fall"}, "modelId": "m"}


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    created = create_async_engine(database_url)
    yield created
    await created.dispose()


@pytest.fixture
async def front(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


def _key() -> str:
    return f"test-{uuid4().hex}"


async def test_spec016_r1_r2_saved_result_is_found_by_another_pod(
    engine: AsyncEngine, redis_url: str
) -> None:
    key = _key()
    first, second = (
        ResultCache(engine, SETTINGS, redis_url),
        ResultCache(engine, SETTINGS, redis_url),
    )

    await first.put(key, "event_judge", uuid4(), VALUE)

    assert await second.get(key) == VALUE
    await first.close()
    await second.close()


async def test_spec016_r2_unknown_key_is_none(engine: AsyncEngine, redis_url: str) -> None:
    cache = ResultCache(engine, SETTINGS, redis_url)

    assert await cache.get(_key()) is None
    await cache.close()


async def test_spec016_r6_db_result_refills_front(
    engine: AsyncEngine, redis_url: str, front: Redis
) -> None:
    key = _key()
    cache = ResultCache(engine, SETTINGS, redis_url)
    await cache.put(key, "event_judge", None, VALUE)
    await front.flushdb()

    assert await cache.get(key) == VALUE
    assert await front.exists(*[k async for k in front.scan_iter(f"*{key}*")]) == 1
    await cache.close()


async def test_spec016_r2_result_older_than_retention_is_not_used(
    engine: AsyncEngine, front: Redis
) -> None:
    key = _key()
    now = datetime.now(UTC)
    await ResultCache(engine, SETTINGS, None, clock=lambda: now).put(
        key, "event_judge", None, VALUE
    )
    later = ResultCache(engine, SETTINGS, None, clock=lambda: now + timedelta(days=8))

    assert await later.get(key) is None


async def test_spec016_r9_delete_source_removes_its_results(
    engine: AsyncEngine, redis_url: str
) -> None:
    cache = ResultCache(engine, SETTINGS, redis_url)
    deleted, kept = uuid4(), uuid4()
    keys = [_key(), _key()]
    other = _key()
    for key in keys:
        await cache.put(key, "event_judge", deleted, VALUE)
    await cache.put(other, "event_judge", kept, VALUE)

    assert await cache.delete_source(deleted) == 2

    assert [await cache.get(k) for k in keys] == [None, None]
    assert await ResultCache(engine, SETTINGS, None).get(keys[0]) is None
    assert await cache.get(other) == VALUE
    await cache.close()


async def test_spec016_r17_front_down_uses_db_only(engine: AsyncEngine) -> None:
    key = _key()
    cache = ResultCache(engine, SETTINGS, DOWN)

    await cache.put(key, "query_parse", None, VALUE)

    assert await cache.get(key) == VALUE
    assert await cache.try_lock(key, 5) is None
    await cache.close()


async def test_spec016_r9_delete_with_front_down_fails_for_retry(engine: AsyncEngine) -> None:
    """앞단을 못 지우면 지운 영상 결과가 TTL 동안 남으므로 실패시켜 다시 받게 한다."""
    cache = ResultCache(engine, SETTINGS, DOWN)
    source = uuid4()
    await cache.put(_key(), "event_judge", source, VALUE)

    with pytest.raises(FrontUnavailableError):
        await cache.delete_source(source)
    await cache.close()


async def test_spec016_r7_only_one_pod_holds_the_lock(engine: AsyncEngine, redis_url: str) -> None:
    key = _key()
    first, second = (
        ResultCache(engine, SETTINGS, redis_url),
        ResultCache(engine, SETTINGS, redis_url),
    )

    assert await first.try_lock(key, 5) is True
    assert await second.try_lock(key, 5) is False
    await first.release(key)
    assert await second.try_lock(key, 5) is True
    await first.close()
    await second.close()


async def test_spec016_r7_waiter_gets_leader_result(engine: AsyncEngine, redis_url: str) -> None:
    key = _key()
    leader, waiter = (
        ResultCache(engine, SETTINGS, redis_url),
        ResultCache(engine, SETTINGS, redis_url),
    )
    assert await leader.try_lock(key, 5)

    async def finish() -> None:
        await asyncio.sleep(0.2)
        await leader.put(key, "event_judge", None, VALUE)
        await leader.release(key)

    task = asyncio.create_task(finish())
    assert await waiter.wait_for(key, 5) == VALUE
    await task
    await leader.close()
    await waiter.close()


async def test_spec016_r8_waiter_stops_when_leader_fails(
    engine: AsyncEngine, redis_url: str
) -> None:
    key = _key()
    leader, waiter = (
        ResultCache(engine, SETTINGS, redis_url),
        ResultCache(engine, SETTINGS, redis_url),
    )
    assert await leader.try_lock(key, 5)

    async def fail() -> None:
        await asyncio.sleep(0.2)
        await leader.release(key)

    task = asyncio.create_task(fail())
    started = asyncio.get_running_loop().time()
    assert await waiter.wait_for(key, 5) is None
    assert asyncio.get_running_loop().time() - started < 2
    await task
    await leader.close()
    await waiter.close()


async def test_spec016_r8_waiter_stops_at_timeout(engine: AsyncEngine, redis_url: str) -> None:
    key = _key()
    leader, waiter = (
        ResultCache(engine, SETTINGS, redis_url),
        ResultCache(engine, SETTINGS, redis_url),
    )
    assert await leader.try_lock(key, 30)

    assert await waiter.wait_for(key, 0.3) is None
    await leader.close()
    await waiter.close()
