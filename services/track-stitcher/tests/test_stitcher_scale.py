"""1시간 영상 확정 시간(SPEC-022 NFR-1).

합성 데이터: 청크 30개, 로컬 트랙 3,000개, 크롭 벡터 9,000개(512차원). 확정은 저장소에서 파일 60개를
읽어 검증하고, 경계 연결·Re-ID·번역표 저장·발행까지 한다.
"""

import math
import time
from datetime import UTC, datetime

from stitcher_support import (
    CHUNK_MS,
    NORMALIZATION,
    OVERLAP_MS,
    OWNER,
    PROFILE,
    SOURCE,
    boxes,
    chunk_tracks,
    store_chunk,
    store_vectors,
    track,
    tracks_key,
    vector_file,
    vectors_key,
)
from track_stitcher.finalize import Finalizer
from track_stitcher.state import ChunkRecord, JobKey, JobMeta, JobSnapshot, VectorRecord
from vlm_contracts.events import Event
from vlm_contracts.tracks import ChunkTrack
from vlm_messaging.timing import StageRecorder
from vlm_storage.memory import InMemoryStorage

CHUNKS = 30
TRACKS_PER_CHUNK = 100
CROPS_PER_TRACK = 3
DIMENSION = 512
BOUNDARY_TRACKS = 10
LIMIT_S = 30.0


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.events.append(event)


def _vector(seed: int) -> list[float]:
    # 트랙마다 방향이 다른 결정적 벡터(유사도 계산량만 재므로 값의 의미는 없다).
    raw = [math.sin(seed * 0.7 + i * 1.3) for i in range(DIMENSION)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def _tracks(index: int, duration: int) -> list[ChunkTrack]:
    start = index * CHUNK_MS
    end = min(start + CHUNK_MS, duration)
    result: list[ChunkTrack] = []
    for j in range(TRACKS_PER_CHUNK - 2 * BOUNDARY_TRACKS):
        t0 = start + (j * 1_400) % (CHUNK_MS - 15_000)
        result.append(track(f"t{j}", t0, t0 + 10_000))
    for j in range(BOUNDARY_TRACKS):
        box = (0.02 + j * 0.09, 0.2, 0.08 + j * 0.09, 0.8)
        head = boxes(start - OVERLAP_MS, start + OVERLAP_MS, box, 100) if index > 0 else []
        tail = boxes(end - OVERLAP_MS, end + OVERLAP_MS, box, 100) if end < duration else []
        result.append(track(f"h{j}", max(start - OVERLAP_MS, 0), start + 20_000, head=head))
        result.append(track(f"e{j}", end - 20_000, min(end + OVERLAP_MS, duration) - 1, tail=tail))
    return result


def test_spec022_nfr1_one_hour_video_finalizes_within_30s() -> None:
    duration = CHUNKS * CHUNK_MS
    storage = InMemoryStorage()
    key = JobKey(SOURCE, chunk_tracks(0).job_id)
    chunks: list[ChunkRecord] = []
    vectors: list[VectorRecord] = []
    seed = 0
    for i in range(CHUNKS):
        file = chunk_tracks(i, _tracks(i, duration), duration_ms=duration)
        crops: dict[str, list[list[float]]] = {}
        for t in file.tracks:
            seed += 1
            crops[t.track_local_id] = [_vector(seed)] * CROPS_PER_TRACK
        vf = vector_file(file, crops)
        store_chunk(storage, file)
        store_vectors(storage, vf)
        chunks.append(
            ChunkRecord(
                file.chunk_id,
                file.execution_id,
                file.owned_range,
                file.processing_range,
                tracks_key(file),
                file.detector_profile,
            )
        )
        vectors.append(VectorRecord(file.execution_id, vectors_key(vf), vf.embedding_model))
    snapshot = JobSnapshot(
        key=key,
        status="finalizing",
        meta=JobMeta(OWNER, NORMALIZATION, duration),
        chunks=tuple(chunks),
        vectors=tuple(vectors),
    )
    publisher = _Publisher()
    finalizer = Finalizer(storage, publisher, PROFILE, lambda: datetime.now(UTC))
    timing = StageRecorder(service=None, attempt=1, received_at=datetime.now(UTC))

    started = time.perf_counter()
    manifest = finalizer.finalize(snapshot, timing)
    elapsed = time.perf_counter() - started

    assert manifest is not None
    assert manifest.local_track_count == CHUNKS * TRACKS_PER_CHUNK
    assert manifest.boundary_link_count == (CHUNKS - 1) * BOUNDARY_TRACKS
    reid = next(t for t in timing.timings if t.stage_id == "stitcher.reid")
    assert reid.count == manifest.local_track_count - manifest.boundary_link_count
    assert elapsed < LIMIT_S, f"finalize took {elapsed:.1f}s"
