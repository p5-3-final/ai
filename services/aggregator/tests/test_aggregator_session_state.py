"""실시간 세션 상태 저장소(SPEC-027 R-30)."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from agg_support import JOB, risk_event, uid
from aggregator.fold import SessionState, fold
from aggregator.kv import MemoryStore, RedisStore
from aggregator.session_state import STATE_TTL_S, SessionStateStore
from redis.asyncio import Redis
from vlm_storage.memory import InMemoryStorage
from vlm_testing import containers


def _active(source: UUID) -> SessionState:
    state, _ = fold(
        SessionState(source_id=source),
        [risk_event(1, 10_000, detected=True)],
        job_id=JOB,
    )
    return state.model_copy(update={"last_sequence": 30, "last_window_end_ms": 600_000})


async def test_spec027_r30_new_session_without_history_starts_empty() -> None:
    """[SPEC-027 R-30] Redis·S3·직전 세션 상태가 모두 없으면 빈 상태다."""
    source = uuid4()

    state = await SessionStateStore(MemoryStore(), InMemoryStorage()).load(source, None)

    assert (state.active_risks, state.last_sequence, state.source_id) == ((), 0, source)


async def test_spec027_r30_redis_state_is_used_first() -> None:
    source = uuid4()
    store = SessionStateStore(MemoryStore(), InMemoryStorage())
    await store.save(source, _active(source), snapshot=False)

    state = await store.load(source, None)

    assert state.last_sequence == 30


async def test_spec027_r30_s3_snapshot_restores_when_redis_is_lost() -> None:
    """[SPEC-027 R-30] 30창마다 남긴 S3 스냅샷으로 Redis를 잃어도 이어 간다."""
    source = uuid4()
    storage = InMemoryStorage()
    await SessionStateStore(MemoryStore(), storage).save(source, _active(source), snapshot=True)

    restored = await SessionStateStore(MemoryStore(), storage).load(source, None)

    assert storage.exists(f"sources/{source}/state/30.json")
    assert restored.last_sequence == 30


async def test_spec027_r30_previous_session_state_is_carried_over() -> None:
    """[SPEC-027 R-30] 새 회전 세션은 직전 세션의 마지막 상태에서 이어 온 위험으로 시작한다."""
    previous, source = uuid4(), uuid4()
    store = SessionStateStore(MemoryStore(), InMemoryStorage())
    await store.save(previous, _active(previous), snapshot=False)

    state = await store.load(source, previous)

    [risk] = state.active_risks
    assert (risk.carried_over, risk.first_seen_ms, state.last_sequence) == (True, 0, 0)
    assert (state.source_id, state.from_source_id) == (source, previous)


async def test_spec027_r30_processed_windows_are_remembered() -> None:
    source = uuid4()
    store = SessionStateStore(MemoryStore(), InMemoryStorage())

    await store.mark_window(source, uid(5), 3)

    assert (
        await store.window_sequence(source, uid(5)),
        await store.window_sequence(source, uid(6)),
    ) == (3, None)


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with containers.redis() as url:
        yield url


@pytest.mark.integration
async def test_spec027_r30_state_round_trips_through_redis_with_ttl(redis_url: str) -> None:
    """[SPEC-027 R-30] Redis `agg:state:<sourceId>`에 쓰고 TTL 48시간을 둔다."""
    source = uuid4()
    kv = RedisStore(redis_url)
    try:
        store = SessionStateStore(kv, InMemoryStorage())
        await store.save(source, _active(source), snapshot=False)
        assert (await store.load(source, None)) == _active(source)
    finally:
        await kv.aclose()
    client = Redis.from_url(redis_url)
    try:
        assert 0 < int(await client.ttl(f"agg:state:{source}")) <= STATE_TTL_S
    finally:
        await client.aclose()
