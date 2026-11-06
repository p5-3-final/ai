"""외부 모델 호출 결과 캐시(SPEC-016 R-1~R-9, R-17).

영속 저장은 DB(`ai.ai_result_cache`)이고 Redis는 앞단 캐시와 동시 호출 잠금에만 쓴다. Redis가
없거나 끊기면 DB만으로 동작한다. 검색이 Redis 장애와 상관없이 이어져야 하기 때문이다
(docs overview "가용성").
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from vlm_common.errors import VlmError

from ai_gateway.db.tables import result_cache
from ai_gateway.profile import CacheProfile

type JsonObject = dict[str, object]

logger = logging.getLogger(__name__)

# 다른 서비스와 같은 Redis를 써도 겹치지 않게 키 앞에 붙인다.
_VALUE_PREFIX = "aigw:cache:"
_LOCK_PREFIX = "aigw:flight:"
# Redis가 느리면 기다리지 않고 DB로 넘어간다. 로컬 왕복은 1ms 안쪽이다.
_REDIS_TIMEOUT_S = 0.2
# 기다리는 쪽이 결과를 다시 확인하는 간격
_POLL_S = 0.05
# Redis 장애 경고를 요청마다 남기지 않는다.
_WARN_EVERY_S = 30.0


class FrontUnavailableError(VlmError):
    """앞단 캐시를 지우지 못했다. 영상 삭제 처리는 다시 시도해야 한다(R-9)."""

    code = "AI_CACHE_FRONT_UNAVAILABLE"


class ResultCache:
    """결과 캐시와 동시 호출 잠금.

    Args:
        engine: ai_db 엔진.
        settings: 보관 기간과 앞단 TTL.
        redis_url: 앞단 캐시 주소. None이면 DB만 쓴다.
        clock: 보관 기간을 재는 시계. 테스트가 바꾼다.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        settings: CacheProfile,
        redis_url: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._clock = clock
        self._redis = (
            Redis.from_url(
                redis_url,
                socket_timeout=_REDIS_TIMEOUT_S,
                socket_connect_timeout=_REDIS_TIMEOUT_S,
            )
            if redis_url
            else None
        )
        self._warned_at = -_WARN_EVERY_S

    async def get(self, key: str) -> JsonObject | None:
        """앞단 → DB 순서로 찾는다. DB에서 찾으면 앞단을 채운다(R-2, R-6)."""
        raw = await self._front(lambda r: r.get(_VALUE_PREFIX + key))
        if isinstance(raw, bytes | str):
            return _load(raw)
        cutoff = self._clock() - timedelta(days=self._settings.retention_days)
        async with self._engine.connect() as conn:
            value = await conn.scalar(
                select(result_cache.c.value).where(
                    result_cache.c.cache_key == key, result_cache.c.created_at >= cutoff
                )
            )
        if not isinstance(value, dict):
            return None
        await self._set_front(key, value)
        return value

    async def put(
        self, key: str, task_type: str, source_id: UUID | None, value: JsonObject
    ) -> None:
        """성공한 결과를 저장한다(R-1). 같은 키면 새 결과와 시각으로 바꾼다."""
        now = self._clock()
        statement = insert(result_cache).values(
            cache_key=key, task_type=task_type, source_id=source_id, value=value, created_at=now
        )
        async with self._engine.begin() as conn:
            await conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[result_cache.c.cache_key],
                    set_={"value": statement.excluded.value, "created_at": now},
                )
            )
        await self._set_front(key, value)

    async def delete_source(self, source_id: UUID) -> int:
        """영상의 결과를 DB와 앞단에서 지우고 지운 수를 돌려준다(R-9).

        앞단을 먼저 지운다. 앞단을 못 지우고 실패하면 DB 행이 남아 있어, 다시 처리할 때
        지울 키를 다시 찾을 수 있다.

        Raises:
            FrontUnavailableError: 앞단이 설정됐는데 지우지 못했을 때.
        """
        async with self._engine.connect() as conn:
            keys = list(
                await conn.scalars(
                    select(result_cache.c.cache_key).where(result_cache.c.source_id == source_id)
                )
            )
        if keys and self._redis is not None:
            try:
                await self._redis.delete(*(_VALUE_PREFIX + k for k in keys))
            except RedisError as e:
                raise FrontUnavailableError("cannot delete cached results from the front") from e
        async with self._engine.begin() as conn:
            await conn.execute(delete(result_cache).where(result_cache.c.source_id == source_id))
        return len(keys)

    async def try_lock(self, key: str, ttl_s: float) -> bool | None:
        """같은 키의 모델 호출을 한 요청만 하게 잠근다(R-7).

        Returns:
            잠갔으면 True, 다른 요청이 잠갔으면 False, 앞단이 없거나 끊겼으면 None(R-17).
        """
        if self._redis is None:
            return None
        result = await self._front(
            lambda r: r.set(_LOCK_PREFIX + key, b"1", nx=True, px=max(1, int(ttl_s * 1000)))
        )
        if result is _UNAVAILABLE:
            return None
        return bool(result)

    async def release(self, key: str) -> None:
        await self._front(lambda r: r.delete(_LOCK_PREFIX + key))

    async def wait_for(self, key: str, timeout_s: float) -> JsonObject | None:
        """다른 요청이 잠근 호출의 결과를 기다린다.

        잠금이 풀렸는데 결과가 없거나(실패) 시간이 지나면 None이다(R-8).
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = await self.get(key)
            if value is not None:
                return value
            held = await self._front(lambda r: r.exists(_LOCK_PREFIX + key))
            if held is _UNAVAILABLE or not held:
                # 잠금이 풀린 사이에 저장했을 수 있어 한 번 더 본다.
                return await self.get(key)
            await asyncio.sleep(_POLL_S)
        return None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def _set_front(self, key: str, value: JsonObject) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._front(lambda r: r.set(_VALUE_PREFIX + key, data, ex=self._settings.front_ttl_s))

    async def _front(self, operation: Callable[[Redis], Awaitable[object]]) -> object:
        """앞단 명령을 실행한다. 앞단이 없거나 끊겼으면 `_UNAVAILABLE`을 돌려준다(R-17)."""
        if self._redis is None:
            return _UNAVAILABLE
        try:
            return await operation(self._redis)
        except RedisError:
            now = time.monotonic()
            if now - self._warned_at >= _WARN_EVERY_S:
                self._warned_at = now
                logger.warning("result cache front unavailable, using database only")
            return _UNAVAILABLE


_UNAVAILABLE = object()


def _load(raw: bytes | str) -> JsonObject | None:
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else None
