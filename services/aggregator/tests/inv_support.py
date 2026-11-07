"""재고(SPEC-028) 테스트가 함께 쓰는 입력 빌더."""

from uuid import UUID

from agg_support import JOB, NORMALIZATION, OWNER, SOURCE, uid
from vlm_contracts.aggregates import ChunkInventory, InventoryObject, inventory_key
from vlm_contracts.events import ChunkManifest, StaticObject
from vlm_storage.storage import ObjectStorage

PERSON_ZONE = UUID("0192f100-0000-7000-8000-0000000000f3")


def execution_of(chunk: UUID) -> UUID:
    return UUID(int=chunk.int + 0xE000)


def prefix(chunk: UUID, *, source: UUID = SOURCE, job: UUID = JOB) -> str:
    return f"sources/{source}/jobs/{job}/chunks/{chunk}/executions/{execution_of(chunk)}/blurred/"


def static(
    local: str,
    chunk: UUID,
    *,
    cls: str = "safety_barrier",
    box: tuple[float, float, float, float] = (0.1, 0.5, 0.2, 0.15),
    zone: UUID | None = None,
    first: int = 0,
    last: int = 50_000,
    source: UUID = SOURCE,
) -> StaticObject:
    x, y, w, h = box
    return StaticObject.model_validate(
        {
            "objectLocalId": local,
            "className": cls,
            "box": {"x": x, "y": y, "w": w, "h": h},
            "zoneId": str(zone) if zone else None,
            "firstSeenMs": first,
            "lastSeenMs": last,
            "observationCount": 10,
            "frameKey": f"{prefix(chunk, source=source)}static-{local}.jpg",
        }
    )


def chunk_inventory(
    chunk: UUID,
    start: int,
    end: int,
    *objects: StaticObject,
    frames: tuple[int, ...] = (),
    source: UUID = SOURCE,
) -> ChunkInventory:
    points = frames or (start, (start + end) // 2, end - 100)
    return ChunkInventory.model_validate(
        {
            "sourceId": str(source),
            "jobId": str(JOB),
            "chunkId": str(chunk),
            "executionId": str(execution_of(chunk)),
            "ownedRange": {"startMs": start, "endMs": end},
            "detectorProfile": "detect-v1",
            "staticObjects": [o.model_dump(mode="json", by_alias=True) for o in objects],
            "referenceFrames": [
                {"ptsMs": p, "key": f"{prefix(chunk, source=source)}ref-{p}.jpg"} for p in points
            ],
        }
    )


def manifest_of(inventory: ChunkInventory, *, job_type: str | None = None) -> ChunkManifest:
    body: dict[str, object] = {
        "eventId": str(uid(0x3A00 + inventory.chunk_id.int % 0x100)),
        "occurredAt": "2026-11-06T10:00:00Z",
        "sourceId": str(inventory.source_id),
        "ownerId": str(OWNER),
        "jobId": str(inventory.job_id),
        "chunkId": str(inventory.chunk_id),
        "executionId": str(inventory.execution_id),
        "normalizationId": str(NORMALIZATION),
        "ownedRange": inventory.owned_range.model_dump(by_alias=True),
        "counts": {"segments": 1, "candidates": 0, "tracks": 0},
        "detector": {"detectorProfile": "detect-v1", "models": [], "supportedEvents": []},
        "unsupportedEvents": {},
        "staticObjects": [
            o.model_dump(mode="json", by_alias=True) for o in inventory.static_objects
        ],
        "referenceFrames": [
            f.model_dump(mode="json", by_alias=True) for f in inventory.reference_frames
        ],
    }
    if job_type is not None:
        body["jobType"] = job_type
    return ChunkManifest.model_validate(body)


def put_inventory(storage: ObjectStorage, inventory: ChunkInventory) -> None:
    storage.put_bytes(
        inventory_key(inventory.source_id, inventory.job_id, inventory.chunk_id),
        inventory.model_dump_json(by_alias=True).encode(),
        "application/json",
    )


def previous_object(
    object_id: str,
    *,
    cls: str = "safety_barrier",
    box: tuple[float, float, float, float] = (0.1, 0.5, 0.2, 0.15),
    zone: UUID | None = None,
    source: UUID,
) -> InventoryObject:
    x, y, w, h = box
    chunk = uid(0x9900)
    return InventoryObject.model_validate(
        {
            "objectId": object_id,
            "className": cls,
            "box": {"x": x, "y": y, "w": w, "h": h},
            "zoneId": str(zone) if zone else None,
            "firstSeenMs": 0,
            "lastSeenMs": 590_000,
            "frameKey": f"{prefix(chunk, source=source, job=uid(0x9901))}static-{object_id}.jpg",
        }
    )
