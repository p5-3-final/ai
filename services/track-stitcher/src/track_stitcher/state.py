"""Job별 대기 상태(SPEC-022 R-1, R-2, R-7, R-15~R-17, R-19).

상태에는 파일 경로·범위 같은 색인만 두고 트랙 내용은 저장소 파일을 다시 읽는다(plan 4.2).
운영은 `redis_state.RedisJobStateStore`, 단위 테스트는 `MemoryJobStateStore`이고 두 구현은 같은
의미를 지킨다.
"""

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol
from uuid import UUID

from vlm_contracts.events import TimeRange

from track_stitcher.coverage import covers

JobStatus = Literal["open", "finalizing", "finalized"]
RecordOutcome = Literal["recorded", "duplicate", "conflict", "late", "deleted"]
"""기록 결과.

- recorded: 새로 기록했다.
- duplicate: 같은 실행을 이미 기록했다(같은 메시지를 다시 받음).
- conflict: 같은 청크를 다른 실행으로 이미 기록했다. 먼저 기록한 실행을 유지한다(R-2).
- late: Job이 확정 중이거나 확정됐다. 기록하지 않는다(R-17).
- deleted: 영상이 삭제됐다. 기록하지 않는다(R-19).
"""


@dataclass(frozen=True, slots=True)
class JobKey:
    """Job 하나. 상태 키는 영상(sourceId) 단위로 묶는다."""

    source_id: UUID
    job_id: UUID


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """기록한 청크 실행 하나(`chunk.boundary`)."""

    chunk_id: UUID
    execution_id: UUID
    owned_range: TimeRange
    processing_range: TimeRange
    tracks_key: str
    detector_profile: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "chunkId": str(self.chunk_id),
                "executionId": str(self.execution_id),
                "ownedRange": self.owned_range.model_dump(by_alias=True),
                "processingRange": self.processing_range.model_dump(by_alias=True),
                "tracksKey": self.tracks_key,
                "detectorProfile": self.detector_profile,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "ChunkRecord":
        data = json.loads(raw)
        return cls(
            chunk_id=UUID(data["chunkId"]),
            execution_id=UUID(data["executionId"]),
            owned_range=TimeRange.model_validate(data["ownedRange"]),
            processing_range=TimeRange.model_validate(data["processingRange"]),
            tracks_key=data["tracksKey"],
            detector_profile=data["detectorProfile"],
        )


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """기록한 실행의 크롭 벡터 파일(`segment.embedded`)."""

    execution_id: UUID
    vectors_key: str
    embedding_model: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "executionId": str(self.execution_id),
                "vectorsKey": self.vectors_key,
                "embeddingModel": self.embedding_model,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "VectorRecord":
        data = json.loads(raw)
        return cls(UUID(data["executionId"]), data["vectorsKey"], data["embeddingModel"])


@dataclass(frozen=True, slots=True)
class JobMeta:
    """Job의 envelope 정보. 먼저 기록한 값을 유지한다(`traceparent`만 마지막 입력의 값).

    `source_duration_ms`는 `chunk.boundary`에만 있어 벡터만 받은 Job은 None이다.
    """

    owner_id: UUID
    normalization_id: UUID
    source_duration_ms: int | None = None
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """한 시점의 Job 상태. 청크는 소유 범위 시작 순서다."""

    key: JobKey
    status: JobStatus
    meta: JobMeta
    chunks: tuple[ChunkRecord, ...] = ()
    vectors: tuple[VectorRecord, ...] = ()

    def chunk(self, chunk_id: UUID) -> ChunkRecord | None:
        return next((c for c in self.chunks if c.chunk_id == chunk_id), None)

    def covered(self) -> bool:
        duration = self.meta.source_duration_ms
        return duration is not None and covers((c.owned_range for c in self.chunks), duration)

    def ready(self) -> bool:
        """소유 범위가 영상 전체를 덮고 기록한 모든 실행의 벡터가 모였다(R-8)."""
        embedded = {v.execution_id for v in self.vectors}
        return self.covered() and all(c.execution_id in embedded for c in self.chunks)


@dataclass(frozen=True, slots=True)
class RecordResult:
    """기록 결과. `late`·`deleted`면 `snapshot`이 None이다."""

    outcome: RecordOutcome
    snapshot: JobSnapshot | None = None
    status: JobStatus | None = None


class JobStateStore(Protocol):
    """Job 대기 상태 저장소. 모든 메서드는 원자적이고 블로킹이다(핸들러는 스레드에서 부른다).

    저장소가 응답하지 않으면 예외를 그대로 던진다. 워커가 재시도 토픽으로 보낸다(R-20).
    """

    def record_chunk(
        self, key: JobKey, meta: JobMeta, chunk: ChunkRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        """청크를 기록하고(같은 chunkId면 먼저 기록한 것 유지) 기록 뒤 상태를 돌려준다.

        새로 기록하면 마지막 입력 시각을 `now_ms`로 바꾸고, 대기 제한 시각을 마지막 입력 +
        `wait_ms`로 둔다(R-15).
        """
        ...

    def record_vectors(
        self, key: JobKey, meta: JobMeta, vectors: VectorRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        """벡터 파일을 기록한다(같은 executionId면 먼저 기록한 것 유지).

        나머지는 `record_chunk`와 같다.
        """
        ...

    def snapshot(self, key: JobKey) -> JobSnapshot | None:
        """지금 상태. 없으면 None."""
        ...

    def claim(self, key: JobKey, *, now_ms: int, lease_ms: int) -> JobSnapshot | None:
        """확정 임대를 얻는다(R-16). `open`이거나 임대가 끝난 `finalizing`일 때만 이기고, 이긴
        시점의 상태를 돌려준다. 그 뒤 들어온 입력은 `late`다. 임대 중 Pod가 죽어도 대기 제한 시각을
        임대 만료로 옮겨 두어 주기 확인이 다시 확정한다.
        """
        ...

    def release(self, key: JobKey, *, keep_due: bool) -> None:
        """확정을 포기하고 `open`으로 되돌린다. `keep_due`가 False면 주기 확인 대상에서 뺀다."""
        ...

    def finish(self, key: JobKey) -> None:
        """확정 완료(`finalized`). 주기 확인 대상에서 뺀다."""
        ...

    def due(self, now_ms: int, limit: int) -> list[JobKey]:
        """대기 제한 시각이 `now_ms` 이하인 Job(이른 순서, 최대 `limit`개)."""
        ...

    def delete_video(self, source_id: UUID) -> int:
        """영상의 모든 Job 상태를 지우고 삭제 표시를 남긴다(R-19). 지운 Job 수를 돌려준다."""
        ...


@dataclass
class _Job:
    meta: JobMeta
    status: JobStatus = "open"
    chunks: dict[UUID, ChunkRecord] = field(default_factory=dict)
    vectors: dict[UUID, VectorRecord] = field(default_factory=dict)
    last_input_ms: int = 0
    lease_until_ms: int = 0


class MemoryJobStateStore:
    """`JobStateStore`의 메모리 구현. 단위 테스트용이다(TTL은 두지 않는다)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[JobKey, _Job] = {}
        self._due: dict[JobKey, int] = {}
        self._deleted: set[UUID] = set()

    def record_chunk(
        self, key: JobKey, meta: JobMeta, chunk: ChunkRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        with self._lock:
            job, early = self._open(key, meta)
            if early is not None:
                return early
            existing = job.chunks.get(chunk.chunk_id)
            if existing is None:
                job.chunks[chunk.chunk_id] = chunk
                outcome: RecordOutcome = "recorded"
            else:
                same = existing.execution_id == chunk.execution_id
                outcome = "duplicate" if same else "conflict"
            return self._after_record(key, job, meta, outcome, now_ms, wait_ms)

    def record_vectors(
        self, key: JobKey, meta: JobMeta, vectors: VectorRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        with self._lock:
            job, early = self._open(key, meta)
            if early is not None:
                return early
            outcome: RecordOutcome = "duplicate"
            if vectors.execution_id not in job.vectors:
                job.vectors[vectors.execution_id] = vectors
                outcome = "recorded"
            return self._after_record(key, job, meta, outcome, now_ms, wait_ms)

    def snapshot(self, key: JobKey) -> JobSnapshot | None:
        with self._lock:
            job = self._jobs.get(key)
            return None if job is None else _snapshot(key, job)

    def claim(self, key: JobKey, *, now_ms: int, lease_ms: int) -> JobSnapshot | None:
        with self._lock:
            job = self._jobs.get(key)
            if job is None or job.status == "finalized":
                return None
            if job.status == "finalizing" and job.lease_until_ms > now_ms:
                return None
            job.status = "finalizing"
            job.lease_until_ms = now_ms + lease_ms
            self._due[key] = job.lease_until_ms
            return _snapshot(key, job)

    def release(self, key: JobKey, *, keep_due: bool) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is not None and job.status == "finalizing":
                job.status = "open"
            if not keep_due:
                self._due.pop(key, None)

    def finish(self, key: JobKey) -> None:
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                job.status = "finalized"
            self._due.pop(key, None)

    def due(self, now_ms: int, limit: int) -> list[JobKey]:
        with self._lock:
            ready = sorted(
                ((at, str(k.source_id), str(k.job_id), k) for k, at in self._due.items())
            )
            return [k for at, _, _, k in ready if at <= now_ms][:limit]

    def delete_video(self, source_id: UUID) -> int:
        with self._lock:
            self._deleted.add(source_id)
            keys = [k for k in self._jobs if k.source_id == source_id]
            for k in keys:
                del self._jobs[k]
                self._due.pop(k, None)
            return len(keys)

    def _open(self, key: JobKey, meta: JobMeta) -> tuple[_Job, RecordResult | None]:
        if key.source_id in self._deleted:
            return _Job(meta), RecordResult("deleted")
        job = self._jobs.setdefault(key, _Job(meta=replace(meta, traceparent=None)))
        if job.status != "open":
            return job, RecordResult("late", status=job.status)
        return job, None

    def _after_record(
        self,
        key: JobKey,
        job: _Job,
        meta: JobMeta,
        outcome: RecordOutcome,
        now_ms: int,
        wait_ms: int,
    ) -> RecordResult:
        if job.meta.source_duration_ms is None and meta.source_duration_ms is not None:
            job.meta = replace(job.meta, source_duration_ms=meta.source_duration_ms)
        if outcome == "recorded":
            job.last_input_ms = now_ms
            job.meta = replace(job.meta, traceparent=meta.traceparent)
        self._due[key] = job.last_input_ms + wait_ms
        return RecordResult(outcome, _snapshot(key, job))


def _snapshot(key: JobKey, job: _Job) -> JobSnapshot:
    return JobSnapshot(
        key=key,
        status=job.status,
        meta=job.meta,
        chunks=_ordered_chunks(job.chunks.values()),
        vectors=tuple(sorted(job.vectors.values(), key=lambda v: str(v.execution_id))),
    )


def _ordered_chunks(chunks: Iterable[ChunkRecord]) -> tuple[ChunkRecord, ...]:
    return tuple(sorted(chunks, key=lambda c: (c.owned_range.start_ms, str(c.chunk_id))))
