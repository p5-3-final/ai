"""실시간(STREAM) 창 증분 fold와 세션 마무리(SPEC-027 R-29~R-34)."""

import json

import pytest
from agg_support import (
    HAZARD,
    RESULT_ID,
    STREAM_JOB,
    STREAM_PREVIOUS,
    STREAM_SOURCE,
    StreamHarness,
    analysis,
    context,
    contract_errors,
    event_errors,
    risk_event,
    stream_analyzed,
    stream_completed,
    stream_manifest,
    stream_tracks,
    uid,
    window_id,
)
from aggregator.fold import SessionState, fold
from aggregator.service import AggregatorService, CountMismatchError
from aggregator.session_state import SessionStateStore
from vlm_contracts.aggregates import result_key
from vlm_contracts.events import SourceCompleted, StateUpdated

FALL = analysis("fall", risk=True, risk_type="fall", risk_reason="쓰러진 작업자", evidence_end=12.0)
UP = analysis("fall", risk=False, risk_type="fall", evidence_end=33.0, after_state="일어남")


async def _window(
    h: StreamHarness,
    service: AggregatorService,
    sequence: int,
    *events: object,
    tracks: tuple[tuple[str, str, int, int], ...] = (),
    attempt: int = 1,
    count: int | None = None,
    manifest: bool = True,
) -> None:
    for e in events:
        await service.on_event_analyzed(e, context(topic="event.analyzed"))  # type: ignore[arg-type]
    if tracks:
        message, body = stream_tracks(sequence, *tracks)
        assert message.tracks_key is not None
        h.storage.put_bytes(message.tracks_key, body, "application/json")
        await service.on_track_detected(message, context(topic="track.detected"))
    if manifest:
        await service.on_chunk_manifest(stream_manifest(sequence), context(topic="chunk.manifest"))
    await service.on_analysis_completed(
        stream_completed(sequence, events=len(events) if count is None else count),
        context(attempt=attempt),
    )


def _updates(h: StreamHarness) -> list[StateUpdated]:
    return [e for e in h.publisher.of("state.updated") if isinstance(e, StateUpdated)]


async def test_spec027_r29_r30_window_is_folded_into_one_state_updated() -> None:
    """[SPEC-027 R-29·R-30] 창의 confirmed 이벤트만 fold해 state.updated(sequence 1) 하나를 낸다."""
    h = StreamHarness()
    service = h.service()
    rejected = analysis("fall", "rejected")

    await _window(
        h,
        service,
        1,
        stream_analyzed(1, 1, at=12_000, result=FALL),
        stream_analyzed(2, 1, at=13_000, result=rejected),
        tracks=(("t5", "person", 0, 20_000),),
    )

    [update] = _updates(h)
    assert event_errors(update, "state.updated") == []
    assert (update.sequence, update.chunk_id, update.window_start_ms, update.window_end_ms) == (
        1,
        window_id(1),
        0,
        20_000,
    )
    assert [t.transition for t in update.risk_transitions] == ["activated"]
    assert update.active_risk_zone_ids == ["global"]
    assert (update.counts.input_events, update.counts.confirmed_events) == (2, 1)
    [exposure] = update.exposure_deltas
    assert (exposure.active_seconds_delta, exposure.exposure_count_delta) == (8.0, 1)


async def test_spec027_r30_next_window_continues_from_session_state() -> None:
    """[SPEC-027 R-30] 다음 창은 세션 현재 상태(Redis)에서 이어 간다. sequence는 창 순번이다."""
    h = StreamHarness()
    service = h.service()

    await _window(h, service, 1, stream_analyzed(1, 1, at=12_000, result=FALL))
    await _window(h, service, 2, stream_analyzed(2, 2, at=33_000, result=UP))

    first, second = _updates(h)
    assert second.sequence == 2
    assert [t.transition for t in second.risk_transitions] == ["resolved"]
    assert second.risk_transitions[0].risk_id == first.risk_transitions[0].risk_id
    assert [s.current_state for s in second.snapshots] == ["일어남"]
    assert second.active_risk_zone_ids == []


async def test_spec027_r30_same_window_again_republishes_without_recomputing() -> None:
    """[SPEC-027 R-30] 이미 처리한 창이 다시 오면 저장한 state.updated를 다시 낸다."""
    h = StreamHarness()
    service = h.service()
    await _window(h, service, 1, stream_analyzed(1, 1, at=12_000, result=FALL))

    await service.on_analysis_completed(stream_completed(1, events=1), context())

    first, again = _updates(h)
    assert again == first


async def test_spec027_r30_count_mismatch_retries_then_partial() -> None:
    """[SPEC-027 R-30] rule* 수가 counts와 다르면 R-4처럼 재시도하고 상한 시도에서 partial이다."""
    h = StreamHarness()
    service = h.service()

    with pytest.raises(CountMismatchError):
        await _window(h, service, 1, stream_analyzed(1, 1, at=12_000, result=FALL), count=2)
    await service.on_analysis_completed(stream_completed(1, events=2), context(attempt=5))

    [update] = _updates(h)
    assert update.partial is True


async def test_spec027_r30_snapshot_is_written_every_n_windows() -> None:
    """[SPEC-027 R-30] N창(기본 30)마다 S3 `sources/<sourceId>/state/<sequence>.json`에 저장한다."""
    h = StreamHarness(snapshot_every=2)
    service = h.service()

    await _window(h, service, 1, stream_analyzed(1, 1, at=12_000, result=FALL))
    assert not h.storage.exists(f"sources/{STREAM_SOURCE}/state/1.json")
    await _window(h, service, 2)

    assert h.storage.exists(f"sources/{STREAM_SOURCE}/state/2.json")


async def test_spec027_r30_rotation_starts_from_previous_session_state() -> None:
    """[SPEC-027 R-30] 세션 상태가 없으면 직전 세션(previousSourceId)의 마지막 상태로 시작한다."""
    h = StreamHarness()
    assert h.store is not None
    previous, _ = fold(
        SessionState(source_id=STREAM_PREVIOUS),
        [risk_event(9, 5_000, detected=True, risk_type="fall", zone=None)],
        job_id=uid(1),
    )
    await SessionStateStore(h.store, h.storage).save(STREAM_PREVIOUS, previous, snapshot=False)
    service = h.service()

    await _window(h, service, 1, stream_analyzed(2, 1, at=12_000, result=UP))

    [update] = _updates(h)
    assert [t.risk_id for t in update.risk_transitions] == [previous.active_risks[0].risk_id]
    assert update.risk_transitions[0].transition == "resolved"


ACCESS = analysis("hazard_zone_access", evidence_end=15.0)
ZONE_RISK = analysis(
    "ppe_missing", risk=True, risk_type="ppe_missing", risk_reason="안전모 미착용", evidence_end=5.0
)


async def test_spec027_r31_access_during_active_risk_is_rejudged_with_crop() -> None:
    """[SPEC-027 R-31] 위험구역 접근이 같은 구역 활성 위험과 겹치면 대표 크롭으로 재판정한다.

    판정 방식은 rule+vlm으로 기록한다.
    """
    h = StreamHarness()
    h.gateway.judge_output = json.loads(
        analysis(
            "hazard_zone_access",
            risk=True,
            risk_type="ppe_missing",
            risk_reason="무단 진입",
            evidence_end=15.0,
        ).model_dump_json(by_alias=True)
    )
    service = h.service()

    await _window(
        h,
        service,
        1,
        stream_analyzed(
            1,
            1,
            at=5_000,
            result=ZONE_RISK,
            method="rule+vlm",
            zone=HAZARD,
            event_type="ppe_missing",
        ),
        stream_analyzed(
            2, 1, at=15_000, result=ACCESS, zone=HAZARD, event_type="hazard_zone_access"
        ),
        tracks=(("t5", "person", 0, 20_000),),
        count=2,
    )

    assert h.gateway.task_types() == ["event_judge"]
    [(request, _)] = h.gateway.requests
    assert [i.storage_key for i in request.images] == [
        f"{stream_tracks(1)[0].tracks_key[: -len('tracks.json')]}blurred/t5.jpg"  # type: ignore[index]
    ]
    [update] = _updates(h)
    assert [t.transition for t in update.risk_transitions] == ["activated", "updated"]


async def test_spec027_r31_no_rejudge_beyond_budget() -> None:
    """[SPEC-027 R-31] job당 상한을 넘으면 부르지 않는다(SPEC-028 R-8과 같은 계수)."""
    h = StreamHarness(judge_limit=0)
    service = h.service()

    await _window(
        h,
        service,
        1,
        stream_analyzed(
            1,
            1,
            at=5_000,
            result=ZONE_RISK,
            method="rule+vlm",
            zone=HAZARD,
            event_type="ppe_missing",
        ),
        stream_analyzed(
            2, 1, at=15_000, result=ACCESS, zone=HAZARD, event_type="hazard_zone_access"
        ),
        tracks=(("t5", "person", 0, 20_000),),
        count=2,
    )

    assert h.gateway.requests == []


async def test_spec027_r32_session_completion_writes_result_and_source_completed() -> None:
    """[SPEC-027 R-32] 세션 완료면 창 기록 전체로 결과 파일과 source.completed(revision 1)."""
    h = StreamHarness()
    service = h.service()
    await _window(h, service, 1, stream_analyzed(1, 1, at=12_000, result=FALL))
    await _window(h, service, 2, stream_analyzed(2, 2, at=33_000, result=UP))

    await service.on_analysis_completed(stream_completed(None), context())

    [sent] = [e for e in h.publisher.of("source.completed") if isinstance(e, SourceCompleted)]
    assert event_errors(sent, "source.completed") == []
    assert (sent.revision, sent.job_type, sent.session) == (1, "STREAM", None)
    body = json.loads(h.storage.get_bytes(result_key(STREAM_SOURCE, STREAM_JOB)))
    assert contract_errors(RESULT_ID, body) == []
    [risk] = body["risks"]
    assert (risk["status"], risk["firstSeenMs"], risk["resolvedAtMs"]) == (
        "resolved",
        12_000,
        33_000,
    )
    assert [s["currentState"] for s in body["snapshots"]] == ["일어남"]
    assert body["summary"]["status"] == "generated"


async def test_spec027_r33_zones_are_read_once_per_session() -> None:
    """[SPEC-027 R-33] 세션 구역은 세션당 한 번 읽어 캐시한다."""
    h = StreamHarness()
    service = h.service()

    await _window(h, service, 1)
    await _window(h, service, 2)

    assert h.zones.calls == 1


async def test_spec027_r34_window_fold_stage_is_recorded() -> None:
    """[SPEC-027 R-34] 창 단위 계산을 aggregate.fold 단계로 남긴다(count = 입력 이벤트 수)."""
    h = StreamHarness()
    service = h.service()
    await service.on_event_analyzed(
        stream_analyzed(1, 1, at=12_000, result=FALL), context(topic="event.analyzed")
    )
    await service.on_chunk_manifest(stream_manifest(1), context())
    ctx = context()

    await service.on_analysis_completed(stream_completed(1, events=1), ctx)

    assert {t.stage_id: t.count for t in ctx.timing.timings} == {"aggregate.fold": 1}
