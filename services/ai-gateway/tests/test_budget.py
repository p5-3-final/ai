import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from ai_gateway.budget import Budget, cost_of
from ai_gateway.db.migrate import upgrade
from ai_gateway.profile import BudgetProfile, ModelPrice
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# 테스트마다 날짜를 다르게 써서 같은 DB에서도 사용량이 섞이지 않게 한다.
_BASE = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    created = create_async_engine(database_url, pool_size=20)
    yield created
    await created.dispose()


def _clock() -> datetime:
    return _BASE + timedelta(days=uuid4().int % 100_000)


def _budget(engine: AsyncEngine, now: datetime, total: str, **callers: str) -> Budget:
    settings = BudgetProfile(
        daily_limit_usd=Decimal(total), callers={k: Decimal(v) for k, v in callers.items()}
    )
    return Budget(engine, settings, clock=lambda: now)


def test_spec016_r10_cost_uses_price_per_million_tokens() -> None:
    price = ModelPrice(input_per_million_usd=Decimal("2"), output_per_million_usd=Decimal("8"))

    assert cost_of(price, 1_000, 500) == Decimal("0.006")


def test_spec016_r14_model_without_price_costs_nothing() -> None:
    assert cost_of(None, 1_000_000, 1_000_000) == Decimal(0)


@pytest.mark.integration
async def test_spec016_r10_reserves_from_caller_and_total(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "10", **{"event-analyzer": "5"})

    reservation = await budget.reserve("event-analyzer", Decimal("2"))

    assert reservation is not None
    assert await budget.spent(now.date(), "event-analyzer") == Decimal("2")
    assert await budget.spent(now.date(), "*") == Decimal("2")


@pytest.mark.integration
async def test_spec016_r11_caller_limit_blocks_only_that_caller(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "100", **{"event-analyzer": "5"})
    assert await budget.reserve("event-analyzer", Decimal("5")) is not None

    assert await budget.reserve("event-analyzer", Decimal("1")) is None
    assert await budget.reserve("search-service", Decimal("1")) is not None
    assert await budget.spent(now.date(), "event-analyzer") == Decimal("5")


@pytest.mark.integration
async def test_spec016_r11_total_limit_blocks_every_caller(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "3", **{"event-analyzer": "5"})
    assert await budget.reserve("search-service", Decimal("3")) is not None

    assert await budget.reserve("event-analyzer", Decimal("1")) is None
    # 전체 한도에 걸린 예약은 호출 서비스 사용량도 남기지 않는다.
    assert await budget.spent(now.date(), "event-analyzer") == Decimal(0)


@pytest.mark.integration
async def test_spec016_r12_settle_replaces_estimate(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "10")
    reservation = await budget.reserve("search-service", Decimal("2"))
    assert reservation is not None

    await budget.settle(reservation, Decimal("0.5"))

    assert await budget.spent(now.date(), "*") == Decimal("0.5")
    assert await budget.spent(now.date(), "search-service") == Decimal("0.5")


@pytest.mark.integration
async def test_spec016_r13_release_returns_reservation(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "10")
    reservation = await budget.reserve("search-service", Decimal("2"))
    assert reservation is not None

    await budget.release(reservation)

    assert await budget.spent(now.date(), "*") == Decimal(0)


@pytest.mark.integration
async def test_spec016_r14_zero_cost_passes_even_when_exhausted(engine: AsyncEngine) -> None:
    now = _clock()
    budget = _budget(engine, now, "1")
    assert await budget.reserve("search-service", Decimal("1")) is not None

    assert await budget.reserve("search-service", Decimal(0)) is not None


@pytest.mark.integration
async def test_spec016_r10_next_day_starts_fresh(engine: AsyncEngine) -> None:
    now = _clock()
    assert await _budget(engine, now, "1").reserve("search-service", Decimal("1")) is not None

    tomorrow = _budget(engine, now + timedelta(days=1), "1")

    assert await tomorrow.reserve("search-service", Decimal("1")) is not None


@pytest.mark.integration
async def test_spec016_nfr2_overspend_blocks_later_reservations(engine: AsyncEngine) -> None:
    """[SPEC-016 NFR-2] 정산이 예상보다 커서 넘은 날은 다음 예약부터 막는다."""
    now = _clock()
    budget = _budget(engine, now, "1")
    reservation = await budget.reserve("search-service", Decimal("0.5"))
    assert reservation is not None
    await budget.settle(reservation, Decimal("1.2"))

    assert await budget.reserve("search-service", Decimal("0.01")) is None


@pytest.mark.integration
async def test_spec016_nfr2_concurrent_reservations_never_exceed_limit(
    engine: AsyncEngine,
) -> None:
    """[SPEC-016 NFR-2] 동시 예약 100건에서 한도를 넘는 예약은 0건이다."""
    now = _clock()
    budgets = [_budget(engine, now, "30") for _ in range(4)]

    results = await asyncio.gather(
        *(budgets[i % 4].reserve(f"caller-{i % 3}", Decimal("1")) for i in range(100))
    )

    assert sum(r is not None for r in results) == 30
    assert await budgets[0].spent(now.date(), "*") == Decimal("30")


@pytest.mark.integration
async def test_spec016_r10_without_budget_profile_always_reserves(engine: AsyncEngine) -> None:
    now = _clock()
    budget = Budget(engine, None, clock=lambda: now)

    assert await budget.reserve("search-service", Decimal("1000000")) is not None
