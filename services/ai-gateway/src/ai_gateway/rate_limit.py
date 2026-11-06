"""외부 모델 분당 호출 한도(SPEC-016 R-15~R-17).

공급자의 분당 요청·토큰 한도를 모든 Pod가 함께 지키려고 Redis에 분 단위 창(window)마다 개수를
센다. 토큰 버킷보다 단순하고 "분당 N 이하"를 그대로 지킨다. Redis가 없거나 끊기면 Pod 메모리에서
Pod별 한도로 센다(R-17).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from ai_gateway.adapters.base import ProviderUnavailableError
from ai_gateway.profile import GatewayProfile, RateLimitProfile

logger = logging.getLogger(__name__)

_WINDOW_S = 60
# 창 키는 다음 창이 끝날 때까지만 남긴다.
_KEY_TTL_S = 2 * _WINDOW_S
_KEY_PREFIX = "aigw:rate:"
_REDIS_TIMEOUT_S = 0.2
_WARN_EVERY_S = 30.0


class RateLimitedError(ProviderUnavailableError):
    """제한 시간 안에 호출 차례를 얻지 못했다.

    응답은 공급자 일시 오류와 같고(`AI_PROVIDER_UNAVAILABLE`), 호출 로그에는 `AI_RATE_LIMITED`를
    남긴다(R-16). 공급자에 보내지 않았으므로 예약을 되돌린다(R-13).
    """

    log_code = "AI_RATE_LIMITED"
    sent = False


@dataclass(frozen=True, slots=True)
class _Limits:
    requests: int | None
    tokens: int | None


class RateLimiter:
    """Adapter·모델별 분당 한도.

    Args:
        profile: `[rate_limits]`가 있는 프로필.
        redis_url: Pod 사이에 개수를 나누는 Redis. None이면 Pod별 한도만 쓴다.
        clock: 창을 정하는 시계(초). 테스트가 바꾼다.
        sleep: 다음 창까지 기다리는 함수. 테스트가 바꾼다.
    """

    def __init__(
        self,
        profile: GatewayProfile,
        redis_url: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._profile = profile
        self._clock = clock
        self._sleep = sleep
        self._redis = (
            Redis.from_url(
                redis_url,
                socket_timeout=_REDIS_TIMEOUT_S,
                socket_connect_timeout=_REDIS_TIMEOUT_S,
            )
            if redis_url
            else None
        )
        # (Adapter, 모델, 창) → [요청 수, 토큰 수]. Redis를 못 쓸 때만 쓴다.
        self._local: dict[tuple[str, str, int], list[int]] = {}
        self._warned_at = -_WARN_EVERY_S

    async def acquire(self, adapter: str, model: str, tokens: int, timeout_s: float) -> None:
        """호출 차례를 얻는다. 이번 분 한도를 넘으면 다음 분까지 기다린다(R-15).

        Raises:
            RateLimitedError: `timeout_s` 안에 차례를 얻지 못했을 때(R-16).
        """
        limit = self._profile.rate_limit_of(adapter, model)
        if limit is None:
            return
        deadline = self._clock() + timeout_s
        while True:
            now = self._clock()
            window = int(now // _WINDOW_S)
            if await self._take(adapter, model, window, tokens, limit):
                return
            wait = (window + 1) * _WINDOW_S - now
            if now + wait > deadline:
                raise RateLimitedError(f"rate limit of {adapter}/{model} reached")
            await self._sleep(wait)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def _take(
        self, adapter: str, model: str, window: int, tokens: int, limit: RateLimitProfile
    ) -> bool:
        if self._redis is not None:
            taken = await self._take_shared(self._redis, adapter, model, window, tokens, limit)
            if taken is not None:
                return taken
        return self._take_local(
            adapter, model, window, tokens, _Limits(limit.pod_rpm, limit.pod_tpm)
        )

    async def _take_shared(
        self,
        redis: Redis,
        adapter: str,
        model: str,
        window: int,
        tokens: int,
        limit: RateLimitProfile,
    ) -> bool | None:
        """Redis 창에서 센다. 한도를 넘으면 더한 만큼 되돌린다. Redis가 끊기면 None이다."""
        base = f"{_KEY_PREFIX}{adapter}/{model}:{window}"
        cost = _capped(tokens, limit.tpm)
        try:
            async with redis.pipeline(transaction=True) as pipe:
                pipe.incr(f"{base}:req")
                pipe.incrby(f"{base}:tok", cost)
                pipe.expire(f"{base}:req", _KEY_TTL_S)
                pipe.expire(f"{base}:tok", _KEY_TTL_S)
                requests, used, *_ = await pipe.execute()
            if _within(requests, limit.rpm) and _within(used, limit.tpm):
                return True
            async with redis.pipeline(transaction=True) as pipe:
                pipe.decr(f"{base}:req")
                pipe.decrby(f"{base}:tok", cost)
                await pipe.execute()
            return False
        except RedisError:
            now = time.monotonic()
            if now - self._warned_at >= _WARN_EVERY_S:
                self._warned_at = now
                logger.warning("rate limit store unavailable, using per-pod limits")
            return None

    def _take_local(
        self, adapter: str, model: str, window: int, tokens: int, limits: _Limits
    ) -> bool:
        # 지난 창은 지워 메모리가 늘지 않게 한다.
        for key in [k for k in self._local if k[2] < window]:
            del self._local[key]
        counts = self._local.setdefault((adapter, model, window), [0, 0])
        cost = _capped(tokens, limits.tokens)
        if not (
            _within(counts[0] + 1, limits.requests) and _within(counts[1] + cost, limits.tokens)
        ):
            return False
        counts[0] += 1
        counts[1] += cost
        return True


def _within(value: int, limit: int | None) -> bool:
    return limit is None or value <= limit


def _capped(tokens: int, limit: int | None) -> int:
    # 한 번에 분당 토큰 한도보다 큰 요청도 빈 창에서는 보낼 수 있게 한도로 자른다.
    return min(tokens, limit) if limit is not None else tokens
