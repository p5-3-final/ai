from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest
from ai_gateway.profile import GatewayProfile
from ai_gateway.rate_limit import RateLimitedError, RateLimiter
from gateway_support import profile

DOWN = "redis://127.0.0.1:1/0"


class FakeTime:
    """분 경계 1초 뒤에서 시작하는 시계. sleep은 기다리지 않고 시계만 앞으로 돌린다."""

    def __init__(self) -> None:
        # Redis 키가 테스트끼리 겹치지 않게 시작 분을 무작위로 둔다.
        self.now = (10**6 + uuid4().int % 10**7) * 60 + 1.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limited(**limit: int) -> GatewayProfile:
    return profile(extra={"rate_limits": {"fake": {"fake-vlm": limit}}})


def _limiter(gp: GatewayProfile, t: FakeTime, url: str | None = None) -> RateLimiter:
    sleep: Callable[[float], Awaitable[None]] = t.sleep
    return RateLimiter(gp, url, clock=t.clock, sleep=sleep)


async def test_spec016_r15_requests_within_limit_pass_at_once() -> None:
    t = FakeTime()
    limiter = _limiter(_limited(rpm=2), t)

    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)
    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)

    assert t.slept == []


async def test_spec016_r15_request_over_limit_waits_for_next_minute() -> None:
    t = FakeTime()
    limiter = _limiter(_limited(rpm=1), t)
    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=120)

    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=120)

    assert sum(t.slept) == pytest.approx(59.0)


async def test_spec016_r15_token_limit_counts_tokens() -> None:
    t = FakeTime()
    limiter = _limiter(_limited(tpm=100), t)
    await limiter.acquire("fake", "fake-vlm", 60, timeout_s=120)

    await limiter.acquire("fake", "fake-vlm", 60, timeout_s=120)

    assert sum(t.slept) > 0


async def test_spec016_r16_no_turn_within_timeout_fails() -> None:
    t = FakeTime()
    limiter = _limiter(_limited(rpm=1), t)
    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)

    with pytest.raises(RateLimitedError) as caught:
        await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)

    assert caught.value.code == "AI_PROVIDER_UNAVAILABLE"
    assert caught.value.log_code == "AI_RATE_LIMITED"
    assert sum(t.slept) <= 5


async def test_spec016_r15_model_without_limit_never_waits() -> None:
    t = FakeTime()
    limiter = _limiter(profile(), t)

    for _ in range(100):
        await limiter.acquire("fake", "fake-vlm", 10**6, timeout_s=0.1)

    assert t.slept == []


@pytest.mark.integration
async def test_spec016_r15_limit_is_shared_by_pods(redis_url: str) -> None:
    t = FakeTime()
    first, second = (_limiter(_limited(rpm=2, pod_rpm=2), t, redis_url) for _ in range(2))
    await first.acquire("fake", "fake-vlm", 10, timeout_s=5)
    await second.acquire("fake", "fake-vlm", 10, timeout_s=5)

    with pytest.raises(RateLimitedError):
        await first.acquire("fake", "fake-vlm", 10, timeout_s=5)
    await first.close()
    await second.close()


async def test_spec016_r17_redis_down_uses_pod_limit() -> None:
    t = FakeTime()
    limiter = _limiter(_limited(rpm=10, pod_rpm=1), t, DOWN)
    await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)

    with pytest.raises(RateLimitedError):
        await limiter.acquire("fake", "fake-vlm", 10, timeout_s=5)
    await limiter.close()
