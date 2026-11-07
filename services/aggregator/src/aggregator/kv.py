"""aggregator가 쓰는 Redis 명령의 얇은 인터페이스.

대기 집합(R-25, R-29), 발행 기록(R-27), 실시간 세션 상태(R-30), 구역 캐시(R-33), 재판정 계수(R-31)가
모두 이 다섯 명령만 쓴다. 서비스 코드는 `KeyValueStore`만 보고, Redis 클라이언트는 `RedisStore`
한 곳에서만 쓴다. 단위 테스트는 `MemoryStore`를 쓴다.
"""

from collections.abc import Callable
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import RedisError
from vlm_common.errors import VlmError

# 명령 하나가 이보다 오래 걸리면 Redis가 일시적으로 응답하지 않는 것으로 보고
# 재시도 토픽으로 보낸다.
_TIMEOUT_S = 5.0


class StoreUnavailableError(VlmError):
    """Redis가 응답하지 않거나 설정되지 않았다. 메시지를 다시 시도한다(R-22)."""

    code = "AGGREGATOR_STORE_UNAVAILABLE"


class KeyValueStore(Protocol):
    """키·해시 저장소. 값은 문자열(JSON)이다."""

    async def hset(self, key: str, field: str, value: str, ttl_s: int) -> None:
        """해시 필드를 쓰고 키의 TTL을 `ttl_s`로 다시 잡는다. 같은 필드면 덮어써 멱등이다."""
        ...

    async def hgetall(self, key: str) -> dict[str, str]:
        """해시 전체. 없으면 빈 dict다."""
        ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_s: int) -> None: ...

    async def incr(self, key: str, ttl_s: int) -> int:
        """1 올린 값. 처음 만들 때 TTL을 잡는다."""
        ...


class RedisStore:
    """Redis 구현(`redis://` URL)."""

    def __init__(self, url: str) -> None:
        self._redis: Redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=_TIMEOUT_S,
            socket_connect_timeout=_TIMEOUT_S,
        )

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def hset(self, key: str, field: str, value: str, ttl_s: int) -> None:
        await self._pipe(lambda p: (p.hset(key, field, value), p.expire(key, ttl_s)))

    async def hgetall(self, key: str) -> dict[str, str]:
        [found] = await self._pipe(lambda p: (p.hgetall(key),))
        return cast(dict[str, str], found)

    async def get(self, key: str) -> str | None:
        [found] = await self._pipe(lambda p: (p.get(key),))
        return cast(str | None, found)

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        await self._pipe(lambda p: (p.set(key, value, ex=ttl_s),))

    async def incr(self, key: str, ttl_s: int) -> int:
        # 계수는 job·세션 동안만 의미가 있다. nx로 처음 만들 때만 TTL을 잡아
        # 계수가 끝없이 남지 않게 한다.
        count, _ = await self._pipe(lambda p: (p.incr(key), p.expire(key, ttl_s, nx=True)))
        return int(cast(int, count))

    async def _pipe(self, build: Callable[[Pipeline], object]) -> list[object]:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                build(pipe)
                return cast(list[object], await pipe.execute())
        except RedisError as e:
            raise StoreUnavailableError(f"redis command failed: {type(e).__name__}") from e


class MemoryStore:
    """테스트·Redis 없는 로컬 실행용 메모리 구현. TTL은 지키지 않는다."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}

    async def hset(self, key: str, field: str, value: str, ttl_s: int) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl_s: int) -> None:
        self.values[key] = value

    async def incr(self, key: str, ttl_s: int) -> int:
        count = int(self.values.get(key, "0")) + 1
        self.values[key] = str(count)
        return count
