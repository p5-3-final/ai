import asyncio
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal

import pytest
from ai_gateway.adapters.base import (
    DeadlineExceededError,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ai_gateway.deadline import Deadline
from ai_gateway.gate import GateCall, ModelGate, Produced
from ai_gateway.rate_limit import RateLimitedError
from gateway_support import MemoryBudget, MemoryCache, profile

CALL = GateCall(
    caller="event-analyzer",
    task_type="event_judge",
    adapter="fake",
    model="fake-vlm",
    cache_key="k1",
    source_id=None,
    input_tokens=1000,
    output_tokens=500,
    timeout_s=5,
)
# 100만 토큰당 입력 2달러, 출력 8달러: 예상 비용 = 1000*2/1e6 + 500*8/1e6 = 0.006
PRICED = profile(
    extra={
        "prices": {"fake": {"fake-vlm": {"input_per_million_usd": 2, "output_per_million_usd": 8}}}
    }
)
ESTIMATE = Decimal("0.006")
STORED: dict[str, object] = {"output": {"eventType": "fall"}}


@dataclass
class Invoker:
    """호출 횟수를 세고 정해 둔 결과를 돌려주는 모델 호출."""

    result: Produced[str] | Exception = field(
        default_factory=lambda: Produced("fresh", 100, 50, STORED)
    )
    calls: int = 0

    async def __call__(self) -> Produced[str]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@dataclass
class FakeLimiter:
    error: Exception | None = None
    calls: list[int] = field(default_factory=list)

    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None:
        self.calls.append(tokens)
        if self.error is not None:
            raise self.error


def _gate(
    cache: MemoryCache | None = None,
    budget: MemoryBudget | None = None,
    limiter: FakeLimiter | None = None,
) -> ModelGate:
    return ModelGate(
        PRICED, cache or MemoryCache(), budget or MemoryBudget(), limiter or FakeLimiter()
    )


async def test_spec016_r1_successful_call_is_stored() -> None:
    cache, invoke = MemoryCache(), Invoker()

    result = await _gate(cache).run(CALL, invoke)

    assert (result.kind, result.value, invoke.calls) == ("called", "fresh", 1)
    assert cache.values["k1"] == STORED


async def test_spec016_r2_cached_result_skips_model() -> None:
    cache, invoke = MemoryCache(values={"k1": STORED}), Invoker()
    budget = MemoryBudget()

    result = await _gate(cache, budget).run(CALL, invoke)

    assert (result.kind, result.cached, invoke.calls) == ("cached", STORED, 0)
    # 캐시 적중은 비용 0이다(R-14).
    assert (result.reserved_usd, result.settled_usd) == (Decimal(0), Decimal(0))
    assert budget.spent == {}


async def test_spec016_r3_unsuccessful_result_is_not_stored() -> None:
    cache = MemoryCache()

    await _gate(cache).run(CALL, Invoker(Produced("unknown", 100, 50, None)))

    assert cache.values == {}


async def test_spec016_r1_call_without_key_skips_cache() -> None:
    cache = MemoryCache(values={"k1": STORED})
    invoke = Invoker()

    result = await _gate(cache).run(replace(CALL, cache_key=None), invoke)

    assert (result.kind, invoke.calls) == ("called", 1)


async def test_spec016_r7_waits_for_other_pod_result() -> None:
    cache = MemoryCache(locks={"k1"}, awaited=STORED)
    invoke = Invoker()

    result = await _gate(cache).run(CALL, invoke)

    assert (result.kind, result.cached, invoke.calls) == ("cached", STORED, 0)


async def test_spec016_r8_calls_itself_when_other_pod_failed() -> None:
    cache = MemoryCache(locks={"k1"}, awaited=None)
    invoke = Invoker()

    result = await _gate(cache).run(CALL, invoke)

    assert (result.kind, invoke.calls) == ("called", 1)


async def test_spec016_r7_lock_is_released_after_call() -> None:
    cache = MemoryCache()

    await _gate(cache).run(CALL, Invoker())

    assert cache.locks == set()


async def test_spec016_r10_reserves_estimate_and_r12_settles_actual() -> None:
    budget = MemoryBudget()

    result = await _gate(budget=budget).run(CALL, Invoker())

    # 실제 100·50토큰: 100*2/1e6 + 50*8/1e6 = 0.0006
    assert result.reserved_usd == ESTIMATE
    assert result.settled_usd == Decimal("0.0006")
    assert budget.spent == {"event-analyzer": Decimal("0.0006"), "*": Decimal("0.0006")}


async def test_spec016_r12_settles_estimate_without_token_counts() -> None:
    budget = MemoryBudget()

    result = await _gate(budget=budget).run(CALL, Invoker(Produced("x", None, None, None)))

    assert result.settled_usd == ESTIMATE


async def test_spec016_r11_budget_exceeded_skips_model() -> None:
    cache, budget, invoke = MemoryCache(), MemoryBudget(limits={"*": Decimal("0.001")}), Invoker()

    result = await _gate(cache, budget).run(CALL, invoke)

    assert (result.kind, invoke.calls) == ("budget_exceeded", 0)
    assert result.reserved_usd is None
    assert cache.locks == set()


async def test_spec016_r13_unsent_call_releases_reservation() -> None:
    budget = MemoryBudget()
    error = ProviderUnavailableError("down")
    error.sent = False

    result = await _gate(budget=budget).run(CALL, Invoker(error))

    assert (result.kind, result.error) == ("failed", error)
    assert result.settled_usd == Decimal(0)
    assert budget.spent["*"] == Decimal(0)


@pytest.mark.parametrize(
    "error", [ProviderUnavailableError("timeout"), ProviderFailedError("rejected")]
)
async def test_spec016_r22_sent_failure_settles_estimate(error: Exception) -> None:
    budget = MemoryBudget()

    result = await _gate(budget=budget).run(CALL, Invoker(error))

    assert (result.kind, result.error) == ("failed", error)
    assert result.settled_usd == ESTIMATE
    assert budget.spent["*"] == ESTIMATE


async def test_spec016_r16_no_rate_limit_turn_fails_without_calling() -> None:
    budget, invoke = MemoryBudget(), Invoker()
    limiter = FakeLimiter(error=RateLimitedError("busy"))

    result = await _gate(budget=budget, limiter=limiter).run(CALL, invoke)

    assert result.kind == "failed"
    assert isinstance(result.error, RateLimitedError)
    assert invoke.calls == 0
    assert budget.spent["*"] == Decimal(0)


async def test_spec016_r15_rate_limit_counts_estimated_tokens() -> None:
    limiter = FakeLimiter()

    await _gate(limiter=limiter).run(CALL, Invoker())

    assert limiter.calls == [1500]


def _deadline_in(seconds: float) -> Deadline:
    return Deadline(int((time.time() + seconds) * 1000))


@dataclass
class SlowLimiter:
    """차례를 주지 않고 받은 제한 시간만큼 기다린 뒤 분당 한도 오류를 던진다."""

    timeouts: list[float] = field(default_factory=list)

    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None:
        self.timeouts.append(timeout_s)
        await asyncio.sleep(timeout_s)
        raise RateLimitedError("busy")


async def test_spec021_r1_waiting_for_same_input_stops_at_deadline() -> None:
    """[SPEC-021 R-1] 같은 입력의 잠금 대기가 남은 시간으로 끊기면 마감 초과다(보내지 않음)."""
    cache = MemoryCache(locks={"k1"}, wait_s=10, awaited=STORED)
    budget, invoke = MemoryBudget(), Invoker()
    started = time.perf_counter()

    result = await _gate(cache, budget).run(replace(CALL, deadline=_deadline_in(0.2)), invoke)

    assert time.perf_counter() - started < 2
    assert result.kind == "failed"
    assert isinstance(result.error, DeadlineExceededError)
    assert result.error.sent is False
    assert (invoke.calls, budget.reserves) == (0, 0)


async def test_spec021_r1_rate_limit_wait_uses_remaining_time_and_releases_budget() -> None:
    """[SPEC-021 R-1] 분당 한도 대기도 남은 시간까지만이고, 끊기면 예약을 되돌린다."""
    budget, limiter, invoke = MemoryBudget(), SlowLimiter(), Invoker()

    result = await ModelGate(PRICED, MemoryCache(), budget, limiter).run(
        replace(CALL, deadline=_deadline_in(0.3)), invoke
    )

    [waited] = limiter.timeouts
    assert 0 < waited <= 0.3
    assert isinstance(result.error, DeadlineExceededError)
    assert result.error.sent is False
    assert invoke.calls == 0
    assert budget.spent["event-analyzer"] == 0


async def test_spec021_r3_model_call_past_deadline_is_settled_as_sent() -> None:
    """[SPEC-021 R-3] 모델 호출이 남은 시간 안에 끝나지 않으면 마감 초과다. 예약액으로 정산한다."""
    budget = MemoryBudget()

    async def slow() -> Produced[str]:
        await asyncio.sleep(10)
        return Produced("late", 1, 1, None)

    result = await _gate(budget=budget).run(replace(CALL, deadline=_deadline_in(0.2)), slow)

    assert isinstance(result.error, DeadlineExceededError)
    assert result.error.sent is True
    assert (result.reserved_usd, result.settled_usd) == (ESTIMATE, ESTIMATE)
    assert budget.spent["event-analyzer"] == ESTIMATE


async def test_spec021_r3_provider_timeout_before_deadline_stays_503() -> None:
    """[SPEC-021 R-3] 마감이 남았는데 프로필 제한 시간이 먼저 끝났으면 지금처럼 공급자 오류다."""
    invoke = Invoker(result=ProviderUnavailableError("timeout"))

    result = await _gate().run(replace(CALL, deadline=_deadline_in(60)), invoke)

    assert type(result.error) is ProviderUnavailableError
