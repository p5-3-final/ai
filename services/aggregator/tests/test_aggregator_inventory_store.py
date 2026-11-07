"""청크 재고 파일 저장(SPEC-028 R-1, R-18)."""

import json

from agg_support import CHUNK_A, CHUNK_B, JOB, SOURCE, Harness, context, contract_errors
from inv_support import chunk_inventory, manifest_of, static
from vlm_contracts.aggregates import inventory_key

CHUNK_SCHEMA = "https://vlm.example/contracts/aggregates/v1/chunk-inventory.schema.json"


async def test_spec028_r1_manifest_is_stored_as_chunk_inventory_file() -> None:
    """[SPEC-028 R-1] chunk.manifest의 재고와 기준 프레임을 청크 재고 파일로 저장한다."""
    h = Harness()
    inventory = chunk_inventory(CHUNK_B, 60_000, 120_000, static("s1", CHUNK_B))

    await h.service().on_chunk_manifest(manifest_of(inventory), context(topic="chunk.manifest"))

    stored = json.loads(h.storage.get_bytes(inventory_key(SOURCE, JOB, CHUNK_B)))
    assert contract_errors(CHUNK_SCHEMA, stored) == []
    assert stored == inventory.model_dump(mode="json", by_alias=True)
    assert h.publisher.published == []


async def test_spec028_r1_first_execution_wins() -> None:
    """[SPEC-028 R-1] 이미 파일이 있으면 다른 실행의 manifest가 와도 아무것도 하지 않는다."""
    h = Harness()
    first = chunk_inventory(CHUNK_B, 60_000, 120_000, static("s1", CHUNK_B))
    second = chunk_inventory(CHUNK_B, 60_000, 120_000)
    service = h.service()

    await service.on_chunk_manifest(manifest_of(first), context(topic="chunk.manifest"))
    await service.on_chunk_manifest(manifest_of(second), context(topic="chunk.manifest"))

    stored = json.loads(h.storage.get_bytes(inventory_key(SOURCE, JOB, CHUNK_B)))
    assert len(stored["staticObjects"]) == 1


async def test_spec028_r18_inventory_store_stage_counts_objects() -> None:
    """[SPEC-028 R-18] 저장은 단계 aggregate.inventory_store로 재고 객체 수를 센다."""
    h = Harness()
    inventory = chunk_inventory(
        CHUNK_A, 0, 60_000, static("s1", CHUNK_A), static("s2", CHUNK_A, box=(0.6, 0.1, 0.1, 0.1))
    )
    ctx = context(topic="chunk.manifest")

    await h.service().on_chunk_manifest(manifest_of(inventory), ctx)

    assert {(t.stage_id, t.count, t.unit) for t in ctx.timing.timings} == {
        ("aggregate.inventory_store", 2, "objects")
    }
