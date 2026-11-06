"""실시간(STREAM) 창 증분 fold와 세션 마무리(R-29~R-34, ADR-0015·ADR-0016).

읽기 API 없이 Kafka 입력만 쓴다. 창 하나가 완료되면(`analysis.completed` scope chunk) 대기 집합의
confirmed 이벤트로 세션 상태를 fold해 `state.updated`를 한 건 낸다. 세션이 끝나면(scope job) 창
기록 전체로 업로드와 같은 결과 파일과 `source.completed`를 낸다. 창은 한 세션 안에서 순서대로
완료되므로(SPEC-036 R-8) 상태와 재판정 계수가 결정적이다.

저장 위치
- 세션 상태: Redis `agg:state:<sourceId>`, 30창마다 S3 `sources/<sourceId>/state/<sequence>.json`
- 창 기록: S3 `sources/<sourceId>/jobs/<jobId>/aggregate/windows/<sequence>.json`(세션 마무리 입력)
- 처리한 창: Redis `agg:window:<sourceId>:<chunkId>` → sequence
- 구역: Redis `agg:zones:<sourceId>`(R-33)

업로드와 달리 stream 계약에는 구역 머문 구간이 없어 체류·배회 규칙 이벤트와 구역 노출은 0이다.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, ImageRef
from vlm_common.http_call import Deadline
from vlm_contracts.aggregates import CarriedRisk, InitialState, StateSnapshot, ZoneState, result_key
from vlm_contracts.events import (
    AnalysisCompleted,
    ChunkManifest,
    Event,
    EventAnalyzed,
    ExposureDelta,
    MissingCounts,
    RiskTransition,
    StateCounts,
    StateSnapshotDelta,
    StateUpdated,
    TrackDetected,
    VLMAnalysis,
)
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage
from vlm_stream_client.models import ZoneList as StreamZoneList

from aggregator import ids
from aggregator.compute import compute
from aggregator.exposure import exposure_of, overlap
from aggregator.fold import FoldDelta, RiskRecord, SessionState, fold
from aggregator.judge_budget import JudgeBudget
from aggregator.kv import KeyValueStore
from aggregator.model import AggregateInput, InputEvent, TrackSpan, ZoneInfo
from aggregator.pending import (
    PendingWindows,
    StoredEvent,
    StoredTracks,
    WindowInputs,
    WindowManifest,
    stream_track_id,
)
from aggregator.ports import CountMismatchError, EventPublisher, ServiceConfig
from aggregator.profile import LoadedProfile
from aggregator.result import (
    AggregateMeta,
    build_result,
    encode_result,
    meta_key,
    source_completed,
)
from aggregator.session_state import STATE_TTL_S, SessionStateStore
from aggregator.state import confirmed_in_order
from aggregator.summary import AnalyzeClient, summarize

STATE_UPDATED = "state.updated"
SOURCE_COMPLETED = "source.completed"
_JUDGE_TASK = "event_judge"
_ACCESS = "hazard_zone_access"
_JSON = "application/json"
# state.updated의 riskType 상한. 결과 파일(100자)보다 짧아 넘는 값은 잘라 싣는다(계약 차이로 보고).
_TRANSITION_RISK_TYPE_MAX = 64

logger = logging.getLogger(__name__)


class ZonesClient(Protocol):
    """stream-service 내부 구역 API. 운영은 `vlm_stream_client.client.StreamApiClient`다."""

    async def get_session_zones(
        self, session_id: UUID, *, deadline: Deadline
    ) -> StreamZoneList: ...


class _Stored(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class StoredTrack(_Stored):
    track_id: UUID
    class_name: str
    start_ms: int
    end_ms: int


class WindowRecord(_Stored):
    """창 하나의 기록. 같은 창이 다시 오면 `state_updated`를 다시 내고, 세션 마무리(R-32)는 기록
    전체의 이벤트·트랙을 첫 창의 시작 상태에서 다시 fold한다(fold 창 누적 = 한 번 fold)."""

    sequence: int
    chunk_id: UUID
    state_updated: StateUpdated
    events: list[StoredEvent]
    tracks: list[StoredTrack]
    state_before: SessionState
    missing_events: int = 0


class _TracksFile(BaseModel):
    """트랙 요약 파일(`tracks/v1/chunk-tracks`)에서 aggregator가 읽는 부분."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    class _Crop(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, extra="ignore")
        key: str

    class _Track(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")
        track_local_id: str
        class_name: str
        start_ms: int
        end_ms: int
        representative_crops: list["_TracksFile._Crop"] = Field(default_factory=list)

    tracks: list[_Track]


class StreamAggregator:
    """실시간 세션 처리.

    Args:
        store: Redis(대기 집합·세션 상태·구역 캐시·재판정 계수).
        storage: 창 기록·상태 스냅샷·결과 파일 저장소.
        publisher: 이벤트 발행기.
        gateway: ai-gateway(`event_judge` 재판정, `source_summary`).
        zones: stream-service 내부 구역 API.
        profile: 기준값과 버전.
        config: 마감·재시도 설정(`aggregator.service.ServiceConfig`).
    """

    def __init__(
        self,
        *,
        store: KeyValueStore,
        storage: ObjectStorage,
        publisher: EventPublisher,
        gateway: AnalyzeClient,
        zones: ZonesClient,
        profile: LoadedProfile,
        config: ServiceConfig,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._storage = storage
        self._publisher = publisher
        self._gateway = gateway
        self._zones = zones
        self._profile = profile
        self._config = config
        self._clock = clock
        self._now = now
        self._windows = PendingWindows(store)
        self._states = SessionStateStore(store, storage)
        self._budget = JudgeBudget(store)

    # ---- 대기 집합(R-29) ----

    async def on_event_analyzed(self, message: EventAnalyzed) -> None:
        await self._windows.add_event(
            message.source_id, message.chunk_id, StoredEvent.from_analyzed(message)
        )

    async def on_track_detected(self, message: TrackDetected) -> None:
        if message.chunk_id is None or message.execution_id is None or message.tracks_key is None:
            return
        await self._windows.add_tracks(
            message.source_id,
            message.chunk_id,
            StoredTracks(execution_id=message.execution_id, tracks_key=message.tracks_key),
        )

    async def on_chunk_manifest(self, message: ChunkManifest) -> None:
        await self._windows.set_manifest(
            message.source_id, message.chunk_id, WindowManifest.of(message)
        )

    # ---- 창 완료(R-30, R-31, R-34) ----

    async def on_chunk_completed(self, message: AnalysisCompleted, ctx: MessageContext) -> None:
        source, chunk = message.source_id, window_of(message)
        done = await self._states.window_sequence(source, chunk)
        if done is not None:
            record = await asyncio.to_thread(self._get_window, source, message.job_id, done)
            if record is not None:
                # 최소 한 번 전달: 다시 계산하지 않고 저장한 결과를 다시 낸다.
                await self._publish(STATE_UPDATED, record.state_updated, ctx)
                return
        inputs = await self._windows.load(source, chunk)
        rules = sum(1 for e in inputs.events if e.judgment_method.startswith("rule"))
        mismatch = rules != message.counts.events or inputs.manifest is None
        if mismatch and ctx.attempt < self._config.max_attempts:
            raise CountMismatchError(
                f"window {chunk}: rule events {rules}/{message.counts.events}, "
                f"manifest {'present' if inputs.manifest else 'missing'}"
            )
        previous = message.session.previous_source_id if message.session else None
        state = await self._states.load(source, previous)
        zones = await self._session_zones(source)
        with ctx.timing.stage("aggregate.fold", unit="events") as block:
            tracks, crops = await asyncio.to_thread(self._read_tracks, inputs.tracks)
            record, next_state = await self._fold_window(
                message, inputs, state, zones, tracks, crops, mismatch, ctx
            )
            block.count = len(inputs.events)
        await asyncio.to_thread(self._put_window, source, message.job_id, record)
        every = self._profile.profile.snapshot_every_windows
        await self._states.save(source, next_state, snapshot=record.sequence % every == 0)
        await self._states.mark_window(source, chunk, record.sequence)
        await self._publish(STATE_UPDATED, record.state_updated, ctx)

    async def _fold_window(
        self,
        message: AnalysisCompleted,
        inputs: WindowInputs,
        state: SessionState,
        zones: Sequence[ZoneInfo],
        tracks: Sequence[TrackSpan],
        crops: dict[tuple[UUID, str], list[str]],
        partial: bool,
        ctx: MessageContext,
    ) -> tuple[WindowRecord, SessionState]:
        job = message.job_id
        start, end, gap = _window_range(inputs.manifest, state, inputs.events)
        names = {z.zone_id: z.name for z in zones}
        stored = await self._rejudged(message, inputs.events, state, end, crops, names, ctx)
        confirmed = confirmed_in_order([e.to_input() for e in stored])
        folded, delta = await asyncio.to_thread(fold, state, confirmed, job_id=job)
        sequence = state.last_sequence + 1
        next_state = folded.model_copy(
            update={"last_sequence": sequence, "last_window_end_ms": end}
        )
        update = StateUpdated(
            event_id=ids.state_updated_id(job, sequence),
            occurred_at=self._now(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            job_id=job,
            chunk_id=window_of(message),
            sequence=sequence,
            window_start_ms=start,
            window_end_ms=end,
            partial=partial or bool(message.partial),
            gap_ms=gap,
            snapshots=[_snapshot_delta(s) for s in delta.snapshots if s.current_state is not None],
            risk_transitions=[_transition(t.kind, t.at_ms, t.risk) for t in delta.transitions],
            rule_events=[],
            exposure_deltas=self._exposure_deltas(state, delta, next_state, tracks, start, end),
            active_risk_zone_ids=sorted({r.zone_id for r in next_state.active_risks}),
            counts=StateCounts(
                input_events=len(inputs.events),
                confirmed_events=len(confirmed),
                active_risks=len(next_state.active_risks),
            ),
            aggregate_profile=self._profile.version,
        )
        rules = sum(1 for e in inputs.events if e.judgment_method.startswith("rule"))
        record = WindowRecord(
            sequence=sequence,
            chunk_id=window_of(message),
            state_updated=update,
            events=list(stored),
            tracks=[
                StoredTrack(
                    track_id=t.track_id,
                    class_name=t.class_name,
                    start_ms=t.start_ms,
                    end_ms=t.end_ms,
                )
                for t in tracks
            ],
            state_before=state,
            missing_events=max(0, message.counts.events - rules),
        )
        return record, next_state

    def _exposure_deltas(
        self,
        before: SessionState,
        delta: FoldDelta,
        after: SessionState,
        tracks: Sequence[TrackSpan],
        start: int,
        end: int,
    ) -> list[ExposureDelta]:
        """창 안에서 활성이던 위험마다 창 구간의 노출 증분(R-16을 창으로 자른 값)."""
        latest: dict[UUID, RiskRecord] = {r.risk_id: r for r in before.active_risks}
        latest.update((t.risk.risk_id, t.risk) for t in delta.transitions)
        latest.update((r.risk_id, r) for r in after.active_risks)
        persons = self._profile.profile.person_classes
        out = []
        for risk in sorted(latest.values(), key=lambda r: r.risk_id):
            if overlap(start, end, risk.first_seen_ms, risk.active_until(end)) <= 0:
                continue
            exposure = exposure_of(risk, end, tracks, (), persons, start_ms=start)
            out.append(
                ExposureDelta(
                    risk_id=risk.risk_id,
                    active_seconds_delta=exposure.active_seconds,
                    exposure_count_delta=exposure.exposure_count,
                    exposure_seconds_delta=exposure.exposure_seconds,
                )
            )
        return out

    async def _rejudged(
        self,
        message: AnalysisCompleted,
        events: Sequence[StoredEvent],
        state: SessionState,
        window_end: int,
        crops: dict[tuple[UUID, str], list[str]],
        zone_names: dict[UUID, str],
        ctx: MessageContext,
    ) -> list[StoredEvent]:
        """활성 위험 구간과 겹친 위험구역 접근을 대표 크롭으로 다시 판정한다(R-31).

        겹침은 창 시작 상태의 활성 위험과 이 창에서 활성된 위험(먼저 한 번 fold해 구한다)으로 본다.
        """
        candidates = [e for e in events if _is_access(e)]
        if not candidates:
            return list(events)
        confirmed = confirmed_in_order([e.to_input() for e in events])
        _, first = fold(state, confirmed, job_id=message.job_id)
        risks = [*state.active_risks, *(t.risk for t in first.transitions)]
        replaced: dict[UUID, StoredEvent] = {}
        for e in sorted(candidates, key=lambda e: (e.end_ms, e.event_id)):
            if not _during_risk(e, risks, window_end):
                continue
            keys = _crops_of(e, crops)
            if not keys:
                continue
            limit = self._profile.profile.judge_call_limit
            if not await self._budget.try_acquire(message.job_id, limit=limit):
                logger.info("rejudge skipped by call cap", extra={"jobId": str(message.job_id)})
                break
            zone_name = e.zone_name or (zone_names.get(e.zone_id) if e.zone_id else None)
            judged = await self._judge(
                message, e.model_copy(update={"zone_name": zone_name}), keys, ctx
            )
            if judged is not None:
                replaced[e.event_id] = e.model_copy(
                    update={"analysis": judged, "judgment_method": "rule+vlm"}
                )
        return [replaced.get(e.event_id, e) for e in events]

    async def _judge(
        self, message: AnalysisCompleted, e: StoredEvent, keys: list[str], ctx: MessageContext
    ) -> VLMAnalysis | None:
        request = AnalyzeRequest(
            task_type=_JUDGE_TASK,
            source_id=message.source_id,
            images=[ImageRef(storage_key=k) for k in keys],
            variables={
                "eventType": e.event_type,
                "startSec": e.start_ms / 1000,
                "endSec": e.end_ms / 1000,
                "zoneName": e.zone_name,
                "evidence": {"path": "rule", "reason": "active_risk_overlap"},
            },
        )
        try:
            response = await self._gateway.analyze(request, traceparent=ctx.traceparent)
            return VLMAnalysis.model_validate(response.output)
        except (AiGatewayError, ValidationError) as e2:
            # 재판정은 부가 판정이다. 실패하면 규칙 판정 그대로 둔다.
            logger.warning(
                "rejudge failed", extra={"eventId": str(e.event_id), "error": type(e2).__name__}
            )
            return None

    # ---- 세션 마무리(R-32) ----

    async def on_session_completed(self, message: AnalysisCompleted, ctx: MessageContext) -> None:
        source = message.source_id
        previous = message.session.previous_source_id if message.session else None
        state = await self._states.load(source, previous)
        records = await asyncio.to_thread(self._all_windows, source, message.job_id, state)
        zones = await self._session_zones(source)
        begin = records[0].state_before if records else state
        events: dict[UUID, InputEvent] = {}
        tracks: dict[UUID, TrackSpan] = {}
        for record in records:
            events.update((e.event_id, e.to_input()) for e in record.events)
            tracks.update(
                (t.track_id, TrackSpan(t.track_id, t.class_name, t.start_ms, t.end_ms))
                for t in record.tracks
            )
        aggregate = AggregateInput(
            source_id=source,
            owner_id=message.owner_id,
            job_id=message.job_id,
            normalization_id=message.normalization_id,
            duration_ms=message.duration_ms,
            session=message.session,
            initial_state=_initial(begin),
            events=tuple(sorted(events.values(), key=lambda e: (e.start_ms, e.event_id))),
            tracks=tuple(sorted(tracks.values(), key=lambda t: t.track_id)),
            zones=tuple(zones),
        )
        # 창 상태(state.updated)와 같은 위험이 나오도록 청크 경계 병합을 하지 않는다.
        computed = await asyncio.to_thread(compute, aggregate, self._profile, merge=False)
        summary = await summarize(self._gateway, aggregate, computed, traceparent=ctx.traceparent)
        missing_events = sum(r.missing_events for r in records)
        partial = any(r.state_updated.partial for r in records)
        event = source_completed(
            aggregate,
            computed,
            summary_status=summary.status,
            version=self._profile.version,
            revision=1,
            occurred_at=self._now(),
            missing=MissingCounts(events=missing_events) if partial else None,
            job_type="STREAM",
        )
        meta = AggregateMeta(
            revision=1,
            input_event_ids=sorted(events),
            partial=partial,
            completed=message,
        )
        data = encode_result(build_result(aggregate, computed, summary, self._profile.version))
        await asyncio.to_thread(self._put_result, aggregate, data, meta)
        await self._publish(SOURCE_COMPLETED, event, ctx)
        logger.info(
            "stream session aggregated",
            extra={"jobId": str(message.job_id), "windows": len(records), "partial": partial},
        )

    # ---- 저장소 ----

    async def _session_zones(self, source_id: UUID) -> list[ZoneInfo]:
        """세션 구역을 세션당 한 번 읽어 Redis에 둔다(R-33). sessionId는 sourceId다."""
        key = f"agg:zones:{source_id}"
        cached = await self._store.get(key)
        if cached is None:
            deadline = Deadline.start(self._config.read_timeout_s, self._clock)
            listed = await self._zones.get_session_zones(source_id, deadline=deadline)
            cached = json.dumps(
                [{"zoneId": str(z.zone_id), "name": z.name, "kind": z.kind} for z in listed.zones]
            )
            await self._store.set(key, cached, STATE_TTL_S)
        return [ZoneInfo(UUID(z["zoneId"]), z["name"], z["kind"]) for z in json.loads(cached)]

    def _read_tracks(
        self, entries: Sequence[StoredTracks]
    ) -> tuple[list[TrackSpan], dict[tuple[UUID, str], list[str]]]:
        tracks: list[TrackSpan] = []
        crops: dict[tuple[UUID, str], list[str]] = {}
        for entry in entries:
            parsed = _TracksFile.model_validate_json(self._storage.get_bytes(entry.tracks_key))
            for t in parsed.tracks:
                tracks.append(
                    TrackSpan(
                        stream_track_id(entry.execution_id, t.track_local_id),
                        t.class_name,
                        t.start_ms,
                        t.end_ms,
                    )
                )
                crops[(entry.execution_id, t.track_local_id)] = [
                    c.key for c in t.representative_crops
                ]
        return tracks, crops

    def _window_key(self, source_id: UUID, job_id: UUID, sequence: int) -> str:
        return f"sources/{source_id}/jobs/{job_id}/aggregate/windows/{sequence}.json"

    def _put_window(self, source_id: UUID, job_id: UUID, record: WindowRecord) -> None:
        self._storage.put_bytes(
            self._window_key(source_id, job_id, record.sequence),
            record.model_dump_json(by_alias=True).encode(),
            _JSON,
        )

    def _get_window(self, source_id: UUID, job_id: UUID, sequence: int) -> WindowRecord | None:
        try:
            raw = self._storage.get_bytes(self._window_key(source_id, job_id, sequence))
        except ObjectNotFoundError:
            return None
        return WindowRecord.model_validate_json(raw)

    def _all_windows(
        self, source_id: UUID, job_id: UUID, state: SessionState
    ) -> list[WindowRecord]:
        found = (self._get_window(source_id, job_id, n) for n in range(1, state.last_sequence + 1))
        return [r for r in found if r is not None]

    def _put_result(self, source: AggregateInput, data: bytes, meta: AggregateMeta) -> None:
        self._storage.put_bytes(result_key(source.source_id, source.job_id), data, _JSON)
        self._storage.put_bytes(
            meta_key(source.source_id, source.job_id),
            meta.model_dump_json(by_alias=True).encode(),
            _JSON,
        )

    async def _publish(self, topic: str, event: Event, ctx: MessageContext) -> None:
        await asyncio.to_thread(
            self._publisher.publish,
            topic,
            event,
            key=str(event.source_id),
            traceparent=ctx.traceparent,
        )


def window_of(message: AnalysisCompleted) -> UUID:
    """창 ID. 모델 검증이 scope chunk면 chunkId가 있다고 확인하므로 없으면 계약 위반이다."""
    if message.chunk_id is None:
        raise NonRetryableError("analysis.completed scope chunk without chunkId")
    return message.chunk_id


def _window_range(
    manifest: WindowManifest | None, state: SessionState, events: Sequence[StoredEvent]
) -> tuple[int, int, int]:
    """창 범위와 빈 시간. manifest가 끝내 없으면(partial) 직전 창 끝 ~ 마지막 이벤트 끝이다."""
    if manifest is not None:
        return manifest.start_ms, manifest.end_ms, manifest.gap_ms
    start = state.last_window_end_ms
    end = max([start + 1, *(e.end_ms for e in events)])
    return start, end, 0


def _crops_of(e: StoredEvent, crops: dict[tuple[UUID, str], list[str]]) -> list[str]:
    """이벤트 트랙의 대표 크롭(트랙 요약 파일 `representativeCrops`)."""
    if e.execution_id is None or e.track_local_id is None:
        return []
    return crops.get((e.execution_id, e.track_local_id), [])


def _is_access(e: StoredEvent) -> bool:
    return (
        e.event_type == _ACCESS
        and e.judgment_method == "rule"
        and e.zone_id is not None
        and e.analysis.verification_status == "confirmed"
    )


def _during_risk(e: StoredEvent, risks: Sequence[RiskRecord], window_end: int) -> bool:
    zone = str(e.zone_id)
    return any(
        r.zone_id == zone
        and overlap(e.start_ms, e.end_ms, r.first_seen_ms, r.active_until(window_end)) > 0
        for r in risks
    )


def _snapshot_delta(s: StateSnapshot) -> StateSnapshotDelta:
    return StateSnapshotDelta.model_validate(s.model_dump(by_alias=True))


def _transition(kind: str, at_ms: int, risk: RiskRecord) -> RiskTransition:
    return RiskTransition.model_validate(
        {
            "riskId": risk.risk_id,
            "zoneId": risk.zone_id,
            "riskType": risk.risk_type[:_TRANSITION_RISK_TYPE_MAX],
            "transition": kind,
            "timestampMs": at_ms,
            "reason": risk.reason[:500],
            "relatedEventIds": list(risk.related_event_ids),
        }
    )


def _initial(state: SessionState) -> InitialState:
    """세션 시작 상태를 결과 파일의 초기 상태로 바꾼다."""
    return InitialState(
        from_source_id=state.from_source_id,
        zone_states=[
            ZoneState(zone_id=zone, state=text)
            for zone, text in sorted(state.zone_states.items())
            if text
        ],
        active_risks=[
            CarriedRisk(
                risk_id=r.risk_id,
                zone_id=r.zone_id,
                risk_type=r.risk_type[:100],
                reason=r.reason[:1000],
                first_seen_ms=r.first_seen_ms,
            )
            for r in state.active_risks
        ],
    )


__all__ = ["StreamAggregator", "WindowRecord"]
