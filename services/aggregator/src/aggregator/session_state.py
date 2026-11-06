"""실시간 세션 상태 저장소(R-30).

현재 상태는 Redis `agg:state:<sourceId>`(TTL 48시간)에 두고, 30창마다 S3
`sources/<sourceId>/state/<sequence>.json`에 스냅샷을 남긴다. Redis를 잃으면 S3 최신 스냅샷, 그것도
없으면 직전 세션(`previousSourceId`)의 마지막 상태, 그것도 없으면 빈 상태로 시작한다.
최신 스냅샷을 목록 조회 없이 찾도록 `state/latest.json`에 같은 내용을 함께 쓴다.
"""

import asyncio
from uuid import UUID

from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from aggregator.fold import SessionState
from aggregator.kv import KeyValueStore

STATE_TTL_S = 48 * 3600
"""세션 상태·처리한 창 기록 TTL. 실시간 세션 최대 길이(24시간)의 두 배다."""
_JSON = "application/json"


class SessionStateStore:
    """세션 상태와 처리한 창 기록."""

    def __init__(self, store: KeyValueStore, storage: ObjectStorage) -> None:
        self._store = store
        self._storage = storage

    async def load(self, source_id: UUID, previous_source_id: UUID | None) -> SessionState:
        """이 세션의 현재 상태. 없으면 직전 세션의 마지막 상태를 이어받는다(R-30, R-5)."""
        current = await self._find(source_id)
        if current is not None:
            return current
        if previous_source_id is not None:
            previous = await self._find(previous_source_id)
            if previous is not None:
                return previous.carried_into(source_id)
        return SessionState(source_id=source_id)

    async def save(self, source_id: UUID, state: SessionState, *, snapshot: bool) -> None:
        """Redis에 쓰고, `snapshot`이면 S3에도 남긴다."""
        data = state.model_dump_json()
        await self._store.set(_state_key(source_id), data, STATE_TTL_S)
        if snapshot:
            await asyncio.to_thread(self._snapshot, source_id, state.last_sequence, data.encode())

    async def mark_window(self, source_id: UUID, chunk_id: UUID, sequence: int) -> None:
        """창을 처리했다고 기록한다. 같은 창이 다시 오면 다시 계산하지 않는다."""
        await self._store.set(_window_key(source_id, chunk_id), str(sequence), STATE_TTL_S)

    async def window_sequence(self, source_id: UUID, chunk_id: UUID) -> int | None:
        found = await self._store.get(_window_key(source_id, chunk_id))
        return int(found) if found is not None else None

    async def _find(self, source_id: UUID) -> SessionState | None:
        cached = await self._store.get(_state_key(source_id))
        if cached is not None:
            return SessionState.model_validate_json(cached)
        try:
            raw = await asyncio.to_thread(
                self._storage.get_bytes, f"sources/{source_id}/state/latest.json"
            )
        except ObjectNotFoundError:
            return None
        return SessionState.model_validate_json(raw)

    def _snapshot(self, source_id: UUID, sequence: int, data: bytes) -> None:
        self._storage.put_bytes(f"sources/{source_id}/state/{sequence}.json", data, _JSON)
        self._storage.put_bytes(f"sources/{source_id}/state/latest.json", data, _JSON)


def _state_key(source_id: UUID) -> str:
    return f"agg:state:{source_id}"


def _window_key(source_id: UUID, chunk_id: UUID) -> str:
    return f"agg:window:{source_id}:{chunk_id}"
