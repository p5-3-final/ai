"""실시간 세션당 판정 호출 상한(SPEC-028 R-8, R-19). 실제 Redis로 계수를 확인한다."""

from collections.abc import AsyncIterator, Iterator

import pytest
from agg_support import uid
from aggregator.judge import LocalBudget, SessionBudget
from aggregator.judge_budget import JUDGE_CALLS_TTL_S, JudgeBudget
from aggregator.kv import RedisStore
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


async def test_spec028_r19_session_cap_shares_the_rejudge_counter(
    store: RedisStore, redis_url: str
) -> None:
    """[SPEC-028 R-8·R-19] 재고 판정과 위험구역 접근 재판정이 세션 계수 하나를 센다.

    계수는 SPEC-027 R-31과 같은 `agg:judge-calls:<jobId>`다.
    """
    job, other = uid(0x5101), uid(0x5102)
    budget = JudgeBudget(store)
    session = SessionBudget(budget, job, limit=2)

    assert await budget.try_acquire(job, limit=2)  # 접근 재판정 한 번
    assert await session.try_acquire()
    assert not await session.try_acquire()
    assert await SessionBudget(budget, other, limit=2).try_acquire()

    client = Redis.from_url(redis_url)
    try:
        ttl = int(await client.ttl(f"agg:judge-calls:{job}"))
    finally:
        await client.aclose()
    assert 0 < ttl <= JUDGE_CALLS_TTL_S


async def test_spec028_r8_upload_cap_is_counted_in_process() -> None:
    """[SPEC-028 R-8] 업로드 job은 메시지마다 프로세스 안 계수다."""
    budget = LocalBudget(1)

    assert await budget.try_acquire()
    assert not await budget.try_acquire()
