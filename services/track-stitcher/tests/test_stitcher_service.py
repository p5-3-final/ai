"""track-stitcher 핸들러 전체(메모리 상태 저장소·메모리 저장소·기록 발행기, SPEC-022)."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from stitcher_support import (
    CHUNK_MS,
    EMBEDDING_MODEL,
    JOB,
    OTHER_MODEL,
    PROFILE,
    SOURCE,
    TRACEPARENT,
    Clock,
    boundary_message,
    boxes,
    chunk_tracks,
    crossing_chunks,
    deleted_message,
    embedded_message,
    event_errors,
    mapping_errors,
    store_chunk,
    store_vectors,
    track,
    tracks_key,
    unit,
    vector_file,
)
from track_stitcher import ids
from track_stitcher.service import SkipCounter, StitcherService
from track_stitcher.state import (
    ChunkRecord,
    JobKey,
    JobMeta,
    MemoryJobStateStore,
    RecordResult,
    VectorRecord,
)
from vlm_contracts.events import (
    ChunkBoundary,
    Event,
    TrackDetected,
    TrackManifest,
)
from vlm_contracts.tracks import ChunkTracks
from vlm_contracts.vectors import VectorFile
from vlm_messaging.publisher import PublishError
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.memory import InMemoryStorage

KEY = JobKey(SOURCE, JOB)
MAPPING_KEY = f"sources/{SOURCE}/jobs/{JOB}/tracks/mapping.json"


@dataclass
class Publisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)
    fail: bool = False

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        if self.fail:
            raise PublishError("broker unavailable")
        self.published.append((topic, event, key, traceparent))

    def detected(self) -> list[TrackDetected]:
        return [e for t, e, _, _ in self.published if isinstance(e, TrackDetected)]

    def manifests(self) -> list[TrackManifest]:
        return [e for t, e, _, _ in self.published if isinstance(e, TrackManifest)]


@dataclass
class Harness:
    storage: InMemoryStorage = field(default_factory=InMemoryStorage)
    store: MemoryJobStateStore = field(default_factory=MemoryJobStateStore)
    publisher: Publisher = field(default_factory=Publisher)
    clock: Clock = field(default_factory=Clock)
    skipped: SkipCounter = field(default_factory=SkipCounter)
    contexts: list[MessageContext] = field(default_factory=list)

    @property
    def service(self) -> StitcherService:
        return StitcherService(
            self.store,
            self.storage,
            self.publisher,
            PROFILE,
            lease_s=60,
            clock=self.clock,
            skipped=self.skipped,
        )

    def ctx(self, topic: str) -> MessageContext:
        ctx = MessageContext(
            topic=topic,
            partition=0,
            offset=len(self.contexts),
            key=None,
            attempt=1,
            traceparent=TRACEPARENT,
            timing=StageRecorder(
                service=None, attempt=1, received_at=datetime.fromtimestamp(self.clock(), UTC)
            ),
        )
        self.contexts.append(ctx)
        return ctx

    async def boundary(self, file: ChunkTracks, message: ChunkBoundary | None = None) -> None:
        store_chunk(self.storage, file)
        msg = message or boundary_message(file, duration_ms=_duration(file))
        await self.service.on_chunk_boundary(msg, self.ctx("chunk.boundary"))

    async def embedded(self, vectors: VectorFile) -> None:
        store_vectors(self.storage, vectors)
        await self.service.on_segment_embedded(
            embedded_message(vectors), self.ctx("segment.embedded")
        )


def _duration(file: ChunkTracks) -> int:
    # crossing_chunks·chunk_tracks 기본값은 5청크(600초) 영상이다.
    return 5 * CHUNK_MS


@pytest.fixture
def h() -> Harness:
    return Harness()


# R-1, R-2 ------------------------------------------------------------------


async def test_spec022_r1_boundary_is_recorded_with_ranges_and_execution(h: Harness) -> None:
    [first, *_] = crossing_chunks()

    await h.boundary(first)

    snapshot = h.store.snapshot(KEY)
    assert snapshot is not None
    [record] = snapshot.chunks
    assert record.chunk_id == first.chunk_id
    assert record.execution_id == first.execution_id
    assert record.owned_range == first.owned_range
    assert record.processing_range == first.processing_range
    assert record.tracks_key == tracks_key(first)
    assert snapshot.meta.source_duration_ms == 5 * CHUNK_MS


async def test_spec022_r2_first_execution_is_kept(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    [first, *_] = crossing_chunks()
    [retried, *_] = crossing_chunks(attempt=1)

    await h.boundary(first)
    with caplog.at_level(logging.WARNING):
        await h.boundary(first)
        assert not _ours(caplog)
        await h.boundary(retried)

    snapshot = h.store.snapshot(KEY)
    assert snapshot is not None
    assert [c.execution_id for c in snapshot.chunks] == [first.execution_id]
    assert [r.message for r in _ours(caplog)] == ["chunk already recorded with another execution"]


# R-3~R-6 -------------------------------------------------------------------


async def test_spec022_r5_adjacent_pair_publishes_one_stitched_event(h: Harness) -> None:
    a, b, *_ = crossing_chunks()

    await h.boundary(a)
    assert h.publisher.detected() == []
    await h.boundary(b)

    [event] = h.publisher.detected()
    assert event_errors(event) == []
    assert event.event_id == ids.track_detected_event_id(JOB, a.chunk_id, b.chunk_id)
    assert (event.origin, event.earlier_chunk_id, event.later_chunk_id) == (
        "stitcher",
        a.chunk_id,
        b.chunk_id,
    )
    [stitched] = event.stitched or []
    assert [m.track_local_id for m in stitched.members] == ["p", "p"]
    assert [m.chunk_id for m in stitched.members] == [a.chunk_id, b.chunk_id]
    assert stitched.start_ms == a.tracks[0].start_ms
    assert stitched.end_ms == b.tracks[0].end_ms
    [(topic, _, key, trace)] = h.publisher.published
    assert (topic, key, trace) == ("track.detected", str(SOURCE), TRACEPARENT)


async def test_spec022_r5_us1_ac3_order_does_not_change_event(h: Harness) -> None:
    a, b, *_ = crossing_chunks()
    other = Harness()

    await h.boundary(a)
    await h.boundary(b)
    await other.boundary(b)
    await other.boundary(a)

    assert h.publisher.detected() == other.publisher.detected()


async def test_spec022_r5_pair_without_links_publishes_empty_stitched(h: Harness) -> None:
    a = chunk_tracks(0, [track("x", 0, 124_000, tail=boxes(115_000, 125_000))])
    b = chunk_tracks(
        1, [track("y", 115_000, 200_000, class_name="forklift", head=boxes(115_000, 125_000))]
    )

    await h.boundary(a)
    await h.boundary(b)

    [event] = h.publisher.detected()
    assert event.stitched == []
    assert event_errors(event) == []


async def test_spec022_r3_non_adjacent_chunks_publish_nothing(h: Harness) -> None:
    a, _, c, *_ = crossing_chunks()

    await h.boundary(a)
    await h.boundary(c)

    assert h.publisher.published == []


async def test_spec022_r5_middle_chunk_publishes_both_pairs(h: Harness) -> None:
    a, b, c, *_ = crossing_chunks()

    await h.boundary(a)
    await h.boundary(c)
    await h.boundary(b)

    pairs = [(e.earlier_chunk_id, e.later_chunk_id) for e in h.publisher.detected()]
    assert pairs == [(a.chunk_id, b.chunk_id), (b.chunk_id, c.chunk_id)]


# R-18 ----------------------------------------------------------------------


async def test_spec022_r18_missing_tracks_file_is_not_recorded(h: Harness) -> None:
    [first, *_] = crossing_chunks()

    with pytest.raises(NonRetryableError):
        await h.service.on_chunk_boundary(boundary_message(first), h.ctx("chunk.boundary"))

    assert h.store.snapshot(KEY) is None


async def test_spec022_r18_invalid_tracks_file_is_not_recorded(h: Harness) -> None:
    [first, *_] = crossing_chunks()
    h.storage.put_bytes(tracks_key(first), b'{"schemaVersion": 1}', "application/json")

    with pytest.raises(NonRetryableError):
        await h.service.on_chunk_boundary(boundary_message(first), h.ctx("chunk.boundary"))
    h.storage.put_bytes(tracks_key(first), b"not json", "application/json")
    with pytest.raises(NonRetryableError):
        await h.service.on_chunk_boundary(boundary_message(first), h.ctx("chunk.boundary"))

    assert h.store.snapshot(KEY) is None


async def test_spec022_r18_tracks_file_must_match_message(h: Harness) -> None:
    [first, *_] = crossing_chunks()
    shifted = first.model_copy(update={"owned_range": first.processing_range})
    message = boundary_message(first)
    h.storage.put_bytes(
        tracks_key(first), shifted.model_dump_json(by_alias=True).encode(), "application/json"
    )

    with pytest.raises(NonRetryableError, match="does not match"):
        await h.service.on_chunk_boundary(message, h.ctx("chunk.boundary"))

    assert h.store.snapshot(KEY) is None


async def test_spec022_r18_vectors_key_must_be_in_execution_folder(h: Harness) -> None:
    a, b, *_ = crossing_chunks()
    message = embedded_message(vector_file(a))
    wrong = message.model_copy(update={"vectors_key": embedded_message(vector_file(b)).vectors_key})

    with pytest.raises(NonRetryableError, match="execution folder"):
        await h.service.on_segment_embedded(wrong, h.ctx("segment.embedded"))


# R-7, R-8, R-12, R-13 --------------------------------------------------------


async def _full(h: Harness, files: list[ChunkTracks], order: list[object] | None = None) -> None:
    steps: list[ChunkTracks | VectorFile] = [*files, *(vector_file(f) for f in files)]
    for step in order or steps:
        if isinstance(step, ChunkTracks):
            await h.boundary(step)
        elif isinstance(step, VectorFile):
            await h.embedded(step)


async def test_spec022_r7_vectors_before_boundary_are_recorded(h: Harness) -> None:
    [first, *_] = crossing_chunks()

    await h.embedded(vector_file(first))

    snapshot = h.store.snapshot(KEY)
    assert snapshot is not None
    assert [(v.execution_id, v.embedding_model) for v in snapshot.vectors] == [
        (first.execution_id, EMBEDDING_MODEL)
    ]
    assert snapshot.chunks == ()


async def test_spec022_r8_r12_us3_complete_job_publishes_one_manifest(h: Harness) -> None:
    files = crossing_chunks()

    await _full(h, files)

    [manifest] = h.publisher.manifests()
    assert event_errors(manifest) == []
    assert manifest.event_id == ids.track_manifest_event_id(JOB)
    assert manifest.complete is True
    assert manifest.missing_ranges == []
    assert (manifest.chunk_count, manifest.local_track_count, manifest.track_count) == (5, 5, 1)
    assert (manifest.boundary_link_count, manifest.reid_link_count) == (4, 0)
    assert manifest.reid_skipped_reason is None
    assert manifest.embedding_model == EMBEDDING_MODEL
    assert manifest.mapping_key == MAPPING_KEY
    assert manifest.stitch_profile == PROFILE.version
    assert len(h.publisher.detected()) == 4
    raw = h.storage.get_bytes(MAPPING_KEY)
    assert mapping_errors(raw) == []


async def test_spec022_r8_not_finalized_until_all_vectors(h: Harness) -> None:
    files = crossing_chunks()

    await _full(h, files, [*files, *(vector_file(f) for f in files[:4])])

    assert h.publisher.manifests() == []
    await h.embedded(vector_file(files[4]))
    assert len(h.publisher.manifests()) == 1


async def test_spec022_r8_last_message_can_be_boundary(h: Harness) -> None:
    files = crossing_chunks()

    await _full(h, files, [*(vector_file(f) for f in files), *files])

    assert len(h.publisher.manifests()) == 1


async def test_spec022_r9_reappearance_is_joined_in_mapping(h: Harness) -> None:
    files = crossing_chunks()
    gone = track("gone", 10_000, 20_000)
    back = track("back", 50_000, 60_000)
    first = files[0].model_copy(update={"tracks": [*files[0].tracks, gone, back]})
    files[0] = first
    vectors = [vector_file(f) for f in files]
    vectors[0] = vector_file(
        first,
        {"p": [unit(0, 0, 1, 0)], "gone": [unit(1, 0, 0, 0)], "back": [unit(1, 0.1, 0, 0)]},
    )
    vectors[1:] = [vector_file(f, {"p": [unit(0, 0, 1, 0)]}) for f in files[1:]]

    await _full(h, files, [*files, *vectors])

    [manifest] = h.publisher.manifests()
    assert manifest.reid_link_count == 1
    mapping = json.loads(h.storage.get_bytes(MAPPING_KEY))
    joined = [t for t in mapping["tracks"] if len(t["members"]) == 2]
    assert [[m["trackLocalId"] for m in t["members"]] for t in joined] == [["gone", "back"]]
    [reid] = [x for x in mapping["links"] if x["kind"] == "reid"]
    assert reid["gapMs"] == 30_000


async def test_spec022_r12_us3_ac2_shuffled_redelivery_gives_same_result(h: Harness) -> None:
    files = crossing_chunks()
    steps: list[object] = [*files, *(vector_file(f) for f in files)]
    other = Harness()
    # 10단계를 7씩 건너뛰며 고른 고정 순서다(경계·벡터 메시지가 섞인다).
    shuffled = [steps[(i * 7) % len(steps)] for i in range(len(steps))]

    await _full(h, files, steps)
    await _full(other, files, shuffled)

    assert h.publisher.manifests() == other.publisher.manifests()
    assert h.storage.get_bytes(MAPPING_KEY) == other.storage.get_bytes(MAPPING_KEY)
    assert sorted(e.event_id for e in h.publisher.detected()) == sorted(
        e.event_id for e in other.publisher.detected()
    )


class _FailingPut(InMemoryStorage):
    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if key.endswith("mapping.json"):
            raise OSError("storage unavailable")
        super().put_bytes(key, data, content_type)


async def test_spec022_r12_r20_mapping_save_failure_publishes_no_manifest() -> None:
    h = Harness(storage=_FailingPut())
    files = crossing_chunks()

    with pytest.raises(OSError, match="storage"):
        await _full(h, files)

    assert h.publisher.manifests() == []
    # 다시 처리하면 확정을 다시 시도할 수 있다(확정 임대를 풀어 둔다).
    h.storage = InMemoryStorage()
    for f in files:
        store_chunk(h.storage, f)
    await h.embedded(vector_file(files[-1]))
    assert len(h.publisher.manifests()) == 1


async def test_spec022_r11_mixed_embedding_models_skip_reid(h: Harness) -> None:
    files = crossing_chunks()
    vectors = [vector_file(f) for f in files]
    vectors[2] = vector_file(files[2], model=OTHER_MODEL)

    await _full(h, files, [*files, *vectors])

    [manifest] = h.publisher.manifests()
    assert manifest.reid_skipped_reason == "embedding_model_mismatch"
    assert manifest.embedding_model is None
    assert manifest.reid_link_count == 0
    assert json.loads(h.storage.get_bytes(MAPPING_KEY))["embeddingModel"] is None


async def test_spec022_r10_unreadable_vector_file_only_drops_that_chunk(h: Harness) -> None:
    files = crossing_chunks()
    vectors = [vector_file(f) for f in files]

    for f in files:
        await h.boundary(f)
    for v in vectors[:4]:
        await h.embedded(v)
    # 마지막 실행의 벡터 파일은 없지만 메시지는 왔다.
    await h.service.on_segment_embedded(embedded_message(vectors[4]), h.ctx("segment.embedded"))

    [manifest] = h.publisher.manifests()
    assert manifest.reid_skipped_reason is None
    assert manifest.embedding_model == EMBEDDING_MODEL


# R-17, R-19, R-20, R-21, R-23 -----------------------------------------------


async def test_spec022_r17_input_after_manifest_is_ignored(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    files = crossing_chunks()
    await _full(h, files)
    published = len(h.publisher.published)

    with caplog.at_level(logging.WARNING):
        await h.boundary(files[0])
        await h.embedded(vector_file(files[0]))

    assert len(h.publisher.published) == published
    assert [r.message for r in _ours(caplog)] == [
        "input after job finalized",
        "input after job finalized",
    ]


async def test_spec022_r19_video_deleted_clears_state_and_blocks_input(h: Harness) -> None:
    a, b, *_ = crossing_chunks()
    await h.boundary(a)

    await h.service.on_video_deleted(deleted_message(), h.ctx("video.deleted"))
    assert h.store.snapshot(KEY) is None
    await h.boundary(b)
    await h.embedded(vector_file(b))

    assert h.store.snapshot(KEY) is None
    assert h.publisher.published == []


async def test_spec022_r20_publish_failure_is_raised_and_retry_republishes(h: Harness) -> None:
    a, b, *_ = crossing_chunks()
    await h.boundary(a)
    h.publisher.fail = True

    with pytest.raises(PublishError):
        await h.boundary(b)
    h.publisher.fail = False
    await h.boundary(b)

    [event] = h.publisher.detected()
    assert event.event_id == ids.track_detected_event_id(JOB, a.chunk_id, b.chunk_id)


async def test_spec022_r21_stage_blocks_have_counts(h: Harness) -> None:
    files = crossing_chunks()

    await _full(h, files)

    timings = [t for c in h.contexts for t in c.timing.timings]
    boundary = [t for t in timings if t.stage_id == "stitcher.boundary"]
    assert [t.count for t in boundary] == [1, 1, 1, 1]
    [reid] = [t for t in timings if t.stage_id == "stitcher.reid"]
    [final] = [t for t in timings if t.stage_id == "stitcher.finalize"]
    assert reid.count == 1  # 경계 연결 뒤 트랙 하나
    assert final.count == 1
    assert reid.path == "stitcher.finalize/stitcher.reid"


class _CountingStore(MemoryJobStateStore):
    """기록 호출 수를 센다(R-23: 실시간 창은 상태 저장소를 건드리지 않는다)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def record_chunk(
        self, key: JobKey, meta: JobMeta, chunk: ChunkRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        self.calls += 1
        return super().record_chunk(key, meta, chunk, now_ms=now_ms, wait_ms=wait_ms)

    def record_vectors(
        self, key: JobKey, meta: JobMeta, vectors: VectorRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        self.calls += 1
        return super().record_vectors(key, meta, vectors, now_ms=now_ms, wait_ms=wait_ms)


async def test_spec022_r23_stream_jobs_are_skipped_without_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _CountingStore()
    h = Harness(store=store)
    [first, *_] = crossing_chunks()
    stream = boundary_message(first, job_type="STREAM")
    vectors = embedded_message(vector_file(first)).model_copy(update={"job_type": "STREAM"})

    with caplog.at_level(logging.INFO):
        # 파일이 없어도 읽지 않으므로 DLQ로 가지 않는다.
        await h.service.on_chunk_boundary(stream, h.ctx("chunk.boundary"))
        await h.service.on_segment_embedded(vectors, h.ctx("segment.embedded"))

    assert store.calls == 0
    assert h.publisher.published == []
    assert h.skipped.total() == 2
    assert [r.message for r in _ours(caplog)] == ["stitcher.skipped_stream"] * 2


async def test_spec022_r23_missing_job_type_is_full(h: Harness) -> None:
    a, b, *_ = crossing_chunks()

    await h.boundary(a, boundary_message(a, job_type=None))
    await h.boundary(b, boundary_message(b, job_type="FULL"))

    assert len(h.publisher.detected()) == 1


# NFR-4 ----------------------------------------------------------------------


def _extras(record: logging.LogRecord) -> Iterator[object]:
    standard = vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    for key, value in vars(record).items():
        if key not in standard and key not in {"message", "asctime", "taskName"}:
            yield value


async def test_spec022_nfr4_logs_have_no_coordinates_or_vectors(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    files = crossing_chunks()
    [retried, *_] = crossing_chunks(attempt=1)

    with caplog.at_level(logging.DEBUG):
        await _full(h, files)
        await h.boundary(retried)

    assert _ours(caplog)
    for record in _ours(caplog):
        text = json.dumps([record.getMessage(), *_extras(record)], default=str)
        assert "box" not in text.lower()
        assert "vector" not in record.getMessage().lower()
        for value in _extras(record):
            assert not isinstance(value, list | dict), (record.getMessage(), value)


def _ours(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # 같은 프로세스의 다른 통합 테스트 워커 스레드가 남기는 로그를 빼고 이 서비스 로그만 본다.
    return [r for r in caplog.records if r.name.startswith("track_stitcher")]
