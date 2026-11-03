import contextlib
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from normalize.decision import Limits, Normalize
from normalize.ffmpeg import DecodeError
from normalize.ids import event_id, hls_playlist_key, hls_prefix, normalized_key
from normalize.probe import MediaReadError, ProbeResult
from normalize.service import NormalizeConfig, NormalizeService
from vlm_contracts.events import Event, VideoNormalized, VideoNormalizing, VideoUploaded
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
ORIGINAL_KEY = f"sources/{SOURCE}/original"

H264_CFR = ProbeResult(
    has_video=True,
    video_codec="h264",
    duration_sec=60.0,
    avg_fps=30.0,
    base_fps=30.0,
    width=1280,
    height=720,
    audio_codec="aac",
)
NORMALIZED = ProbeResult(
    has_video=True,
    video_codec="h264",
    duration_sec=60.04,
    avg_fps=30.0,
    base_fps=30.0,
    width=1280,
    height=720,
    audio_codec="aac",
)


@dataclass
class FakeMedia:
    """원본·사본의 ffprobe 결과와 실패를 정해 둘 수 있는 MediaTool."""

    original: ProbeResult | Exception = H264_CFR
    output: ProbeResult = NORMALIZED
    normalize_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    plans: list[Normalize] = field(default_factory=list)

    def probe(self, path: Path) -> ProbeResult:
        self.calls.append(f"probe:{path.name}")
        result = self.output if path.name == "video.mp4" else self.original
        if isinstance(result, Exception):
            raise result
        return result

    def normalize(self, src: Path, dst: Path, plan: Normalize, audio_codec: str | None) -> None:
        self.calls.append("normalize")
        self.plans.append(plan)
        if self.normalize_error is not None:
            raise self.normalize_error
        dst.write_bytes(b"mp4")

    def make_hls(self, src: Path, out_dir: Path, segment_s: float) -> list[Path]:
        self.calls.append("hls")
        out_dir.mkdir(parents=True, exist_ok=True)
        files = [out_dir / "seg_00000.ts", out_dir / "seg_00001.ts", out_dir / "index.m3u8"]
        for f in files:
            f.write_bytes(f.name.encode())
        return files


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)
    calls: list[str] | None = None

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.published.append((topic, event, key, traceparent))
        if self.calls is not None:
            self.calls.append(f"publish:{topic}")

    def events(self, topic: str) -> list[Event]:
        return [e for t, e, _, _ in self.published if t == topic]


class FailingStorage(InMemoryStorage):
    def put_file(self, key: str, path: Path, content_type: str) -> None:
        raise RuntimeError("storage unavailable")


@dataclass
class Harness:
    service: NormalizeService
    storage: InMemoryStorage
    media: FakeMedia
    publisher: RecordingPublisher
    work_dir: Path


def _harness(
    tmp_path: Path, media: FakeMedia | None = None, storage: InMemoryStorage | None = None
) -> Harness:
    media = media or FakeMedia()
    storage = storage or InMemoryStorage()
    storage.put_bytes(ORIGINAL_KEY, b"original", "video/mp4")
    publisher = RecordingPublisher(calls=media.calls)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config = NormalizeConfig(
        limits=Limits(min_duration_s=1.0, max_duration_s=14_400.0, max_fps=30.0),
        hls_segment_s=6.0,
        work_dir=work_dir,
    )
    service = NormalizeService(storage, media, publisher, config, clock=lambda: NOW)
    return Harness(service, storage, media, publisher, work_dir)


def _uploaded() -> VideoUploaded:
    return VideoUploaded(
        event_id=UUID("0192f0d3-0000-7000-8000-000000000001"),
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        storage_key=ORIGINAL_KEY,
        size_bytes=8,
        content_type="video/mp4",
    )


CTX = MessageContext(
    topic="video.uploaded", partition=0, offset=0, key=None, attempt=1, traceparent="00-t-s-01"
)


def _normalized(h: Harness) -> VideoNormalized:
    [event] = h.publisher.events("video.normalized")
    assert isinstance(event, VideoNormalized)
    return event


async def test_r1_publishes_normalizing_before_probe(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    assert h.media.calls.index("publish:video.normalizing") < h.media.calls.index("probe:original")
    [started] = h.publisher.events("video.normalizing")
    assert isinstance(started, VideoNormalizing)
    assert started.source_id == SOURCE
    assert started.owner_id == OWNER


async def test_r2_accepted_video_is_stored(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    assert h.storage.get_bytes(normalized_key(SOURCE)) == b"mp4"


async def test_r3_r4_plan_from_decision_is_passed_to_ffmpeg(tmp_path: Path) -> None:
    hevc = replace(H264_CFR, video_codec="hevc")
    h = _harness(tmp_path, FakeMedia(original=hevc))

    await h.service.handle(_uploaded(), CTX)

    assert h.media.plans == [Normalize(transcode=True, fps=30.0)]
    assert _normalized(h).transcoded is True


async def test_r5_hls_segments_are_stored_before_playlist(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    assert h.storage.get_bytes(hls_playlist_key(SOURCE)) == b"index.m3u8"
    assert h.storage.exists(f"{hls_prefix(SOURCE)}seg_00000.ts")
    assert h.storage.exists(f"{hls_prefix(SOURCE)}seg_00001.ts")


async def test_r6_ready_event_payload(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    event = _normalized(h)
    assert event.result == "READY"
    assert event.reject_reason is None
    assert event.normalized_key == normalized_key(SOURCE)
    assert event.hls_playlist_key == hls_playlist_key(SOURCE)
    # 길이·fps·해상도는 원본이 아니라 정규화 사본에서 읽은 값이다.
    assert event.duration_sec == 60.04
    assert (event.fps, event.width, event.height, event.transcoded) == (30.0, 1280, 720, False)
    assert event.owner_id == OWNER
    assert event.occurred_at == NOW


async def test_r6_ready_is_published_after_uploads(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    assert h.media.calls[-1] == "publish:video.normalized"
    assert h.storage.exists(hls_playlist_key(SOURCE))


async def test_events_carry_trace_and_source_key(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)

    for _, _, key, traceparent in h.publisher.published:
        assert key is None  # 발행기가 sourceId를 키로 쓴다
        assert traceparent == "00-t-s-01"


async def test_r7_unreadable_original_is_rejected(tmp_path: Path) -> None:
    h = _harness(tmp_path, FakeMedia(original=MediaReadError("broken")))

    await h.service.handle(_uploaded(), CTX)

    event = _normalized(h)
    assert (event.result, event.reject_reason) == ("REJECTED", "UNREADABLE")
    assert event.normalized_key is None
    assert not h.storage.exists(normalized_key(SOURCE))


async def test_r8_decision_reject_is_published_without_ffmpeg(tmp_path: Path) -> None:
    audio_only = ProbeResult(
        has_video=False,
        video_codec=None,
        duration_sec=10.0,
        avg_fps=None,
        base_fps=None,
        width=None,
        height=None,
        audio_codec="aac",
    )
    h = _harness(tmp_path, FakeMedia(original=audio_only))

    await h.service.handle(_uploaded(), CTX)

    assert _normalized(h).reject_reason == "NO_VIDEO_STREAM"
    assert "normalize" not in h.media.calls


async def test_r12_decode_failure_rejects(tmp_path: Path) -> None:
    h = _harness(tmp_path, FakeMedia(normalize_error=DecodeError("invalid data")))

    await h.service.handle(_uploaded(), CTX)

    assert _normalized(h).reject_reason == "DECODE_FAILED"
    assert not h.storage.exists(normalized_key(SOURCE))


async def test_r13_storage_error_propagates_without_result(tmp_path: Path) -> None:
    h = _harness(tmp_path, storage=FailingStorage())

    with pytest.raises(RuntimeError, match="storage unavailable"):
        await h.service.handle(_uploaded(), CTX)

    assert h.publisher.events("video.normalized") == []


async def test_r14_reprocessing_publishes_same_event_ids(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    await h.service.handle(_uploaded(), CTX)
    await h.service.handle(_uploaded(), CTX)

    ids = [e.event_id for _, e, _, _ in h.publisher.published]
    assert (
        ids
        == [
            event_id("video.normalizing", SOURCE),
            event_id("video.normalized", SOURCE),
        ]
        * 2
    )


async def test_deleted_original_ends_without_result(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.storage.delete_prefix(f"sources/{SOURCE}/")

    await h.service.handle(_uploaded(), CTX)

    assert h.publisher.events("video.normalized") == []


async def test_nfr4_logs_have_no_owner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    h = _harness(tmp_path)

    with caplog.at_level(logging.DEBUG):
        await h.service.handle(_uploaded(), CTX)

    assert caplog.records
    for record in caplog.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        assert str(OWNER) not in rendered
        assert str(SOURCE) in rendered


@pytest.mark.parametrize("storage", [InMemoryStorage(), FailingStorage()])
async def test_nfr5_work_dir_removed_on_success_and_failure(
    tmp_path: Path, storage: InMemoryStorage
) -> None:
    h = _harness(tmp_path, storage=storage)

    # 실패하는 저장소에서는 예외가 나야 한다(R-13). 여기서는 작업 폴더만 확인한다.
    with contextlib.suppress(RuntimeError):
        await h.service.handle(_uploaded(), CTX)

    assert list(h.work_dir.iterdir()) == []
