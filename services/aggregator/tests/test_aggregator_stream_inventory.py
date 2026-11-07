"""실시간 세션의 주기 재고 비교·장비 방치·예측(SPEC-028 R-19)."""

import json

from agg_support import (
    AISLE,
    EXECUTION,
    RESULT_ID,
    STREAM_JOB,
    STREAM_PREVIOUS,
    STREAM_SOURCE,
    StreamHarness,
    context,
    contract_errors,
    event_errors,
    stream_completed,
    stream_manifest,
    stream_prefix,
    window_id,
)
from aggregator.fold import SessionState
from aggregator.inventory_store import InventoryStore
from aggregator.service import AggregatorService
from aggregator.session_state import SessionStateStore
from aggregator.stream import inventory_due
from vlm_contracts.aggregates import InventoryObject, inventory_key, result_key
from vlm_contracts.events import ChunkManifest, SourceCompleted, StateUpdated, StaticObject

BARRIER = (0.1, 0.5, 0.2, 0.15)


def _static(sequence: int, local: str, *, first: int, last: int) -> StaticObject:
    return StaticObject.model_validate(
        {
            "objectLocalId": local,
            "className": "safety_barrier",
            "box": {"x": 0.1, "y": 0.5, "w": 0.2, "h": 0.15},
            "zoneId": str(AISLE),
            "firstSeenMs": first,
            "lastSeenMs": last,
            "observationCount": 5,
            "frameKey": f"{stream_prefix(sequence)}blurred/static-{local}.jpg",
        }
    )


def _manifest(sequence: int, *objects: StaticObject) -> ChunkManifest:
    return stream_manifest(sequence).model_copy(update={"static_objects": list(objects)})


async def _window(
    h: StreamHarness, service: AggregatorService, sequence: int, *objects: StaticObject
) -> None:
    await service.on_chunk_manifest(_manifest(sequence, *objects), context(topic="chunk.manifest"))
    await service.on_analysis_completed(stream_completed(sequence), context())


def _updates(h: StreamHarness) -> list[StateUpdated]:
    return [e for e in h.publisher.of("state.updated") if isinstance(e, StateUpdated)]


def test_spec028_r19_inventory_is_due_every_interval() -> None:
    """[SPEC-028 R-19] 처음이거나 마지막 계산 뒤 주기(초)가 지났으면 계산한다."""
    assert inventory_due(SessionState(), 20_000, interval_s=600)
    done = SessionState(inventory_at_ms=20_000)
    assert not inventory_due(done, 619_999, interval_s=600)
    assert inventory_due(done, 620_000, interval_s=600)


async def test_spec028_r19_stream_manifest_is_stored_as_chunk_inventory() -> None:
    """[SPEC-028 R-1·R-19] STREAM chunk.manifest도 창 chunkId로 청크 재고 파일에 저장한다."""
    h = StreamHarness()
    service = h.service()

    await service.on_chunk_manifest(
        _manifest(1, _static(1, "s1", first=0, last=19_000)), context(topic="chunk.manifest")
    )

    assert h.storage.exists(inventory_key(STREAM_SOURCE, STREAM_JOB, window_id(1)))


async def test_spec028_r19_removed_barrier_is_judged_and_enters_next_state_updated() -> None:
    """[SPEC-028 R-19] 주기 계산에서 확정된 재고 이벤트는 다음 창의 state.updated에 담긴다."""
    h = StreamHarness(inventory_interval_s=20)
    service = h.service()

    await _window(h, service, 1, _static(1, "s1", first=0, last=15_000))
    await _window(h, service, 2)
    assert h.gateway.task_types() == ["state_compare"]
    assert [u.risk_transitions for u in _updates(h)] == [[], []]

    await _window(h, service, 3)

    third = _updates(h)[2]
    [transition] = third.risk_transitions
    assert (transition.transition, transition.zone_id) == ("activated", str(AISLE))
    assert event_errors(third, "state.updated") == []
    # 이미 판정한 후보는 다음 주기에 다시 부르지 않는다.
    assert h.gateway.task_types() == ["state_compare"]


async def test_spec028_r19_not_due_window_does_not_compare() -> None:
    """[SPEC-028 R-19] 주기(기본 600초)가 지나지 않은 창에서는 재고 비교를 하지 않는다."""
    h = StreamHarness()
    service = h.service()

    await _window(h, service, 1, _static(1, "s1", first=0, last=15_000))
    await _window(h, service, 2)

    assert h.gateway.requests == []


async def test_spec028_r8_r19_cap_is_counted_per_session() -> None:
    """[SPEC-028 R-8·R-19] 판정 상한은 세션당이다. 상한이 0이면 부르지 않는다."""
    h = StreamHarness(inventory_interval_s=20, judge_limit=0)
    service = h.service()

    await _window(h, service, 1, _static(1, "s1", first=0, last=15_000))
    await _window(h, service, 2)

    assert h.gateway.requests == []


async def test_spec028_r6_r19_rotation_uses_previous_session_end_inventory() -> None:
    """[SPEC-028 R-6·R-19] 회전 세션은 직전 세션 상태의 끝 재고를 첫 청크 앞에 놓는다."""
    h = StreamHarness()
    assert h.store is not None
    frame = (
        f"sources/{STREAM_PREVIOUS}/jobs/{STREAM_JOB}/chunks/{window_id(9)}/executions/"
        f"{EXECUTION}/blurred/static-p1.jpg"
    )
    h.storage.put_bytes(frame, b"jpeg", "image/jpeg")
    before = InventoryObject.model_validate(
        {
            "objectId": "prev-1",
            "className": "safety_barrier",
            "box": {"x": 0.1, "y": 0.5, "w": 0.2, "h": 0.15},
            "zoneId": str(AISLE),
            "firstSeenMs": 0,
            "lastSeenMs": 1_000,
            "frameKey": frame,
        }
    )
    previous = SessionState(source_id=STREAM_PREVIOUS, inventory=(before,))
    await SessionStateStore(h.store, h.storage).save(STREAM_PREVIOUS, previous, snapshot=False)
    service = h.service()

    await _window(h, service, 1)

    [(request, _)] = h.gateway.requests
    assert request.variables["changeKind"] == "not_restored"


async def test_spec028_r19_session_completion_has_inventory_and_forecasts() -> None:
    """[SPEC-028 R-19] 세션 마무리는 마지막으로 한 번 더 계산하고 결과 파일에 재고·예측을 싣는다."""
    h = StreamHarness(inventory_interval_s=20)
    service = h.service()
    await _window(h, service, 1, _static(1, "s1", first=0, last=15_000))
    await _window(h, service, 2)

    await service.on_analysis_completed(stream_completed(None), context())

    body = json.loads(h.storage.get_bytes(result_key(STREAM_SOURCE, STREAM_JOB)))
    assert contract_errors(RESULT_ID, body) == []
    assert [e["eventType"] for e in body["inventoryEvents"]] == ["object_removed"]
    assert [f["status"] for f in body["forecasts"]] == ["generated"]
    assert body["inventory"] == []
    [sent] = [e for e in h.publisher.of("source.completed") if isinstance(e, SourceCompleted)]
    assert (sent.counts.inventory_events, sent.counts.forecasts) == (1, 1)
    assert len(InventoryStore(h.storage).load_all(STREAM_SOURCE, STREAM_JOB)) == 2
