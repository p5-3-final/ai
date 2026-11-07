"""aggregator 핸들러(R-1~R-5, R-19~R-29). 실시간 창(STREAM)은 `stream.py`가 맡는다.

처리 순서(업로드 job): 읽기(청크 재고 포함) → 개수 대조 → 재고 비교 → 재고 판정 → 계산 → 예측 →
요약 → 결과 파일 저장 → `source.completed` 발행(SPEC-028은 재고 비교·판정·예측을 더한다).
발행이 끝나야 워커가 커밋한다(NFR-4). 재시도할 수 있는 실패는 그대로 던져 재시도 토픽으로 보내고,
다시 해도 같은 실패는 `NonRetryableError`로 바꿔 DLQ로 보낸다(R-22, R-23).
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from vlm_analysis_client.client import AnalysisApiError
from vlm_common.http_call import Deadline
from vlm_contracts.aggregates import ChunkInventory, result_key
from vlm_contracts.events import (
    AnalysisCompleted,
    ChunkManifest,
    EventAnalyzed,
    TrackDetected,
)
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from aggregator.compute import compute
from aggregator.equipment import abandoned_equipment
from aggregator.forecast import forecast_risks
from aggregator.inventory import compare
from aggregator.inventory_store import InventoryIncompleteError, InventoryStore
from aggregator.judge import InventoryJudge, LocalBudget
from aggregator.kv import KeyValueStore, StoreUnavailableError
from aggregator.model import AggregateInput, InputEvent
from aggregator.pending import PendingVectors, StoredEvent
from aggregator.ports import CountMismatchError, EventPublisher, ServiceConfig
from aggregator.profile import LoadedProfile
from aggregator.reader import InvalidInputError, JobReader, missing_counts
from aggregator.result import (
    AggregateMeta,
    InventoryResult,
    build_result,
    encode_result,
    meta_key,
    source_completed,
)
from aggregator.summary import AnalyzeClient, summarize

SOURCE_COMPLETED = "source.completed"
_JSON = "application/json"
# 다시 읽어도 같은 결과가 나오는 analysis-api 오류(R-23)
_PERMANENT_CODES = frozenset({"ANALYSIS_INVALID_RESPONSE"})

__all__ = ["AggregatorService", "CountMismatchError", "ServiceConfig"]

logger = logging.getLogger(__name__)


class WindowHandler(Protocol):
    """실시간 창 처리(`stream.StreamAggregator`)."""

    async def on_chunk_completed(self, message: AnalysisCompleted, ctx: MessageContext) -> None: ...

    async def on_session_completed(
        self, message: AnalysisCompleted, ctx: MessageContext
    ) -> None: ...

    async def on_event_analyzed(self, message: EventAnalyzed) -> None: ...

    async def on_track_detected(self, message: TrackDetected) -> None: ...

    async def on_chunk_manifest(self, message: ChunkManifest) -> None: ...


class AggregatorService:
    """`analysis.completed`·`event.analyzed`·`track.detected`·`chunk.manifest` 핸들러.

    Args:
        reader: analysis-api 읽기.
        gateway: ai-gateway(`source_summary`).
        storage: 결과 파일 저장소.
        publisher: 이벤트 발행기.
        profile: 기준값과 버전.
        config: 마감·재시도 설정.
        store: Redis(대기 집합·발행 기록). None이면 업로드 기본 경로만 동작한다.
        stream: 실시간 창 처리. None이면 STREAM 메시지는 재시도 가능 오류다.
        clock: epoch 초 시계(마감 계산).
        now: 이벤트 발생 시각.
    """

    def __init__(
        self,
        *,
        reader: JobReader,
        gateway: AnalyzeClient,
        storage: ObjectStorage,
        publisher: EventPublisher,
        profile: LoadedProfile,
        config: ServiceConfig,
        store: KeyValueStore | None = None,
        stream: WindowHandler | None = None,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader
        self._gateway = gateway
        self._storage = storage
        self._publisher = publisher
        self._profile = profile
        self._config = config
        self._pending = PendingVectors(store) if store is not None else None
        self._inventory = InventoryStore(storage)
        self._judge = InventoryJudge(gateway, storage, profile.profile)
        self._stream = stream
        self._clock = clock
        self._now = now

    async def on_analysis_completed(self, message: AnalysisCompleted, ctx: MessageContext) -> None:
        """job(또는 실시간 창)의 적재 완료(R-1, R-2, R-30, R-32)."""
        if message.job_type == "REEMBED":
            # 재임베딩은 이벤트·트랙이 바뀌지 않아 결과가 같다(R-2).
            logger.info(
                "aggregate skipped",
                extra={"jobId": str(message.job_id), "reason": "jobType REEMBED"},
            )
            return
        if message.job_type == "STREAM":
            stream = self._require_stream()
            if message.scope == "chunk":
                await stream.on_chunk_completed(message, ctx)
            else:
                await stream.on_session_completed(message, ctx)
            return
        meta = await asyncio.to_thread(self._load_meta, message.source_id, message.job_id)
        # 같은 job을 다시 받으면 지금 차수 그대로 다시 낸다(같은 입력이면 같은 내용, R-21).
        await self._aggregate(message, ctx, revision=meta.revision if meta else 1)

    async def on_event_analyzed(self, message: EventAnalyzed, ctx: MessageContext) -> None:
        """판정 이벤트(R-25, R-27, R-29). 업로드는 `vector*`만 대기 집합에 넣는다."""
        if message.job_type == "STREAM":
            await self._require_stream().on_event_analyzed(message)
            return
        if not message.judgment_method.startswith("vector"):
            return
        stored = StoredEvent.from_analyzed(message)
        await self._require_pending().add(message.job_id, stored)
        if message.analysis.verification_status != "confirmed":
            return
        meta = await asyncio.to_thread(self._load_meta, message.source_id, message.job_id)
        if meta is None or message.event_id in meta.input_event_ids:
            return
        # 증분 fold는 이미 지난 시각의 이벤트를 넣을 수 없어 전체를 다시 계산한다(plan 9절).
        logger.info(
            "late vector event, recomputing",
            extra={"jobId": str(message.job_id), "revision": meta.revision + 1},
        )
        await self._aggregate(meta.completed, ctx, revision=meta.revision + 1)

    async def on_track_detected(self, message: TrackDetected, ctx: MessageContext) -> None:
        """트랙 준비(R-29). 실시간 창의 origin chunk만 모은다. 업로드는 analysis-api가 적재한다."""
        if message.job_type == "STREAM" and message.origin == "chunk":
            await self._require_stream().on_track_detected(message)

    async def on_chunk_manifest(self, message: ChunkManifest, ctx: MessageContext) -> None:
        """청크 manifest. 재고를 청크 재고 파일로 남기고(SPEC-028 R-1, R-19), 실시간 창이면 범위·빈
        시간도 쓴다(R-30)."""
        await self._inventory.save(message, ctx)
        if message.job_type == "STREAM":
            await self._require_stream().on_chunk_manifest(message)

    async def _aggregate(
        self, message: AnalysisCompleted, ctx: MessageContext, *, revision: int
    ) -> None:
        deadline = Deadline.start(self._config.read_timeout_s, self._clock)
        extra = await self._pending_events(message.job_id)
        with ctx.timing.stage("aggregate.read", unit="events") as block:
            source = await self._read(message, deadline, ctx.traceparent, extra)
            chunks = await asyncio.to_thread(
                self._inventory.load_all, message.source_id, message.job_id
            )
            block.count = len(source.events)
        missing = missing_counts(message.counts, source.events, source.tracks)
        last_attempt = ctx.attempt >= self._config.max_attempts
        if missing is not None and not last_attempt:
            raise CountMismatchError(
                f"read counts differ from analysis.completed (missing {missing.model_dump()})"
            )
        if len(chunks) < message.counts.chunks and not last_attempt:
            raise InventoryIncompleteError(
                f"inventory files {len(chunks)} < chunks {message.counts.chunks}"
            )
        stocked = await self._inventory_stage(source, chunks, message, ctx)
        with ctx.timing.stage("aggregate.compute", unit="events") as block:
            computed = await asyncio.to_thread(
                compute, source, self._profile, inventory=stocked.events if stocked else ()
            )
            block.count = len(source.events)
        with ctx.timing.stage("aggregate.forecast", unit="risks") as block:
            forecasts = await forecast_risks(
                self._gateway, source, computed, self._profile.profile, traceparent=ctx.traceparent
            )
            block.count = len(forecasts)
        with ctx.timing.stage("aggregate.summary", unit="calls") as block:
            summary = await summarize(self._gateway, source, computed, traceparent=ctx.traceparent)
            block.count = 0 if summary.status == "empty" else 1
        result = build_result(
            source,
            computed,
            summary,
            self._profile.version,
            inventory=stocked,
            forecasts=forecasts,
        )
        event = source_completed(
            source,
            computed,
            summary_status=summary.status,
            version=self._profile.version,
            revision=revision,
            occurred_at=self._now(),
            missing=missing,
            job_type=message.job_type,
            inventory=stocked,
            forecasts=forecasts,
        )
        meta = AggregateMeta(
            revision=revision,
            input_event_ids=sorted({e.event_id for e in source.events}),
            partial=missing is not None,
            completed=message,
        )
        with ctx.timing.stage("aggregate.store", unit="files") as block:
            await asyncio.to_thread(self._store_result, source, encode_result(result), meta)
            block.count = 1
        await asyncio.to_thread(
            self._publisher.publish,
            SOURCE_COMPLETED,
            event,
            key=str(source.source_id),
            traceparent=ctx.traceparent,
        )
        logger.info(
            "source aggregated",
            extra={
                "jobId": str(source.job_id),
                "revision": revision,
                "partial": missing is not None,
                "summaryStatus": summary.status,
                "events": len(source.events),
                "risks": len(computed.risks),
            },
        )

    async def _inventory_stage(
        self,
        source: AggregateInput,
        chunks: list[ChunkInventory],
        message: AnalysisCompleted,
        ctx: MessageContext,
    ) -> InventoryResult | None:
        """재고 비교·장비 방치(순수 계산)와 판정(SPEC-028 R-3~R-13).

        재시도 상한에서도 재고 파일이 모자라면 없는 청크를 사라진 것으로 잘못 볼 수 있어 재고 단계를
        건너뛰고 None을 낸다(R-2, plan 4절).
        """
        if len(chunks) < message.counts.chunks:
            logger.warning(
                "inventory skipped",
                extra={
                    "jobId": str(source.job_id),
                    "inventoryFiles": len(chunks),
                    "chunks": message.counts.chunks,
                },
            )
            return None
        loaded = self._profile.profile
        with ctx.timing.stage("aggregate.inventory_compare", unit="objects") as block:
            comparison = await asyncio.to_thread(
                compare, chunks, source.previous_inventory, job_id=source.job_id, profile=loaded
            )
            abandoned = abandoned_equipment(
                comparison.chains, source.tracks, source.presences, loaded, job_id=source.job_id
            )
            block.count = sum(len(c.static_objects) for c in chunks)
        with ctx.timing.stage("aggregate.judge", unit="calls") as block:
            judged = await self._judge.judge(
                comparison,
                abandoned,
                source_id=source.source_id,
                job_id=source.job_id,
                zones=source.zones,
                budget=LocalBudget(loaded.judge_call_limit),
                traceparent=ctx.traceparent,
            )
            block.count = judged.calls
        return InventoryResult(comparison.end_inventory, judged.changes, judged.events)

    async def _read(
        self,
        message: AnalysisCompleted,
        deadline: Deadline,
        traceparent: str | None,
        extra: Sequence[InputEvent],
    ) -> AggregateInput:
        try:
            return await self._reader.read(
                message, deadline=deadline, traceparent=traceparent, extra_events=extra
            )
        except InvalidInputError as e:
            raise NonRetryableError(str(e)) from e
        except AnalysisApiError as e:
            if e.status == 400 or e.code in _PERMANENT_CODES:
                raise NonRetryableError(f"analysis-api rejected the read: {e.code}") from e
            raise

    async def _pending_events(self, job_id: UUID) -> list[InputEvent]:
        if self._pending is None:
            return []
        return [e.to_input() for e in await self._pending.all(job_id)]

    def _store_result(self, source: AggregateInput, data: bytes, meta: AggregateMeta) -> None:
        # 결과 파일 먼저, 그다음 자기 기록. 기록만 남고 결과가 없는 상태를 만들지 않는다.
        self._storage.put_bytes(result_key(source.source_id, source.job_id), data, _JSON)
        self._storage.put_bytes(
            meta_key(source.source_id, source.job_id),
            meta.model_dump_json(by_alias=True).encode(),
            _JSON,
        )

    def _load_meta(self, source_id: UUID, job_id: UUID) -> AggregateMeta | None:
        try:
            raw = self._storage.get_bytes(meta_key(source_id, job_id))
        except ObjectNotFoundError:
            return None
        return AggregateMeta.model_validate_json(raw)

    def _require_pending(self) -> PendingVectors:
        if self._pending is None:
            raise StoreUnavailableError("AGGREGATOR_REDIS_URL is not configured")
        return self._pending

    def _require_stream(self) -> WindowHandler:
        if self._stream is None:
            # build_worker는 Redis와 stream-service 주소가 둘 다 있어야 처리기를 만든다.
            raise StoreUnavailableError(
                "STREAM handling needs AGGREGATOR_REDIS_URL and AGGREGATOR_STREAM_SERVICE_URL"
            )
        return self._stream
