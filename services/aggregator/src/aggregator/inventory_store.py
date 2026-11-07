"""청크 재고 파일 저장·읽기(SPEC-028 R-1, R-2).

`chunk.manifest`는 청크 실행마다 온다. 재시도로 같은 청크의 다른 실행이 와도 처음 쓴 실행의 재고가
이긴다(`put_bytes_if_absent`). 결과는 저장된 파일로만 계산하므로 다시 처리해도 같은 재고를 본다.
"""

import asyncio
from uuid import UUID

from vlm_contracts.aggregates import ChunkInventory, inventory_key, inventory_prefix
from vlm_contracts.events import ChunkManifest
from vlm_messaging.worker import MessageContext
from vlm_storage.storage import ObjectStorage

_JSON = "application/json"


def inventory_of(message: ChunkManifest) -> ChunkInventory:
    """manifest에서 재고 파일 내용만 옮긴다."""
    return ChunkInventory(
        source_id=message.source_id,
        job_id=message.job_id,
        chunk_id=message.chunk_id,
        execution_id=message.execution_id,
        owned_range=message.owned_range,
        detector_profile=message.detector.detector_profile,
        static_objects=list(message.static_objects),
        reference_frames=list(message.reference_frames),
    )


class InventoryStore:
    """청크 재고 파일(`sources/<sourceId>/jobs/<jobId>/aggregate/inventory/<chunkId>.json`)."""

    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    async def save(self, message: ChunkManifest, ctx: MessageContext) -> bool:
        """파일이 없을 때만 쓴다(R-1). 썼으면 True, 이미 있었으면 False다."""
        inventory = inventory_of(message)
        key = inventory_key(message.source_id, message.job_id, message.chunk_id)
        with ctx.timing.stage("aggregate.inventory_store", unit="objects") as block:
            written = await asyncio.to_thread(
                self._storage.put_bytes_if_absent,
                key,
                inventory.model_dump_json(by_alias=True).encode(),
                _JSON,
            )
            block.count = len(inventory.static_objects)
        return written

    def load_all(self, source_id: UUID, job_id: UUID) -> list[ChunkInventory]:
        """job의 재고 파일 전체. 블로킹이라 `asyncio.to_thread`로 부른다."""
        return [
            ChunkInventory.model_validate_json(self._storage.get_bytes(key))
            for key in self._storage.list_keys(inventory_prefix(source_id, job_id))
            if key.endswith(".json")
        ]
