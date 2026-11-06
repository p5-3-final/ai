"""외부 모델 하루 비용 예산(SPEC-016 R-10~R-14, R-21, R-22).

사용자 상한이 아니라 재시도 루프·잘못된 재처리 같은 사고로 비용이 폭주하는 것을 막는 안전장치다.
예산은 Redis가 아니라 DB 조건부 갱신으로 지킨다. Redis가 끊겨도 한도가 그대로여야 하기 때문이다
(R-17). 여러 Pod가 동시에 예약해도 `spent + cost <= limit` 조건을 행 잠금 안에서 확인하므로 한도를
넘는 예약이 생기지 않는다(NFR-2).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ai_gateway.db.tables import budget_usage
from ai_gateway.profile import BudgetProfile, ModelPrice

# 전체 한도의 범위 이름. 호출 서비스 이름은 소문자로 시작해 겹치지 않는다(callers 형식).
TOTAL_SCOPE = "*"
_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Reservation:
    """호출 하나의 예약. 끝나면 정산하거나 되돌린다."""

    day: date
    scopes: tuple[str, ...]
    cost: Decimal


def cost_of(price: ModelPrice | None, input_tokens: int, output_tokens: int) -> Decimal:
    """토큰 수로 비용(USD)을 계산한다. 단가가 없는 모델은 0이다(R-14)."""
    if price is None:
        return Decimal(0)
    return (
        price.input_per_million_usd * input_tokens + price.output_per_million_usd * output_tokens
    ) / _PER_MILLION


class Budget:
    """호출 서비스별·전체 하루(UTC) 예산.

    Args:
        engine: ai_db 엔진.
        settings: 한도. None이면 한도 없이 사용량만 센다.
        clock: 날짜를 정하는 시계. 테스트가 바꾼다.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        settings: BudgetProfile | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._clock = clock

    async def reserve(self, caller: str, cost: Decimal) -> Reservation | None:
        """예상 비용을 호출 서비스와 전체 예산에서 함께 예약한다(R-10).

        Returns:
            예약. 어느 한쪽이라도 남은 금액이 모자라면 None이고 아무것도 예약하지 않는다(R-11).
        """
        day = self._clock().date()
        # 호출 서비스 한도가 없으면 전체 한도만 적용한다(R-21).
        # 잠그는 순서를 늘 같게 둬 교착을 막는다.
        scopes = (caller, TOTAL_SCOPE)
        async with self._engine.begin() as conn:
            for scope in scopes:
                if not await self._add_within_limit(conn, day, scope, cost):
                    await conn.rollback()
                    return None
        return Reservation(day, scopes, cost)

    async def settle(self, reservation: Reservation, actual: Decimal) -> None:
        """예약을 실제 비용으로 바꾼다(R-12, R-22). 한도를 넘어도 기록한다(NFR-2)."""
        await self._adjust(reservation, actual - reservation.cost)

    async def release(self, reservation: Reservation) -> None:
        """보내지 못한 호출의 예약을 되돌린다(R-13)."""
        await self._adjust(reservation, -reservation.cost)

    async def spent(self, day: date, scope: str) -> Decimal:
        """그날 범위의 예약·정산 합계."""
        async with self._engine.connect() as conn:
            value = await conn.scalar(
                select(budget_usage.c.spent_usd).where(
                    budget_usage.c.day == day, budget_usage.c.scope == scope
                )
            )
        return Decimal(value) if value is not None else Decimal(0)

    def _limit(self, scope: str) -> Decimal | None:
        if self._settings is None:
            return None
        if scope == TOTAL_SCOPE:
            return self._settings.daily_limit_usd
        return self._settings.limit_for(scope)

    async def _add_within_limit(
        self, conn: AsyncConnection, day: date, scope: str, cost: Decimal
    ) -> bool:
        await conn.execute(
            insert(budget_usage)
            .values(day=day, scope=scope, spent_usd=Decimal(0))
            .on_conflict_do_nothing(index_elements=[budget_usage.c.day, budget_usage.c.scope])
        )
        statement = (
            update(budget_usage)
            .where(budget_usage.c.day == day, budget_usage.c.scope == scope)
            .values(spent_usd=budget_usage.c.spent_usd + cost)
            .returning(budget_usage.c.scope)
        )
        limit = self._limit(scope)
        if limit is not None:
            # 비용 0인 호출은 한도를 다 써도 통과한다(R-14).
            statement = statement.where(budget_usage.c.spent_usd + cost <= limit)
        return (await conn.execute(statement)).first() is not None

    async def _adjust(self, reservation: Reservation, delta: Decimal) -> None:
        if delta == 0:
            return
        async with self._engine.begin() as conn:
            for scope in reservation.scopes:
                await conn.execute(
                    update(budget_usage)
                    .where(budget_usage.c.day == reservation.day, budget_usage.c.scope == scope)
                    .values(spent_usd=budget_usage.c.spent_usd + delta)
                )
