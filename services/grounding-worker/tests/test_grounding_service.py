"""grounding-worker 핸들러(SPEC-026 R-1, R-3~R-14, R-17, R-18)."""

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from grounding_worker.detector import Box, Detection, RgbFrame
from grounding_worker.ids import completed_event_id
from grounding_worker.keys import clip_key, result_key
from grounding_worker.profile import RenderProfile
from grounding_worker.render import target_color
from grounding_worker.service import GroundingConfig, GroundingService
from gw_support import (
    MODEL,
    NORMALIZED_KEY,
    NOW,
    SOURCE,
    TARGETS,
    TRACEPARENT,
    FakeDetector,
    FakeMedia,
    RecordingPublisher,
    event_errors,
    grounding_request,
    result_errors,
    segment,
)
from vlm_contracts.events import GroundingCompleted, GroundingRequested
from vlm_contracts.groundings import GroundingResultFile
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage

CTX = MessageContext(
    topic="grounding.requested",
    partition=0,
    offset=0,
    key=None,
    attempt=1,
    traceparent=TRACEPARENT,
)


class RecordingStorage(InMemoryStorage):
    """쓴 순서를 남기고, 지정한 키 쓰기에서 연결 오류를 낸다."""

    def __init__(self, fail_put: str | None = None) -> None:
        super().__init__()
        self.puts: list[str] = []
        self.fail_put = fail_put

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if key == self.fail_put:
            raise ConnectionError("storage down")
        self.puts.append(key)
        super().put_bytes(key, data, content_type)


def _storage(fail_put: str | None = None) -> RecordingStorage:
    storage = RecordingStorage(fail_put)
    storage.put_bytes(NORMALIZED_KEY, b"normalized", "video/mp4")
    storage.puts.clear()
    return storage


def _config(
    *,
    score_threshold: float = 0.25,
    sample_interval_ms: int = 1_000,
    item_timeout_s: float = 120,
    work_dir: Path | None = None,
) -> GroundingConfig:
    return GroundingConfig(
        score_threshold=score_threshold,
        render=RenderProfile(),
        sample_interval_ms=sample_interval_ms,
        max_samples=12,
        item_timeout_s=item_timeout_s,
        work_dir=work_dir,
    )


async def _run(
    message: GroundingRequested | None = None,
    *,
    storage: RecordingStorage | None = None,
    media: FakeMedia | None = None,
    detector: FakeDetector | None = None,
    publisher: RecordingPublisher | None = None,
    config: GroundingConfig | None = None,
    ctx: MessageContext = CTX,
) -> tuple[RecordingStorage, FakeMedia, FakeDetector, RecordingPublisher]:
    storage = storage or _storage()
    media = media or FakeMedia()
    detector = detector or FakeDetector()
    publisher = publisher or RecordingPublisher()
    service = GroundingService(
        storage, media, detector, publisher, config or _config(), clock=lambda: NOW
    )
    await service.handle(message or grounding_request(), ctx)
    return storage, media, detector, publisher


def _events(publisher: RecordingPublisher) -> list[GroundingCompleted]:
    events = []
    for topic, event, key, traceparent in publisher.published:
        assert (topic, key, traceparent) == ("grounding.completed", str(SOURCE), TRACEPARENT)
        assert isinstance(event, GroundingCompleted)
        assert event_errors(event) == []
        events.append(event)
    return events


def _result(storage: InMemoryStorage, index: int = 0) -> GroundingResultFile:
    data = storage.get_bytes(result_key(SOURCE, segment(index).grounding_id, MODEL))
    assert result_errors(data) == []
    return GroundingResultFile.model_validate_json(data)


async def test_spec026_r1_each_segment_in_order_publishes_one_event() -> None:
    """[SPEC-026 R-1] 구간 순서대로 하나씩 처리하고 구간마다 한 건 발행한다."""
    segments = [segment(0, 40_000, 50_000), segment(1, 10_000, 20_000), segment(2, 0, 5_000)]

    _, media, _, publisher = await _run(grounding_request(segments))

    events = _events(publisher)
    assert [e.grounding_id for e in events] == [s.grounding_id for s in segments]
    assert [e.outcome for e in events] == ["READY"] * 3
    assert [r[1:] for r in media.renders] == [(40_000, 10_000), (10_000, 10_000), (0, 5_000)]


async def test_spec026_r2_samples_one_second_apart_from_the_presigned_source() -> None:
    _, media, detector, _ = await _run()

    assert media.frames == [40_000 + k * 1_000 for k in range(10)]
    assert detector.frames == media.frames
    assert media.probes[0] == f"memory://{NORMALIZED_KEY}"


async def test_spec026_r6_stores_clip_then_result_then_publishes_ready() -> None:
    """[SPEC-026 R-6] 클립 → 결과 파일 순서로 저장하고 READY를 발행한다."""
    storage, _, _, publisher = await _run()

    clip = clip_key(SOURCE, segment().grounding_id, MODEL)
    result = result_key(SOURCE, segment().grounding_id, MODEL)
    assert storage.puts == [clip, result]
    assert storage.get_bytes(clip) == b"fake mp4"
    [event] = _events(publisher)
    assert (event.clip_key, event.result_key, event.error_code) == (clip, result, None)
    assert event.event_id == completed_event_id(
        segment().grounding_id, MODEL, event.request_id, "READY"
    )
    assert _result(storage).clip.key == clip


async def test_spec026_r4_r5_filter_draws_each_target_in_its_color() -> None:
    _, media, _, _ = await _run(grounding_request([segment(0, 40_000, 42_000)]))

    [script] = media.scripts
    for target in TARGETS:
        assert f"color=0x{target_color(target)[1:]}" in script
    assert script.count("drawbox") == 2 * len(TARGETS)


async def test_spec026_r3_detections_below_threshold_are_dropped() -> None:
    """[SPEC-026 R-3] 프로필 기준 신뢰도 미만인 탐지는 결과·클립·수에서 빠진다."""

    def mixed(frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        return [
            Detection("forklift", Box(0.1, 0.1, 0.2, 0.2), 0.25),
            Detection("forklift", Box(0.5, 0.5, 0.2, 0.2), 0.24),
            Detection("person without a hard hat", Box(0.3, 0.3, 0.1, 0.1), 0.1),
        ]

    storage, media, _, publisher = await _run(
        grounding_request([segment(0, 40_000, 41_000)]), detector=FakeDetector(reply=mixed)
    )

    [frame] = _result(storage).frames
    assert [(d.target, d.confidence) for d in frame.detections] == [("forklift", 0.25)]
    assert media.scripts[0].count("drawbox") == 1
    [event] = _events(publisher)
    assert event.frames is not None
    assert event.frames[0].counts == {"person without a hard hat": 0, "forklift": 1}


async def test_spec026_r7_counts_every_target_per_sample_with_zero() -> None:
    """[SPEC-026 R-7] 대상어가 없으면 박스 없는 클립과 객체 수 0이 담긴 READY다."""
    _, media, _, publisher = await _run(detector=FakeDetector(reply=lambda f, t: []))

    [event] = _events(publisher)
    assert event.outcome == "READY"
    assert event.frames is not None
    assert [f.pts_ms for f in event.frames] == [40_000 + k * 1_000 for k in range(10)]
    assert all(f.counts == dict.fromkeys(TARGETS, 0) for f in event.frames)
    assert media.scripts == ["null"]


async def test_spec026_r17_result_records_model_threshold_and_sample_fps() -> None:
    """[SPEC-026 R-17] 결과 파일에 탐지기 버전·기준 신뢰도, 이벤트에 탐지기 버전을 싣는다."""
    storage, _, _, publisher = await _run(
        config=_config(score_threshold=0.4, sample_interval_ms=500)
    )

    result = _result(storage)
    assert (result.grounding_model, result.score_threshold, result.sample_fps) == (MODEL, 0.4, 2)
    assert result.colors == {t: target_color(t) for t in TARGETS}
    assert (result.clip.fps, result.clip.duration_ms, result.clip.audio) == (30, 10_000, True)
    [event] = _events(publisher)
    assert event.grounding_model == MODEL


async def test_spec026_r18_records_stages_with_counts() -> None:
    ctx = replace(CTX, timing=StageRecorder(service=None, attempt=1, received_at=NOW))

    await _run(grounding_request([segment(0, 40_000, 42_000)]), ctx=ctx)

    assert [(t.stage_id, t.status, t.count) for t in ctx.timing.timings] == [
        ("grounding.sample", "ok", 2),
        ("grounding.detect", "ok", 2),
        ("grounding.render", "ok", 300),
        ("grounding.store", "ok", 2),
    ]


async def test_spec026_r8_existing_result_is_republished_without_detecting() -> None:
    """[SPEC-026 R-8] 결과 파일이 있으면 탐지·인코딩 없이 그 내용으로 같은 eventId를 다시 낸다."""
    storage, _, _, first = await _run()

    _, media, detector, again = await _run(storage=storage)

    assert detector.frames == []
    assert not media.called()
    assert _events(again) == _events(first)


async def test_spec026_r8_reuse_for_another_request_uses_a_new_event_id() -> None:
    storage, _, _, first = await _run()
    other = grounding_request(request_id=segment(9).grounding_id)

    _, _, detector, again = await _run(other, storage=storage)

    assert detector.frames == []
    [before], [after] = _events(first), _events(again)
    assert after.event_id != before.event_id
    assert after.frames == before.frames


async def test_spec026_r14_storage_error_is_raised_and_retry_reuses_finished_segments() -> None:
    """[SPEC-026 R-14] 저장 오류는 그대로 던지고, 다시 처리하면 끝난 구간은 다시 만들지 않는다."""
    message = grounding_request([segment(0), segment(1)])
    failing = result_key(SOURCE, segment(1).grounding_id, MODEL)
    storage = _storage(fail_put=failing)
    publisher = RecordingPublisher()

    with pytest.raises(ConnectionError):
        await _run(message, storage=storage, publisher=publisher)
    assert len(publisher.published) == 1

    storage.fail_put = None
    _, _, detector, retried = await _run(message, storage=storage)

    assert detector.frames == [50_000 - 10_000 + k * 1_000 for k in range(10)]
    assert [e.outcome for e in _events(retried)] == ["READY", "READY"]


async def test_spec026_r14_publish_error_is_raised() -> None:
    with pytest.raises(ConnectionError):
        await _run(publisher=RecordingPublisher(fails=True))


def _failed(publisher: RecordingPublisher) -> GroundingCompleted:
    [event] = _events(publisher)
    assert event.outcome == "FAILED"
    assert (event.clip_key, event.result_key, event.frames) == (None, None, None)
    assert event.event_id == completed_event_id(
        event.grounding_id, MODEL, event.request_id, "FAILED"
    )
    return event


@pytest.mark.parametrize(
    ("start", "end"),
    [(40_000, 40_000), (50_000, 40_000), (115_000, 125_000), (120_000, 120_100)],
)
async def test_spec026_r10_segment_outside_the_video_fails(start: int, end: int) -> None:
    """[SPEC-026 R-10] 끝이 영상 길이를 넘거나 끝이 시작 이하이면 OUT_OF_RANGE다."""
    storage, _, detector, publisher = await _run(grounding_request([segment(0, start, end)]))

    assert _failed(publisher).error_code == "GROUNDING_SEGMENT_OUT_OF_RANGE"
    assert detector.frames == []
    assert storage.puts == []


async def test_spec026_r10_end_within_one_frame_of_the_length_is_accepted() -> None:
    """영상 길이 비교는 프레임 간격 하나만큼 허용한다(ffprobe 길이 반올림, 보고 C-10)."""
    _, media, _, publisher = await _run(grounding_request([segment(0, 110_000, 120_030)]))

    [event] = _events(publisher)
    assert event.outcome == "READY"
    assert max(media.frames) < 120_000


async def test_spec026_r11_missing_source_fails_unreadable() -> None:
    storage = RecordingStorage()

    _, _, _, publisher = await _run(storage=storage)

    assert _failed(publisher).error_code == "GROUNDING_SOURCE_UNREADABLE"


@pytest.mark.parametrize(
    "media",
    [FakeMedia(probe_fails=True), FakeMedia(frame_fails_at=42_000), FakeMedia(render_fails=True)],
)
async def test_spec026_r11_unreadable_source_fails(media: FakeMedia) -> None:
    storage, _, _, publisher = await _run(media=media)

    assert _failed(publisher).error_code == "GROUNDING_SOURCE_UNREADABLE"
    assert storage.puts == []


async def test_spec026_r12_detector_error_fails() -> None:
    _, _, _, publisher = await _run(detector=FakeDetector(fails=True))

    assert _failed(publisher).error_code == "GROUNDING_DETECTOR_FAILED"


@pytest.mark.parametrize(
    "bad",
    [
        Detection("crane", Box(0.1, 0.1, 0.1, 0.1), 0.9),
        Detection("forklift", Box(1.1, 0.1, 0.1, 0.1), 0.9),
        Detection("forklift", Box(0.1, 0.1, 0.1, 0.1), 1.5),
    ],
)
async def test_spec026_r12_detector_output_outside_the_contract_fails(bad: Detection) -> None:
    _, _, _, publisher = await _run(detector=FakeDetector(reply=lambda f, t: [bad]))

    assert _failed(publisher).error_code == "GROUNDING_DETECTOR_FAILED"


async def test_spec026_r13_slow_segment_times_out_and_next_segment_continues() -> None:
    """[SPEC-026 R-13] 구간 하나가 제한 시간을 넘으면 그 구간만 TIMEOUT으로 실패한다."""
    message = grounding_request([segment(0, 40_000, 50_000), segment(1, 0, 1_000)])
    media = FakeMedia(delay_s=0.05)

    storage, _, _, publisher = await _run(message, media=media, config=_config(item_timeout_s=0.3))

    first, second = _events(publisher)
    assert (first.outcome, first.error_code) == ("FAILED", "GROUNDING_TIMEOUT")
    assert second.outcome == "READY"
    assert storage.puts == [
        clip_key(SOURCE, segment(1).grounding_id, MODEL),
        result_key(SOURCE, segment(1).grounding_id, MODEL),
    ]


async def test_failure_log_has_codes_but_not_the_query(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="grounding_worker.service"):
        await _run(detector=FakeDetector(fails=True))

    [record] = [r for r in caplog.records if r.name == "grounding_worker.service"]
    assert record.__dict__["code"] == "GROUNDING_DETECTOR_FAILED"
    assert "hard hat" not in json.dumps(record.__dict__, default=str)


async def test_work_files_are_removed(tmp_path: Path) -> None:
    await _run(config=_config(work_dir=tmp_path))

    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
