"""외부 모델 호출 하나를 캐시·예산·분당 한도로 감싼다(SPEC-016).

순서는 docs overview "AI Gateway" 처리 순서와 같다: 결과 캐시 → 동시 호출 합치기 → 예산 → 분당 한도
→ Adapter → 정산·저장. 각 경로(`/v1/analyze`, `query_parse`, `embed_text`)는 결과 종류를 보고 응답과
호출 로그를 정한다.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from ai_gateway.adapters.base import (
    DeadlineExceededError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ai_gateway.budget import Reservation, cost_of
from ai_gateway.deadline import Deadline, cap
from ai_gateway.profile import GatewayProfile

type JsonObject = dict[str, object]
type GateKind = Literal["called", "cached", "budget_exceeded", "failed"]


def cache_key(
    task_type: str, adapter: str, model: str, prompt_version: str | None, content: object
) -> str:
    """입력이 같으면 같은 키(SPEC-016 R-2). 프롬프트 버전에 분류 체계 내용이 들어 있다."""
    payload = {
        "taskType": task_type,
        "adapter": adapter,
        "model": model,
        "promptVersion": prompt_version,
        "content": content,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheStore(Protocol):
    """결과 캐시(`ResultCache`)."""

    async def get(self, key: str) -> JsonObject | None: ...
    async def put(
        self, key: str, task_type: str, source_id: UUID | None, value: JsonObject
    ) -> None: ...
    async def try_lock(self, key: str, ttl_s: float) -> bool | None: ...
    async def release(self, key: str) -> None: ...
    async def wait_for(self, key: str, timeout_s: float) -> JsonObject | None: ...


class BudgetStore(Protocol):
    """하루 예산(`Budget`)."""

    async def reserve(self, caller: str, cost: Decimal) -> Reservation | None: ...
    async def settle(self, reservation: Reservation, actual: Decimal) -> None: ...
    async def release(self, reservation: Reservation) -> None: ...


class Limiter(Protocol):
    """분당 한도(`RateLimiter`)."""

    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None: ...


@dataclass(frozen=True, slots=True)
class GateCall:
    """감쌀 호출 하나. `cache_key`가 None이면 캐시와 동시 호출 합치기를 하지 않는다."""

    caller: str
    task_type: str
    adapter: str
    model: str
    cache_key: str | None
    source_id: UUID | None
    # 예산 예약·분당 한도에 쓰는 예상 토큰(R-10, R-15)
    input_tokens: int
    output_tokens: int
    timeout_s: float
    # 호출자 마감(SPEC-021). 있으면 대기·모델 호출이 남은 시간 안에서 끝난다.
    deadline: Deadline | None = None


@dataclass(frozen=True, slots=True)
class Produced[T]:
    """모델 호출 결과. `store`는 캐시에 저장할 값이고 성공이 아니면 None이다(R-3)."""

    value: T
    input_tokens: int | None
    output_tokens: int | None
    store: JsonObject | None


@dataclass(frozen=True, slots=True)
class GateResult[T]:
    """게이트를 지난 결과.

    - `called`: 모델을 불렀다. `value`가 결과다.
    - `cached`: 저장된 결과다(R-2, R-7). `cached`가 저장한 값이다.
    - `budget_exceeded`: 예산이 모자라 부르지 않았다(R-11).
    - `failed`: 공급자 오류나 분당 한도로 끝났다. `error`를 그대로 처리한다.
    """

    kind: GateKind
    value: T | None = None
    cached: JsonObject | None = None
    error: Exception | None = None
    reserved_usd: Decimal | None = None
    settled_usd: Decimal | None = None


class ModelGate:
    """캐시·예산·분당 한도를 정해진 순서로 적용한다.

    Args:
        profile: 모델 단가를 찾는 프로필.
        cache: 결과 캐시.
        budget: 하루 예산.
        limiter: 분당 한도.
    """

    def __init__(
        self,
        profile: GatewayProfile,
        cache: CacheStore,
        budget: BudgetStore,
        limiter: Limiter,
    ) -> None:
        self._profile = profile
        self._cache = cache
        self._budget = budget
        self._limiter = limiter

    async def run[T](
        self, call: GateCall, invoke: Callable[[], Awaitable[Produced[T]]]
    ) -> GateResult[T]:
        """캐시에 없으면 예산을 예약하고 차례를 얻은 뒤 `invoke`로 모델을 부른다."""
        key = call.cache_key
        if key is not None:
            cached = await self._cache.get(key)
            if cached is not None:
                return _cached(cached)
            locked = await self._cache.try_lock(key, _wait_s(call))
            if locked is False:
                # 다른 요청이 같은 입력을 부르는 중이다. 결과가 오면 쓰고 실패하면 직접 부른다(R-8).
                awaited = await self._cache.wait_for(key, _wait_s(call))
                if awaited is not None:
                    return _cached(awaited)
                if _expired(call):
                    # 기다리는 동안 마감이 지났다. 공급자에 보내지 않았다(SPEC-021 R-1).
                    error = DeadlineExceededError("deadline passed while waiting", sent=False)
                    return GateResult(kind="failed", error=error)
        else:
            locked = None
        try:
            return await self._call(call, invoke)
        finally:
            if key is not None and locked:
                await self._cache.release(key)

    async def _call[T](
        self, call: GateCall, invoke: Callable[[], Awaitable[Produced[T]]]
    ) -> GateResult[T]:
        price = self._profile.price_of(call.adapter, call.model)
        estimate = cost_of(price, call.input_tokens, call.output_tokens)
        reservation = await self._budget.reserve(call.caller, estimate)
        if reservation is None:
            return GateResult(kind="budget_exceeded")
        try:
            await self._acquire(call)
            produced = await _invoke(call, invoke)
        except (ProviderUnavailableError, ProviderFailedError) as e:
            settled = await self._close_failed(reservation, e)
            return GateResult(kind="failed", error=e, reserved_usd=estimate, settled_usd=settled)
        actual = (
            cost_of(price, produced.input_tokens or 0, produced.output_tokens or 0)
            if produced.input_tokens is not None or produced.output_tokens is not None
            else estimate
        )
        await self._budget.settle(reservation, actual)
        if call.cache_key is not None and produced.store is not None:
            await self._cache.put(call.cache_key, call.task_type, call.source_id, produced.store)
        return GateResult(
            kind="called", value=produced.value, reserved_usd=estimate, settled_usd=actual
        )

    async def _acquire(self, call: GateCall) -> None:
        tokens = call.input_tokens + call.output_tokens
        try:
            await self._limiter.acquire(call.adapter, call.model, tokens, _wait_s(call))
        except ProviderUnavailableError as e:
            # 차례를 기다리다 마감이 지났으면 분당 한도가 아니라 마감 초과다. 보내지 않았다(R-1).
            if _expired(call):
                raise DeadlineExceededError("deadline passed while waiting", sent=False) from e
            raise

    async def _close_failed(self, reservation: Reservation, error: Exception) -> Decimal:
        # 보내지 못했으면 되돌리고(R-13), 보냈을 수 있으면 예상 비용으로 남긴다(R-22).
        if isinstance(error, ProviderUnavailableError) and not error.sent:
            await self._budget.release(reservation)
            return Decimal(0)
        await self._budget.settle(reservation, reservation.cost)
        return reservation.cost


def _wait_s(call: GateCall) -> float:
    """대기(잠금·분당 한도)의 제한 시간. 부를 때마다 남은 시간으로 다시 계산한다(SPEC-021 R-1)."""
    return cap(call.deadline, call.timeout_s)


def _expired(call: GateCall) -> bool:
    return call.deadline is not None and call.deadline.expired()


async def _invoke[T](call: GateCall, invoke: Callable[[], Awaitable[Produced[T]]]) -> Produced[T]:
    """외부 모델 호출을 마감 안에서 끝낸다(SPEC-021 R-3).

    Adapter는 `timeout_s`를 시도마다 적용하고 프로필 `max_retries`만큼 다시 시도하므로 호출 전체를
    남은 시간으로 한 번 더 감싼다. 공급자 시간 초과로 끝났는데 그때 마감이 지났으면 원인을 마감으로
    바꾼다. 마감이 남았으면 지금처럼 공급자 오류(503)다.
    """
    deadline = call.deadline
    if deadline is None:
        return await invoke()
    scope = asyncio.timeout(deadline.remaining_s())
    try:
        async with scope:
            return await invoke()
    except TimeoutError as e:
        if not scope.expired():
            raise
        raise DeadlineExceededError("deadline passed while calling the model", sent=True) from e
    except ProviderUnavailableError as e:
        if isinstance(e, DeadlineExceededError) or not deadline.expired():
            raise
        raise DeadlineExceededError("deadline passed while calling the model", sent=e.sent) from e


def _cached[T](value: JsonObject) -> GateResult[T]:
    # 캐시 적중은 비용 0이다(R-14).
    return GateResult(kind="cached", cached=value, reserved_usd=Decimal(0), settled_usd=Decimal(0))
