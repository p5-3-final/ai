"""실시간(STREAM) 재판정 간격 상태(SPEC-023 R-18).

실시간 창은 20초라 메시지 안 구간 규칙만으로는 같은 작업자를 창마다 다시 판정한다. 같은
(sourceId, trackId, 유형)의 마지막 `confirmed` 판정을 Redis에 두고 창을 넘어 잇는다. 창이 카메라당
순서대로 오므로 결정적이다. Redis가 없거나 끊기면 판정한다(비용이 늘 뿐 결과는 맞다).
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from vlm_contracts.events import VLMAnalysis

from event_analyzer.carry import TrackKey

logger = logging.getLogger(__name__)

# Redis가 느리면 기다리지 않고 판정한다. 로컬 왕복은 1ms 안쪽이다.
_REDIS_TIMEOUT_S = 0.2
# Redis 장애 경고를 메시지마다 남기지 않는다.
_WARN_EVERY_S = 30.0


@dataclass(frozen=True, slots=True)
class Prior:
    """같은 (트랙, 유형)의 마지막 `confirmed` 판정."""

    start_ms: int
    event_id: UUID
    analysis: VLMAnalysis


class RejudgeState(Protocol):
    """재판정 간격 상태 저장소."""

    async def read(self, source_id: UUID, keys: Sequence[TrackKey]) -> dict[TrackKey, Prior]:
        """키마다 마지막 `confirmed` 판정. 없거나 읽지 못한 키는 빠진다."""
        ...

    async def write(self, source_id: UUID, key: TrackKey, prior: Prior, ttl_s: float) -> None:
        """마지막 `confirmed` 판정을 바꾼다. 쓰지 못해도 예외를 던지지 않는다."""
        ...


class NoRejudgeState:
    """Redis를 설정하지 않았을 때. 창마다 판정한다."""

    async def read(self, source_id: UUID, keys: Sequence[TrackKey]) -> dict[TrackKey, Prior]:
        return {}

    async def write(self, source_id: UUID, key: TrackKey, prior: Prior, ttl_s: float) -> None:
        return None


class _Stored(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    start_ms: int
    event_id: UUID
    analysis: VLMAnalysis


def redis_key(source_id: UUID, key: TrackKey) -> str:
    """아키텍처 v5.1 6.2절의 키 `rejudge:<sourceId>:<trackId>:<eventType>`."""
    track, event_type = key
    return f"rejudge:{source_id}:{track}:{event_type}"


class RedisRejudgeState:
    """Redis 구현. 이벤트 루프 하나(워커)에서만 쓴다. 연결은 처음 호출할 때 열린다."""

    def __init__(self, redis_url: str) -> None:
        self._redis = Redis.from_url(
            redis_url, socket_timeout=_REDIS_TIMEOUT_S, socket_connect_timeout=_REDIS_TIMEOUT_S
        )
        self._warned_at = -_WARN_EVERY_S

    async def read(self, source_id: UUID, keys: Sequence[TrackKey]) -> dict[TrackKey, Prior]:
        if not keys:
            return {}
        try:
            values = await self._redis.mget([redis_key(source_id, k) for k in keys])
        except RedisError as e:
            self._warn("rejudge state read failed", e)
            return {}
        priors: dict[TrackKey, Prior] = {}
        for key, raw in zip(keys, values, strict=True):
            if raw is None:
                continue
            try:
                stored = _Stored.model_validate_json(raw)
            except ValidationError:
                # 다른 버전이 쓴 값처럼 읽지 못하면 없는 것으로 보고 판정한다.
                logger.warning("unreadable rejudge state", extra={"key": redis_key(source_id, key)})
                continue
            priors[key] = Prior(stored.start_ms, stored.event_id, stored.analysis)
        return priors

    async def write(self, source_id: UUID, key: TrackKey, prior: Prior, ttl_s: float) -> None:
        value = _Stored(start_ms=prior.start_ms, event_id=prior.event_id, analysis=prior.analysis)
        try:
            await self._redis.set(
                redis_key(source_id, key),
                value.model_dump_json(by_alias=True),
                px=int(ttl_s * 1000),
            )
        except RedisError as e:
            self._warn("rejudge state write failed", e)

    async def aclose(self) -> None:
        await self._redis.aclose()

    def _warn(self, message: str, error: RedisError) -> None:
        now = time.monotonic()
        if now - self._warned_at >= _WARN_EVERY_S:
            self._warned_at = now
            logger.warning(message, extra={"error": type(error).__name__})
