"""aggregator 테스트가 함께 쓰는 입력 빌더·계약 검증 도우미."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID

from aggregator.kv import MemoryStore
from aggregator.model import AggregateInput, InputEvent, Presence, TrackSpan, ZoneInfo
from aggregator.ports import ServiceConfig
from aggregator.profile import DEFAULT_PROFILE_PATH, LoadedProfile, load_aggregate_profile
from aggregator.reader import JobReader
from aggregator.service import AggregatorService
from aggregator.stream import StreamAggregator
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse
from vlm_analysis_client.client import (
    AnalysisApiError,
    EventPage,
    PresencePage,
    TrackPage,
    ZoneList,
)
from vlm_analysis_client.models import FinalState
from vlm_common.http_call import Deadline, Failure
from vlm_contracts.aggregates import CarriedRisk, InitialState, ZoneState
from vlm_contracts.events import (
    AnalysisCompleted,
    AnalysisCounts,
    ChunkManifest,
    Event,
    EventAnalyzed,
    EventType,
    SessionRef,
    TrackDetected,
    VLMAnalysis,
)
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage
from vlm_stream_client.models import ZoneList as StreamZoneList

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f0d5-0000-7000-8000-000000000001")
NORMALIZATION = UUID("0192f0d4-0000-7000-8000-000000000001")
PREVIOUS_SOURCE = UUID("0192f0d2-0000-7000-8000-0000000000aa")
SESSION_ID = UUID("0192f0e0-5a6b-7c8d-9e0f-1a2b3c4d5e6f")
CHUNK_A = UUID("0192f0d6-0000-7000-8000-00000000000a")
CHUNK_B = UUID("0192f0d6-0000-7000-8000-00000000000b")
HAZARD = UUID("0192f100-0000-7000-8000-0000000000f1")
AISLE = UUID("0192f100-0000-7000-8000-0000000000f2")
DURATION_MS = 600_000
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def uid(n: int) -> UUID:
    """테스트용 결정적 UUID. 정렬 순서가 n 순서와 같다."""
    return UUID(int=n)


def profile() -> LoadedProfile:
    return load_aggregate_profile(DEFAULT_PROFILE_PATH)


def analysis(
    event_type: str = "ppe_missing",
    status: str = "confirmed",
    *,
    after_state: str | None = None,
    risk: bool | None = None,
    risk_type: str | None = None,
    risk_reason: str | None = None,
    severity: str | None = "medium",
    evidence_end: float | None = None,
    description: str | None = "설명 원문",
) -> VLMAnalysis:
    return VLMAnalysis.model_validate(
        {
            "eventType": event_type,
            "eventDetected": status == "confirmed",
            "verificationStatus": status,
            "description": description,
            "beforeState": None,
            "afterState": after_state,
            "riskDetected": risk,
            "riskType": risk_type,
            "riskReason": risk_reason,
            "solution": None,
            "severity": severity,
            "evidenceStart": None,
            "evidenceEnd": evidence_end,
        }
    )


def event(
    n: int,
    *,
    start: int,
    end: int | None = None,
    event_type: EventType = "ppe_missing",
    chunk: UUID | None = CHUNK_A,
    track: UUID | None = None,
    zone: UUID | None = None,
    score: float | None = 0.5,
    method: str = "rule+vlm",
    result: VLMAnalysis | None = None,
) -> InputEvent:
    return InputEvent(
        event_id=uid(n),
        chunk_id=chunk,
        event_type=event_type,
        start_ms=start,
        end_ms=start + 5_000 if end is None else end,
        track_id=track,
        zone_id=zone,
        zone_name="A동 크레인 하부" if zone == HAZARD else None,
        judgment_method=method,
        score=score,
        analysis=result or analysis(event_type),
    )


def risk_event(
    n: int, at: int, *, detected: bool, risk_type: str = "ppe_missing", zone: UUID | None = HAZARD
) -> InputEvent:
    """`at` 시각에 위험을 알리거나(detected) 해소를 알리는 확정 이벤트."""
    return event(
        n,
        start=at - 5_000,
        end=at,
        zone=zone,
        result=analysis(
            risk=detected,
            risk_type=risk_type,
            risk_reason="안전모 미착용" if detected else None,
        ),
    )


def person(n: int, start: int, end: int, cls: str = "person") -> TrackSpan:
    return TrackSpan(uid(n), cls, start, end)


def stay(track: int, zone: UUID, enter: int, exit_: int) -> Presence:
    return Presence(uid(track), zone, enter, exit_)


ZONES = (ZoneInfo(HAZARD, "A동 크레인 하부", "hazard"), ZoneInfo(AISLE, "통로 1", "aisle"))
EMPTY_INITIAL = InitialState(from_source_id=None, zone_states=[], active_risks=[])


def carried_initial(risk_id: UUID, *, state: str | None = "방호벽 설치됨") -> InitialState:
    return InitialState(
        from_source_id=PREVIOUS_SOURCE,
        zone_states=[ZoneState(zone_id=str(HAZARD), state=state)] if state else [],
        active_risks=[
            CarriedRisk(
                risk_id=risk_id,
                zone_id=str(HAZARD),
                risk_type="ppe_missing",
                reason="안전모 미착용",
                first_seen_ms=40_000,
            )
        ],
    )


def inputs(
    *events: InputEvent,
    tracks: tuple[TrackSpan, ...] = (),
    presences: tuple[Presence, ...] = (),
    initial: InitialState = EMPTY_INITIAL,
    session: SessionRef | None = None,
    zones: tuple[ZoneInfo, ...] = ZONES,
    duration_ms: int = DURATION_MS,
) -> AggregateInput:
    return AggregateInput(
        source_id=SOURCE,
        owner_id=OWNER,
        job_id=JOB,
        normalization_id=NORMALIZATION,
        duration_ms=duration_ms,
        session=session,
        initial_state=initial,
        events=tuple(events),
        tracks=tracks,
        presences=presences,
        zones=zones,
    )


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


def contracts_dir() -> Path:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "events").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    return path


@cache
def _registry() -> Registry[Schema]:
    path = contracts_dir()
    files = [
        *sorted((path / "events").glob("*.schema.json")),
        *sorted((path / "aggregates" / "v1").glob("*.schema.json")),
        path / "taxonomy/v2/taxonomy.schema.json",
    ]
    resources = []
    for schema in files:
        raw = json.loads(schema.read_text(encoding="utf-8"))
        resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    return Registry().with_resources(resources)


def contract_errors(schema_id: str, body: object) -> list[str]:
    """`body`(JSON 값)를 계약 스키마(`$id`)로 검증한 오류 메시지."""
    validator = Draft202012Validator({"$ref": schema_id}, registry=_registry())
    return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(body)]


EVENTS_ID = "https://vlm.example/contracts/events/"
RESULT_ID = "https://vlm.example/contracts/aggregates/v1/source-result.schema.json"


def event_errors(event: BaseModel, topic: str) -> list[str]:
    body = json.loads(event.model_dump_json(by_alias=True))
    return contract_errors(f"{EVENTS_ID}{topic}.schema.json", body)


# ---- 가짜 analysis-api(읽기 클라이언트 프로토콜) ----


def api_error(status: int, code: str) -> AnalysisApiError:
    return AnalysisApiError(
        code, code, status=status, retryable=status == 503, failure=Failure("status", status)
    )


def job_event_json(e: InputEvent) -> dict[str, object]:
    """InputEvent를 `listJobEvents` 항목 JSON으로 바꾼다."""
    return {
        "eventId": str(e.event_id),
        "chunkId": str(e.chunk_id) if e.chunk_id else None,
        "eventType": e.event_type,
        "startMs": e.start_ms,
        "endMs": e.end_ms,
        "trackId": str(e.track_id) if e.track_id else None,
        "zoneId": str(e.zone_id) if e.zone_id else None,
        "zoneName": e.zone_name,
        "judgmentMethod": e.judgment_method,
        "score": e.score,
        "reviewRequired": False,
        "analysis": json.loads(e.analysis.model_dump_json(by_alias=True)),
    }


@dataclass
class FakeAnalysisApi:
    """쪽 크기(`page_size`)로 나눠 돌려주는 가짜 analysis-api. 받은 호출과 마감을 남긴다."""

    events: list[dict[str, object]] = field(default_factory=list)
    tracks: list[TrackSpan] = field(default_factory=list)
    presences: list[Presence] = field(default_factory=list)
    zones: tuple[ZoneInfo, ...] = ZONES
    final_state: FinalState | AnalysisApiError | None = None
    page_size: int = 2
    fail: dict[str, AnalysisApiError] = field(default_factory=dict)
    calls: list[tuple[str, int, str | None]] = field(default_factory=list)

    def _page(
        self, name: str, items: list[dict[str, object]], cursor: str | None, deadline: Deadline
    ) -> dict[str, object]:
        self.calls.append((name, deadline.at_ms, cursor))
        if name in self.fail:
            raise self.fail[name]
        start = int(cursor or 0)
        end = start + self.page_size
        return {
            "items": items[start:end],
            "nextCursor": str(end) if end < len(items) else None,
        }

    async def list_job_events(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> EventPage:
        return EventPage.model_validate(self._page("events", self.events, cursor, deadline))

    async def list_job_tracks(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> TrackPage:
        items: list[dict[str, object]] = [
            {
                "trackId": str(t.track_id),
                "className": t.class_name,
                "startMs": t.start_ms,
                "endMs": t.end_ms,
            }
            for t in self.tracks
        ]
        return TrackPage.model_validate(self._page("tracks", items, cursor, deadline))

    async def list_job_zone_presences(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> PresencePage:
        items: list[dict[str, object]] = [
            {
                "trackId": str(p.track_id),
                "zoneId": str(p.zone_id),
                "enterMs": p.enter_ms,
                "exitMs": p.exit_ms,
            }
            for p in self.presences
        ]
        return PresencePage.model_validate(self._page("presences", items, cursor, deadline))

    async def list_source_zones(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> ZoneList:
        self.calls.append(("zones", deadline.at_ms, None))
        if "zones" in self.fail:
            raise self.fail["zones"]
        return ZoneList.model_validate(
            {
                "items": [
                    {"zoneId": str(z.zone_id), "name": z.name, "kind": z.kind} for z in self.zones
                ]
            }
        )

    async def get_source_final_state(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> FinalState:
        self.calls.append(("final-state", deadline.at_ms, None))
        if isinstance(self.final_state, AnalysisApiError):
            raise self.final_state
        if self.final_state is None:
            raise api_error(404, "ANALYSIS_NO_ACTIVE_VERSION")
        return self.final_state


# ---- 가짜 ai-gateway(source_summary) ----

SUMMARY_TEXT = "요약 문장 원문"


@dataclass
class FakeGateway:
    """`source_summary` 응답을 정하는 가짜 ai-gateway. 받은 요청을 남긴다."""

    output: dict[str, object] = field(
        default_factory=lambda: {
            "summary": SUMMARY_TEXT,
            "highlights": [{"atMs": 0, "text": "시작"}],
        }
    )
    model_id: str | None = "summary-m"
    error: AiGatewayError | None = None
    # event_judge(실시간 재판정, R-31) 응답. source_summary는 `output`이다.
    judge_output: dict[str, object] | None = None
    requests: list[tuple[AnalyzeRequest, str | None]] = field(default_factory=list)

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse:
        self.requests.append((request, traceparent))
        if self.error is not None:
            raise self.error
        return AnalyzeResponse(
            call_id=uid(0xCA11 + len(self.requests)),
            task_type=request.task_type,
            model_id=self.model_id,
            prompt_version=f"{request.task_type}@0123456789ab",
            taxonomy_version="v2",
            output=self.judge_output
            if request.task_type == "event_judge" and self.judge_output is not None
            else self.output,
        )

    def task_types(self) -> list[str]:
        return [r.task_type for r, _ in self.requests]


# ---- 서비스 조립 ----


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.published.append((topic, event, key, traceparent))

    def of(self, topic: str) -> list[Event]:
        return [e for t, e, _, _ in self.published if t == topic]


def context(attempt: int = 1, topic: str = "analysis.completed") -> MessageContext:
    return MessageContext(
        topic=topic, partition=0, offset=0, key=None, attempt=attempt, traceparent=TRACEPARENT
    )


def completed(
    *,
    job_type: str = "FULL",
    events: int = 0,
    tracks: int = 0,
    session: SessionRef | None = None,
    job: UUID = JOB,
    source: UUID = SOURCE,
    extra: dict[str, object] | None = None,
) -> AnalysisCompleted:
    return AnalysisCompleted.model_validate(
        {
            "eventId": str(uid(0xAC)),
            "occurredAt": "2026-11-06T10:00:00Z",
            "sourceId": str(source),
            "ownerId": str(OWNER),
            "jobId": str(job),
            "jobType": job_type,
            "normalizationId": str(NORMALIZATION),
            "durationMs": DURATION_MS,
            "session": session.model_dump(mode="json", by_alias=True) if session else None,
            "counts": AnalysisCounts(chunks=1, segments=1, events=events, tracks=tracks),
            **(extra or {}),
        }
    )


T0 = 1_762_000_000.0
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


@dataclass
class Harness:
    """가짜 analysis-api·ai-gateway·메모리 저장소·기록 발행기로 조립한 서비스."""

    api: FakeAnalysisApi = field(default_factory=FakeAnalysisApi)
    gateway: FakeGateway = field(default_factory=FakeGateway)
    storage: InMemoryStorage = field(default_factory=InMemoryStorage)
    publisher: RecordingPublisher = field(default_factory=RecordingPublisher)
    store: MemoryStore | None = field(default_factory=MemoryStore)

    def service(self) -> "AggregatorService":
        return AggregatorService(
            reader=JobReader(self.api, page_limit=2),
            gateway=self.gateway,
            storage=self.storage,
            publisher=self.publisher,
            profile=profile(),
            config=ServiceConfig(read_timeout_s=300, max_attempts=5),
            store=self.store,
            clock=lambda: T0,
            now=lambda: NOW,
        )


# ---- 실시간(STREAM) 창 ----
STREAM_SOURCE = UUID("0192fb00-0000-7000-8000-000000000010")
STREAM_JOB = UUID("0192fb00-0000-7000-8000-000000000030")
STREAM_PREVIOUS = UUID("0192fb00-0000-7000-8000-0000000000aa")
EXECUTION = UUID("0192fb00-0000-7000-8000-0000000000e0")
WINDOW_MS = 20_000


def window_id(sequence: int) -> UUID:
    return uid(0xB000 + sequence)


def stream_analyzed(
    n: int,
    sequence: int,
    *,
    at: int,
    result: VLMAnalysis,
    method: str = "rule",
    event_type: str = "fall",
    zone: UUID | None = None,
    track: str = "t5",
) -> EventAnalyzed:
    """창 `sequence`의 실시간 `event.analyzed`(탐지기 경로)."""
    chunk = window_id(sequence)
    prefix = stream_prefix(sequence)
    no_call = method in {"rule", "rule+carry"}
    return EventAnalyzed.model_validate(
        {
            "eventId": str(uid(n)),
            "occurredAt": "2026-11-06T10:00:00Z",
            "sourceId": str(STREAM_SOURCE),
            "ownerId": str(OWNER),
            "jobType": "STREAM",
            "jobId": str(STREAM_JOB),
            "chunkId": str(chunk),
            "executionId": str(EXECUTION),
            "normalizationId": str(NORMALIZATION),
            "candidateId": str(uid(n + 0x100)),
            "candidateEventType": event_type,
            "startMs": at - 1_000,
            "endMs": at,
            "evidenceFrameKeys": [f"{prefix}blurred/c{n}.jpg"],
            "judgmentMethod": method,
            "analysis": json.loads(result.model_dump_json(by_alias=True)),
            "judgments": []
            if no_call
            else [
                {
                    "stage": "judge",
                    "taskType": "event_judge",
                    "callId": str(uid(n + 0x200)),
                    "modelId": "m",
                    "promptVersion": "event_judge@0123456789ab",
                    "verificationStatus": result.verification_status,
                    "errorCode": None,
                }
            ],
            "reviewRequired": False,
            "reviewReasons": [],
            "detectorCandidate": {
                "chunkId": str(chunk),
                "executionId": str(EXECUTION),
                "triggerClass": "person",
                "score": 0.9,
                "observationCount": 3,
                "trackLocalId": track,
                "zoneId": str(zone) if zone else None,
                "zoneName": "A동 크레인 하부" if zone else None,
                "detectorProfile": "industrial-default@1",
                "mergedCandidateIds": [],
            },
        }
    )


def stream_prefix(sequence: int) -> str:
    """창 `sequence`의 실행 폴더."""
    chunk = window_id(sequence)
    return f"sources/{STREAM_SOURCE}/jobs/{STREAM_JOB}/chunks/{chunk}/executions/{EXECUTION}/"


def stream_tracks(sequence: int, *tracks: tuple[str, str, int, int]) -> tuple[TrackDetected, bytes]:
    """창의 `track.detected`와 그 트랙 요약 파일 내용. 트랙은 (로컬 ID, 클래스, 시작, 끝)이다."""
    chunk = window_id(sequence)
    prefix = stream_prefix(sequence)
    message = TrackDetected.model_validate(
        {
            "eventId": str(uid(0xD000 + sequence)),
            "occurredAt": "2026-11-06T10:00:00Z",
            "sourceId": str(STREAM_SOURCE),
            "ownerId": str(OWNER),
            "jobType": "STREAM",
            "jobId": str(STREAM_JOB),
            "normalizationId": str(NORMALIZATION),
            "origin": "chunk",
            "chunkId": str(chunk),
            "executionId": str(EXECUTION),
            "tracksKey": f"{prefix}tracks.json",
            "detectionsKey": f"{prefix}detections.json",
            "trackCount": len(tracks),
        }
    )
    body = {
        "tracks": [
            {
                "trackLocalId": local,
                "className": cls,
                "startMs": start,
                "endMs": end,
                "representativeCrops": [
                    {
                        "cropId": str(uid(0xE000 + i)),
                        "ptsMs": start,
                        "key": f"{prefix}blurred/{local}.jpg",
                    }
                ],
            }
            for i, (local, cls, start, end) in enumerate(tracks)
        ]
    }
    return message, json.dumps(body).encode()


def stream_manifest(sequence: int, *, gap_ms: int = 0, last: bool = False) -> ChunkManifest:
    chunk = window_id(sequence)
    prefix = stream_prefix(sequence)
    body: dict[str, object] = {
        "eventId": str(uid(0xF000 + sequence)),
        "occurredAt": "2026-11-06T10:00:00Z",
        "sourceId": str(STREAM_SOURCE),
        "ownerId": str(OWNER),
        "jobType": "STREAM",
        "jobId": str(STREAM_JOB),
        "chunkId": str(chunk),
        "executionId": str(EXECUTION),
        "normalizationId": str(NORMALIZATION),
        "ownedRange": {"startMs": (sequence - 1) * WINDOW_MS, "endMs": sequence * WINDOW_MS},
        "counts": {"segments": 0, "candidates": 0, "tracks": 0},
        "detector": {
            "detectorProfile": "industrial-default@1",
            "models": [],
            "supportedEvents": [],
        },
        "unsupportedEvents": {},
        "staticObjects": [],
        "referenceFrames": [{"ptsMs": 0, "key": f"{prefix}blurred/ref-0.jpg"}],
        "gapMs": gap_ms,
    }
    if last:
        body["last"] = True
    return ChunkManifest.model_validate(body)


STREAM_SESSION = SessionRef(
    session_id=uid(0x5E55), role="ROTATION", previous_source_id=STREAM_PREVIOUS
)


def stream_completed(
    sequence: int | None, *, events: int = 0, session: SessionRef | None = STREAM_SESSION
) -> AnalysisCompleted:
    """창 완료(scope chunk) 또는 sequence가 None이면 세션 완료(scope job)."""
    extra: dict[str, object] = (
        {"scope": "chunk", "chunkId": str(window_id(sequence))}
        if sequence is not None
        else {"scope": "job"}
    )
    return completed(
        job_type="STREAM",
        events=events,
        session=session,
        job=STREAM_JOB,
        source=STREAM_SOURCE,
        extra=extra,
    )


@dataclass
class FakeZones:
    """가짜 stream-service 내부 구역 API. 부른 횟수를 센다."""

    zones: tuple[ZoneInfo, ...] = ZONES
    calls: int = 0

    async def get_session_zones(self, session_id: UUID, *, deadline: Deadline) -> StreamZoneList:
        self.calls += 1
        return StreamZoneList.model_validate(
            {
                "zones": [
                    {
                        "zoneId": str(z.zone_id),
                        "name": z.name,
                        "kind": z.kind,
                        "polygon": [[0, 0], [1, 0], [1, 1]],
                    }
                    for z in self.zones
                ],
                "updatedAt": None,
            }
        )


@dataclass
class StreamHarness(Harness):
    """실시간 창 처리를 붙인 서비스. 재판정 상한·스냅샷 간격은 프로필 값을 바꿔 쓴다."""

    zones: FakeZones = field(default_factory=FakeZones)
    snapshot_every: int = 30
    judge_limit: int = 20

    def service(self) -> "AggregatorService":
        loaded = profile()
        changed = LoadedProfile(
            loaded.profile.model_copy(
                update={
                    "snapshot_every_windows": self.snapshot_every,
                    "judge_call_limit": self.judge_limit,
                }
            ),
            loaded.version,
        )
        store = self.store or MemoryStore()
        config = ServiceConfig(read_timeout_s=300, max_attempts=5)
        stream = StreamAggregator(
            store=store,
            storage=self.storage,
            publisher=self.publisher,
            gateway=self.gateway,
            zones=self.zones,
            profile=changed,
            config=config,
            clock=lambda: T0,
            now=lambda: NOW,
        )
        return AggregatorService(
            reader=JobReader(self.api, page_limit=2),
            gateway=self.gateway,
            storage=self.storage,
            publisher=self.publisher,
            profile=changed,
            config=config,
            store=store,
            stream=stream,
            clock=lambda: T0,
            now=lambda: NOW,
        )


FALL_STREAM = analysis(
    "fall", risk=True, risk_type="fall", risk_reason="쓰러진 작업자", evidence_end=12.0
)
UP_STREAM = analysis("fall", risk=False, risk_type="fall", evidence_end=33.0, after_state="일어남")
