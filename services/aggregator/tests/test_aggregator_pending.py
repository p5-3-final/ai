"""대기 집합·재판정 계수를 실제 Redis로 확인한다(SPEC-027 R-25, R-29, R-31)."""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from agg_support import CHUNK_A, event, uid
from aggregator.judge_budget import JudgeBudget
from aggregator.kv import RedisStore
from aggregator.pending import (
    VECTOR_TTL_S,
    WINDOW_TTL_S,
    PendingVectors,
    PendingWindows,
    StoredEvent,
    StoredTracks,
    WindowManifest,
)
from redis.asyncio import Redis
from vlm_testing import containers

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with containers.redis() as url:
        yield url


@pytest.fixture
async def store(redis_url: str) -> AsyncIterator[RedisStore]:
    kv = RedisStore(redis_url)
    yield kv
    await kv.aclose()


async def _ttl(redis_url: str, key: str) -> int:
    client = Redis.from_url(redis_url)
    try:
        return int(await client.ttl(key))
    finally:
        await client.aclose()


async def test_spec027_r25_vector_events_are_kept_once_per_event_id(
    store: RedisStore, redis_url: str
) -> None:
    """[SPEC-027 R-25] `pending-vector:<jobId>`에 eventId로 넣는다. 같은 이벤트는 한 번, TTL 7일."""
    job = uuid4()
    pending = PendingVectors(store)
    stored = StoredEvent.of(event(1, start=0, method="vector+vlm"))

    await pending.add(job, stored)
    await pending.add(job, stored)
    await pending.add(job, StoredEvent.of(event(2, start=10, method="vector+vlm")))

    assert sorted(e.event_id for e in await pending.all(job)) == [uid(1), uid(2)]
    assert 0 < await _ttl(redis_url, f"pending-vector:{job}") <= VECTOR_TTL_S


async def test_spec027_r29_window_collects_events_tracks_and_manifest(
    store: RedisStore, redis_url: str
) -> None:
    """[SPEC-027 R-29] `pending:<sourceId>:<chunkId>`(TTL 1시간)에 창 입력을 모은다."""
    source = uuid4()
    windows = PendingWindows(store)
    tracks_key = f"sources/{source}/jobs/{uid(1)}/chunks/{CHUNK_A}/executions/{uid(2)}/tracks.json"

    await windows.add_event(source, CHUNK_A, StoredEvent.of(event(1, start=0, method="rule")))
    await windows.add_tracks(
        source, CHUNK_A, StoredTracks(execution_id=uid(2), tracks_key=tracks_key)
    )
    await windows.set_manifest(
        source, CHUNK_A, WindowManifest(start_ms=0, end_ms=20_000, gap_ms=0, last=False)
    )

    loaded = await windows.load(source, CHUNK_A)
    assert [e.event_id for e in loaded.events] == [uid(1)]
    assert [t.tracks_key for t in loaded.tracks] == [tracks_key]
    assert loaded.manifest is not None
    assert loaded.manifest.end_ms == 20_000
    assert 0 < await _ttl(redis_url, f"pending:{source}:{CHUNK_A}") <= WINDOW_TTL_S


async def test_spec027_r31_judge_budget_stops_at_limit(store: RedisStore) -> None:
    """[SPEC-027 R-31] job당 재판정 호출을 Redis 계수로 세고 상한을 넘으면 거절한다."""
    budget = JudgeBudget(store)
    job = uuid4()

    taken = [await budget.try_acquire(job, limit=2) for _ in range(3)]

    assert taken == [True, True, False]
