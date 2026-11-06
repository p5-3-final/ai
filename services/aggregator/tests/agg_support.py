"""aggregator 테스트가 함께 쓰는 입력 빌더·계약 검증 도우미."""

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from uuid import UUID

from aggregator.model import AggregateInput, InputEvent, Presence, TrackSpan, ZoneInfo
from aggregator.profile import DEFAULT_PROFILE_PATH, LoadedProfile, load_aggregate_profile
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
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
from vlm_contracts.events import EventType, SessionRef, VLMAnalysis

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
