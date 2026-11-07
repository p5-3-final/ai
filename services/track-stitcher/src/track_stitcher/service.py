"""track-stitcher 핸들러: `chunk.boundary`·`segment.embedded`·`video.deleted`(SPEC-022).

핸들러 본문은 블로킹(저장소·상태 저장소·발행)이라 `asyncio.to_thread`로 워커 이벤트 루프 밖에서
돈다. 저장소·상태 저장소·발행 오류는 잡지 않고 던져 워커가 재시도 토픽으로 보낸다(R-20).
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from vlm_contracts.events import (
    ChunkBoundary,
    SegmentEmbedded,
    StitchedTrack,
    TrackDetected,
    VideoDeleted,
)
from vlm_contracts.tracks import ChunkTracks
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectStorage

from track_stitcher import files, ids
from track_stitcher.boundary import BoundaryLink, adjacent_pairs, link_pair
from track_stitcher.finalize import Finalizer
from track_stitcher.profile import StitchProfile
from track_stitcher.publishing import EventPublisher
from track_stitcher.state import (
    ChunkRecord,
    JobKey,
    JobMeta,
    JobSnapshot,
    JobStateStore,
    RecordResult,
    VectorRecord,
)

TRACK_DETECTED_TOPIC = "track.detected"

type _Input = ChunkBoundary | SegmentEmbedded

logger = logging.getLogger(__name__)


class SkipCounter:
    """실시간 창(`jobType: STREAM`)으로 건너뛴 메시지 수(R-23). 토픽별로 센다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def increment(self, topic: str) -> int:
        """`topic`의 수를 하나 올리고 전체 수를 돌려준다."""
        with self._lock:
            self._counts[topic] = self._counts.get(topic, 0) + 1
            return sum(self._counts.values())

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())


class StitcherService:
    """청크 트랙 요약과 크롭 벡터를 Job별로 모아 경계 연결·Re-ID·확정을 한다.

    Args:
        store: Job 대기 상태 저장소.
        storage: 트랙·벡터 파일을 읽고 번역표를 쓰는 저장소.
        publisher: `track.detected`·`track.manifest` 발행기.
        profile: 기준값 프로필.
        lease_s: 확정 임대 시간(R-16). 확정 한 번보다 넉넉히 길어야 두 Pod가 함께 확정하지 않는다.
        clock: epoch 초 시계. 테스트가 바꾼다.
        skipped: 실시간 창으로 건너뛴 수(R-23).
    """

    def __init__(
        self,
        store: JobStateStore,
        storage: ObjectStorage,
        publisher: EventPublisher,
        profile: StitchProfile,
        *,
        lease_s: float,
        clock: Callable[[], float] = time.time,
        skipped: SkipCounter | None = None,
    ) -> None:
        self._store = store
        self._storage = storage
        self._publisher = publisher
        self._profile = profile
        self._lease_ms = int(lease_s * 1000)
        self._wait_ms = profile.finalize.wait_timeout_s * 1000
        self._clock = clock
        self.skipped = skipped or SkipCounter()
        self._finalizer = Finalizer(storage, publisher, profile, self._now)

    async def on_chunk_boundary(self, event: ChunkBoundary, ctx: MessageContext) -> None:
        """청크 트랙 요약을 기록하고 인접 청크가 있으면 경계를 잇는다(R-1~R-6)."""
        await asyncio.to_thread(self._chunk_boundary, event, ctx)

    async def on_segment_embedded(self, event: SegmentEmbedded, ctx: MessageContext) -> None:
        """실행의 크롭 벡터 파일 경로를 기록한다(R-7)."""
        await asyncio.to_thread(self._segment_embedded, event, ctx)

    async def on_video_deleted(self, event: VideoDeleted, ctx: MessageContext) -> None:
        """영상의 대기 상태를 지운다(R-19)."""
        await asyncio.to_thread(self._video_deleted, event)

    def finalize_due(self, key: JobKey, timing: StageRecorder) -> bool:
        """대기 제한 시간이 지난 Job을 받은 입력만으로 확정한다(R-15). 확정했으면 True.

        임대를 얻지 못하면(다른 Pod가 확정 중이거나 끝냄) 아무것도 하지 않는다(R-16). 청크를
        하나도 받지 못한 Job은 영상 길이·정규화 ID를 몰라 manifest를 만들 수 없으므로 발행하지
        않고 경고만 남긴 뒤 주기 확인 대상에서 뺀다(plan 8절 6번).
        """
        claimed = self._store.claim(key, now_ms=self._now_ms(), lease_ms=self._lease_ms)
        if claimed is None:
            return False
        if not claimed.chunks or claimed.meta.source_duration_ms is None:
            logger.warning("job timed out without chunk boundary", extra=_job_fields(key))
            self._store.release(key, retry_at_ms=None)
            return False
        return self._finalize(claimed, timing)

    def _chunk_boundary(self, event: ChunkBoundary, ctx: MessageContext) -> None:
        if self._skip_stream(event, ctx.topic):
            return
        tracks = files.read_message_tracks(self._storage, event)
        key = JobKey(event.source_id, event.job_id)
        meta = JobMeta(
            event.owner_id, event.normalization_id, event.source_duration_ms, ctx.traceparent
        )
        record = ChunkRecord(
            chunk_id=event.chunk_id,
            execution_id=event.execution_id,
            owned_range=event.owned_range,
            processing_range=event.processing_range,
            tracks_key=event.tracks_key,
            detector_profile=event.detector_profile,
        )
        result = self._store.record_chunk(
            key, meta, record, now_ms=self._now_ms(), wait_ms=self._wait_ms
        )
        snapshot = self._accepted(result, event)
        if snapshot is None:
            return
        if result.outcome == "conflict":
            existing = snapshot.chunk(event.chunk_id)
            logger.warning(
                "chunk already recorded with another execution",
                extra={
                    **_message_fields(event),
                    "recordedExecutionId": str(existing.execution_id) if existing else None,
                },
            )
            return
        if snapshot.meta.source_duration_ms != event.source_duration_ms:
            # spec에 규칙이 없어 먼저 기록한 값을 쓴다(plan 8절 4번).
            logger.warning(
                "source duration differs from first recorded value",
                extra={
                    **_message_fields(event),
                    "recordedDurationMs": snapshot.meta.source_duration_ms,
                    "durationMs": event.source_duration_ms,
                },
            )
        self._publish_pairs(snapshot, tracks, event, ctx)
        self._finalize_if_ready(snapshot, ctx.timing)

    def _segment_embedded(self, event: SegmentEmbedded, ctx: MessageContext) -> None:
        if self._skip_stream(event, ctx.topic):
            return
        if event.vectors_key != event.execution_prefix() + "vectors.json":
            # R-18: 다른 영상·실행의 벡터 파일을 Re-ID에 섞지 않는다.
            raise NonRetryableError("vectorsKey is outside the execution folder of this message")
        key = JobKey(event.source_id, event.job_id)
        meta = JobMeta(event.owner_id, event.normalization_id, None, ctx.traceparent)
        record = VectorRecord(event.execution_id, event.vectors_key, event.embedding_model)
        result = self._store.record_vectors(
            key, meta, record, now_ms=self._now_ms(), wait_ms=self._wait_ms
        )
        snapshot = self._accepted(result, event)
        if snapshot is not None:
            self._finalize_if_ready(snapshot, ctx.timing)

    def _video_deleted(self, event: VideoDeleted) -> None:
        removed = self._store.delete_video(event.source_id)
        logger.info(
            "video state deleted", extra={"sourceId": str(event.source_id), "jobCount": removed}
        )

    def _skip_stream(self, event: _Input, topic: str) -> bool:
        # R-23: 실시간 창은 stream-ingestor가 세션 전체에서 같은 트랙 ID를 유지해 번역표가 없다.
        # 상태를 만들지 않으므로 같은 메시지를 다시 받아도 무해하다.
        if event.job_type != "STREAM":
            return False
        total = self.skipped.increment(topic)
        logger.info(
            "stitcher.skipped_stream",
            extra={**_message_fields(event), "topic": topic, "skippedTotal": total},
        )
        return True

    def _accepted(self, result: RecordResult, event: _Input) -> JobSnapshot | None:
        if result.outcome == "deleted":
            logger.info("input for deleted video ignored", extra=_message_fields(event))
            return None
        if result.outcome == "late":
            # R-17: 확정한(또는 확정 중인) Job은 다시 발행하지 않는다.
            logger.warning(
                "input after job finalized",
                extra={**_message_fields(event), "status": result.status},
            )
            return None
        return result.snapshot

    def _publish_pairs(
        self,
        snapshot: JobSnapshot,
        tracks: ChunkTracks,
        event: ChunkBoundary,
        ctx: MessageContext,
    ) -> None:
        pairs = [p for p in adjacent_pairs(snapshot.chunks) if event.chunk_id in _ids(p)]
        if not pairs:
            return
        with ctx.timing.stage("stitcher.boundary") as block:
            compared = 0
            for earlier, later in pairs:
                a = tracks if earlier.chunk_id == event.chunk_id else self._load(snapshot, earlier)
                b = tracks if later.chunk_id == event.chunk_id else self._load(snapshot, later)
                result = link_pair(a, b, self._profile.boundary)
                compared += result.compared
                self._publisher.publish(
                    TRACK_DETECTED_TOPIC,
                    self._pair_event(snapshot, event, a, b, result.links),
                    key=str(event.source_id),
                    traceparent=ctx.traceparent,
                )
            block.count = compared

    def _load(self, snapshot: JobSnapshot, chunk: ChunkRecord) -> ChunkTracks:
        return files.read_recorded_tracks(self._storage, snapshot.key, chunk)

    def _pair_event(
        self,
        snapshot: JobSnapshot,
        event: ChunkBoundary,
        earlier: ChunkTracks,
        later: ChunkTracks,
        links: tuple[BoundaryLink, ...],
    ) -> TrackDetected:
        job = snapshot.key.job_id
        stitched = [
            StitchedTrack(
                # 쌍 이벤트의 trackId는 두 구성원 중 이른 구성원으로 R-14와 같게 만든다. 여러
                # 경계를 거친 최종 ID는 track.manifest 번역표가 정한다(계약 4.1.0).
                track_id=ids.final_track_id(job, ids.earliest([x.earlier, x.later]).ref),
                class_name=x.earlier.class_name,
                start_ms=min(x.earlier.start_ms, x.later.start_ms),
                end_ms=max(x.earlier.end_ms, x.later.end_ms),
                members=[x.earlier.ref.member(), x.later.ref.member()],
            )
            for x in links
        ]
        return TrackDetected(
            event_id=ids.track_detected_event_id(job, earlier.chunk_id, later.chunk_id),
            occurred_at=self._now(),
            source_id=snapshot.key.source_id,
            owner_id=snapshot.meta.owner_id,
            job_type=event.job_type,
            job_id=job,
            normalization_id=snapshot.meta.normalization_id,
            origin="stitcher",
            earlier_chunk_id=earlier.chunk_id,
            later_chunk_id=later.chunk_id,
            stitched=stitched,
        )

    def _finalize_if_ready(self, snapshot: JobSnapshot, timing: StageRecorder) -> None:
        if not snapshot.ready():
            return
        claimed = self._store.claim(snapshot.key, now_ms=self._now_ms(), lease_ms=self._lease_ms)
        if claimed is not None:
            self._finalize(claimed, timing)

    def _finalize(self, claimed: JobSnapshot, timing: StageRecorder) -> bool:
        try:
            manifest = self._finalizer.finalize(claimed, timing)
        except Exception:
            # 임대를 풀어 재시도 메시지나 다음 주기 확인이 바로 다시 확정할 수 있게 한다. 풀지
            # 못해도 임대가 끝나면 주기 확인이 이어 받는다.
            try:
                self._store.release(claimed.key, retry_at_ms=self._now_ms())
            except Exception:
                logger.exception("finalize lease not released", extra=_job_fields(claimed.key))
            raise
        if manifest is None:
            self._store.release(claimed.key, retry_at_ms=None)
            return False
        self._store.finish(claimed.key)
        return True

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)


def _ids(pair: tuple[ChunkRecord, ChunkRecord]) -> tuple[object, object]:
    return pair[0].chunk_id, pair[1].chunk_id


def _job_fields(key: JobKey) -> dict[str, object]:
    return {"sourceId": str(key.source_id), "jobId": str(key.job_id)}


def _message_fields(event: _Input) -> dict[str, object]:
    return {
        "sourceId": str(event.source_id),
        "jobId": str(event.job_id),
        "chunkId": str(event.chunk_id),
        "executionId": str(event.execution_id),
    }
