"""대기 제한 시간 주기 확인(SPEC-022 R-15, R-16, R-21)."""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from stitcher_support import (
    CHUNK_MS,
    JOB,
    PROFILE,
    SOURCE,
    Clock,
    boundary_message,
    crossing_chunks,
    embedded_message,
    event_errors,
    mapping_errors,
    store_chunk,
    store_vectors,
    vector_file,
)
from track_stitcher.service import StitcherService
from track_stitcher.state import JobKey, MemoryJobStateStore
from track_stitcher.sweeper import Sweeper
from vlm_contracts.events import Event, TimeRange, TrackManifest
from vlm_contracts.stages import StageTimingBatch
from vlm_contracts.tracks import ChunkTracks
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage

WAIT_S = PROFILE.finalize.wait_timeout_s
MAPPING_KEY = f"sources/{SOURCE}/jobs/{JOB}/tracks/mapping.json"


@dataclass
class _Publisher:
    published: list[Event] = field(default_factory=list)

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.published.append(event)

    def manifests(self) -> list[TrackManifest]:
        return [e for e in self.published if isinstance(e, TrackManifest)]


@dataclass
class _Sink:
    batches: list[StageTimingBatch] = field(default_factory=list)

    def emit(self, batch: StageTimingBatch, *, traceparent: str | None) -> None:
        self.batches.append(batch)


@dataclass
class _Env:
    storage: InMemoryStorage = field(default_factory=InMemoryStorage)
    store: MemoryJobStateStore = field(default_factory=MemoryJobStateStore)
    publisher: _Publisher = field(default_factory=_Publisher)
    clock: Clock = field(default_factory=Clock)
    sink: _Sink = field(default_factory=_Sink)

    def service(self) -> StitcherService:
        return StitcherService(
            self.store, self.storage, self.publisher, PROFILE, lease_s=60, clock=self.clock
        )

    def sweeper(self) -> Sweeper:
        return Sweeper(
            self.store, self.service(), interval_s=0.01, timing=self.sink, clock=self.clock
        )

    def ctx(self, topic: str) -> MessageContext:
        now = datetime.fromtimestamp(self.clock(), UTC)
        return MessageContext(
            topic, 0, 0, None, 1, None, StageRecorder(service=None, attempt=1, received_at=now)
        )

    async def boundary(self, file: ChunkTracks) -> None:
        store_chunk(self.storage, file)
        await self.service().on_chunk_boundary(boundary_message(file), self.ctx("chunk.boundary"))

    async def embedded(self, file: ChunkTracks) -> None:
        vectors = vector_file(file)
        store_vectors(self.storage, vectors)
        await self.service().on_segment_embedded(
            embedded_message(vectors), self.ctx("segment.embedded")
        )


@pytest.fixture
def env() -> _Env:
    return _Env()


async def test_spec022_r15_timeout_finalizes_with_received_chunks(env: _Env) -> None:
    files = crossing_chunks()
    for f in (files[0], files[1], files[3]):
        await env.boundary(f)
    await env.embedded(files[0])

    env.clock.advance(WAIT_S - 1)
    assert env.sweeper().sweep_once() == 0
    env.clock.advance(1)
    assert env.sweeper().sweep_once() == 1

    [manifest] = env.publisher.manifests()
    assert event_errors(manifest) == []
    assert manifest.complete is False
    assert manifest.missing_ranges == [
        TimeRange(start_ms=2 * CHUNK_MS, end_ms=3 * CHUNK_MS),
        TimeRange(start_ms=4 * CHUNK_MS, end_ms=5 * CHUNK_MS),
    ]
    assert manifest.chunk_count == 3
    # 청크 0·1은 경계로 이어지고 청크 3은 혼자다.
    assert (manifest.boundary_link_count, manifest.track_count) == (1, 2)
    assert mapping_errors(env.storage.get_bytes(MAPPING_KEY)) == []
    # 다시 돌아도 같은 Job을 또 발행하지 않는다.
    env.clock.advance(WAIT_S)
    assert env.sweeper().sweep_once() == 0
    assert len(env.publisher.manifests()) == 1


async def test_spec022_r15_new_input_resets_wait(env: _Env) -> None:
    files = crossing_chunks()
    await env.boundary(files[0])
    env.clock.advance(WAIT_S - 10)
    await env.boundary(files[1])
    env.clock.advance(10)

    assert env.sweeper().sweep_once() == 0
    env.clock.advance(WAIT_S)
    assert env.sweeper().sweep_once() == 1


async def test_spec022_r15_job_without_boundary_is_dropped_with_warning(
    env: _Env, caplog: pytest.LogCaptureFixture
) -> None:
    [first, *_] = crossing_chunks()
    await env.embedded(first)
    env.clock.advance(WAIT_S)

    with caplog.at_level(logging.WARNING):
        assert env.sweeper().sweep_once() == 0
        assert env.sweeper().sweep_once() == 0

    assert env.publisher.published == []
    assert [r.message for r in caplog.records] == ["job timed out without chunk boundary"]


async def test_spec022_r21_sweep_records_stage_timing_outside_handler(env: _Env) -> None:
    files = crossing_chunks()
    await env.boundary(files[0])
    await env.embedded(files[0])
    env.clock.advance(WAIT_S)

    env.sweeper().sweep_once()

    [batch] = env.sink.batches
    stages = {t.stage_id: t for t in batch.timings}
    assert set(stages) == {"stitcher.finalize", "stitcher.reid"}
    assert stages["stitcher.finalize"].count == 1
    assert stages["stitcher.finalize"].subject.source_id == SOURCE
    assert batch.service == "track-stitcher"


class _FlakyStorage(InMemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if self.broken and key.endswith("mapping.json"):
            raise OSError("storage unavailable")
        super().put_bytes(key, data, content_type)


async def test_spec022_r15_failed_job_is_retried_next_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _FlakyStorage()
    env = _Env(storage=storage)
    await env.boundary(crossing_chunks()[0])
    env.clock.advance(WAIT_S)

    with caplog.at_level(logging.ERROR):
        assert env.sweeper().sweep_once() == 0
    assert [r.message for r in caplog.records] == ["sweep finalize failed"]
    storage.broken = False
    env.clock.advance(1)
    assert env.sweeper().sweep_once() == 1


async def test_spec022_r16_lease_blocks_second_finalizer(env: _Env) -> None:
    await env.boundary(crossing_chunks()[0])
    env.clock.advance(WAIT_S)
    key = JobKey(SOURCE, JOB)

    # 다른 Pod가 먼저 임대를 얻었다.
    assert env.store.claim(key, now_ms=int(env.clock() * 1000), lease_ms=60_000) is not None
    assert env.sweeper().sweep_once() == 0
    # 그 Pod가 죽어 임대가 끝나면 이어 받는다.
    env.clock.advance(61)
    assert env.sweeper().sweep_once() == 1


async def test_sweeper_run_stops_on_event(env: _Env) -> None:
    await env.boundary(crossing_chunks()[0])
    env.clock.advance(WAIT_S)
    stop = threading.Event()
    thread = threading.Thread(target=env.sweeper().run, args=(stop,), daemon=True)

    thread.start()
    for _ in range(200):
        if env.publisher.manifests():
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(env.publisher.manifests()) == 1
    assert json.loads(env.storage.get_bytes(MAPPING_KEY))["jobId"] == str(JOB)
