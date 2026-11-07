# 생성 코드. scripts/generate-clients.sh로 만든다. 손으로 고치지 않는다.

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyUrl, AwareDatetime, BaseModel, ConfigDict, Field

type EventType = Annotated[
    Literal[
        "fall",
        "fire_or_smoke",
        "ppe_missing",
        "object_removed",
        "barrier_not_restored",
        "hazard_zone_access",
        "equipment_abandoned",
        "object_moved",
        "other",
    ],
    Field(
        description="이벤트 유형 8종과 other. fall(쓰러짐), fire_or_smoke(화재·연기), ppe_missing(보호구 미착용), object_removed(물건 제거), barrier_not_restored(방호 미복구), hazard_zone_access(위험구역 접근), equipment_abandoned(장비 방치: 장비 트랙이 사람 없이 정해진 시간 이상 정지), object_moved(안전시설 이동: 같은 클래스의 제거와 등장이 시간 창 안에서 짝지어짐), other(목록 밖)"
    ),
]


type ClassName = Annotated[
    str,
    Field(
        description="탐지기 클래스·트리거 클래스 이름. 영어 소문자 snake_case(예: no_helmet, missing_barrier, forklift)",
        max_length=64,
        pattern="^[a-z][a-z0-9_]*$",
    ),
]


class ZonePresence(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    track_id: Annotated[UUID, Field(alias="trackId")]
    zone_id: Annotated[UUID, Field(alias="zoneId")]
    enter_ms: Annotated[int, Field(alias="enterMs", ge=0)]
    exit_ms: Annotated[
        int, Field(alias="exitMs", description="영상 끝까지 나가지 않았으면 마지막 관측 시각", ge=0)
    ]


class Zone(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zone_id: Annotated[UUID, Field(alias="zoneId")]
    name: Annotated[str, Field(max_length=100, min_length=1)]
    kind: Annotated[
        Literal["hazard", "aisle", "general"],
        Field(description="hazard는 위험구역(체류·배회 규칙 대상), aisle은 통로"),
    ]


class Items(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zone_id: Annotated[
        str,
        Field(
            alias="zoneId",
            description="구역 ID. 구역이 없는 영상·구역 밖 이벤트는 global",
            pattern="^(global|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        ),
    ]
    state: Annotated[str, Field(max_length=500, min_length=1)]


class ItemsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    risk_id: Annotated[UUID, Field(alias="riskId")]
    zone_id: Annotated[
        str,
        Field(
            alias="zoneId",
            description="구역 ID. 구역이 없는 영상·구역 밖 이벤트는 global",
            pattern="^(global|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        ),
    ]
    risk_type: Annotated[str, Field(alias="riskType", max_length=100, min_length=1)]
    reason: Annotated[str, Field(max_length=1000)]
    first_seen_ms: Annotated[int, Field(alias="firstSeenMs", ge=0, le=9223372036854775807)]


class Box(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    x: Annotated[float, Field(ge=0.0, le=1.0)]
    y: Annotated[float, Field(ge=0.0, le=1.0)]
    w: Annotated[float, Field(ge=0.0, le=1.0)]
    h: Annotated[float, Field(ge=0.0, le=1.0)]


type ImageKey = Annotated[
    str,
    Field(
        description="블러 사본 경로. 이 메시지의 sourceId·jobId·chunkId·executionId 폴더의 blurred/ 아래여야 한다(ai-gateway가 블러 사본만 받는 규칙과 같다).",
        pattern="^sources/[0-9a-f-]{36}/jobs/[0-9a-f-]{36}/chunks/[0-9a-f-]{36}/executions/[0-9a-f-]{36}/blurred/[^/?#]+\\.(jpg|jpeg|png|webp)$",
    ),
]


class SignedFrame(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    pts_ms: Annotated[int | None, Field(alias="ptsMs", ge=0)]
    url: Annotated[AnyUrl, Field(description="블러 사본의 서명 주소(1시간)")]


class EventSummary(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    unverified_reason: Annotated[
        Literal["budget_exceeded", "model_unavailable", "call_cap_reached"] | None,
        Field(
            alias="unverifiedReason",
            description="verificationStatus가 unverified인 이유(v5.1, event.analyzed.unverifiedReason 그대로). 없으면 사유 없음. 생산자 analysis-api 확인 필요 (SPEC-031 R-23)",
        ),
    ] = None
    event_id: Annotated[UUID, Field(alias="eventId")]
    source_id: Annotated[UUID, Field(alias="sourceId")]
    event_type: Annotated[EventType, Field(alias="eventType")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]
    seek_ms: Annotated[
        int, Field(alias="seekMs", description="플레이어 이동 시각. max(0, startMs − 2000)", ge=0)
    ]
    severity: Literal["low", "medium", "high"] | None
    verification_status: Annotated[
        Literal["confirmed", "rejected", "unverified", "unknown"], Field(alias="verificationStatus")
    ]
    judgment_method: Annotated[
        str,
        Field(
            alias="judgmentMethod",
            description="rule·rule+vlm·rule+vlm+escalate·vector+vlm·vector+vlm+escalate·inventory+state_compare",
            max_length=50,
        ),
    ]
    description: Annotated[str | None, Field(max_length=1000)]
    zone_id: Annotated[UUID | None, Field(alias="zoneId")]
    zone_name: Annotated[str | None, Field(alias="zoneName")]
    track_id: Annotated[UUID | None, Field(alias="trackId")]
    review_required: Annotated[bool, Field(alias="reviewRequired")]
    review_state: Annotated[
        Literal["UNREVIEWED", "CONFIRMED", "DISMISSED"], Field(alias="reviewState")
    ]
    thumbnail_url: Annotated[AnyUrl | None, Field(alias="thumbnailUrl")]
    merged_into: Annotated[
        UUID | None,
        Field(
            alias="mergedInto",
            description="청크 경계 병합으로 다른 이벤트에 합쳐졌으면 대표 eventId. 목록 기본값은 대표만 보여 준다",
        ),
    ]
    origin: Annotated[
        Literal["candidate", "rule", "inventory"],
        Field(
            description="candidate: event.analyzed, rule: aggregator 시간 규칙, inventory: 재고 비교·장비 방치"
        ),
    ]


class Judgment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    task_type: Annotated[str, Field(alias="taskType")]
    model_id: Annotated[str | None, Field(alias="modelId")]
    verification_status: Annotated[str, Field(alias="verificationStatus")]
    event_detected: Annotated[bool | None, Field(alias="eventDetected")]


class EventDetail(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    event: EventSummary
    analysis: Annotated[
        dict[str, object], Field(description="VLMAnalysis(`events/_vlm-analysis.schema.json`)")
    ]
    evidence_frames: Annotated[list[SignedFrame], Field(alias="evidenceFrames", max_length=12)]
    judgments: list[Judgment]
    review_reasons: Annotated[list[str], Field(alias="reviewReasons")]
    detector_profile: Annotated[str | None, Field(alias="detectorProfile")]
    versions: Annotated[
        dict[str, str], Field(description="detector·model·prompt·taxonomy·analysis 버전")
    ]
    review_note: Annotated[str | None, Field(alias="reviewNote", max_length=500)]
    reviewed_by: Annotated[UUID | None, Field(alias="reviewedBy")]
    reviewed_at: Annotated[AwareDatetime | None, Field(alias="reviewedAt")]


class ReviewEventRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    review_state: Annotated[
        Literal["UNREVIEWED", "CONFIRMED", "DISMISSED"], Field(alias="reviewState")
    ]
    note: Annotated[str | None, Field(max_length=500)] = None


class Exposure(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    active_seconds: Annotated[float, Field(alias="activeSeconds")]
    exposure_count: Annotated[int, Field(alias="exposureCount")]
    exposure_seconds: Annotated[float, Field(alias="exposureSeconds")]


class Risk(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    risk_id: Annotated[UUID, Field(alias="riskId")]
    zone_id: Annotated[str, Field(alias="zoneId", description="구역 ID 또는 global")]
    zone_name: Annotated[str | None, Field(alias="zoneName")]
    risk_type: Annotated[str, Field(alias="riskType")]
    status: Literal["active", "resolved"]
    reason: str
    first_seen_ms: Annotated[int, Field(alias="firstSeenMs", ge=0)]
    last_seen_ms: Annotated[int, Field(alias="lastSeenMs", ge=0)]
    resolved_at_ms: Annotated[int | None, Field(alias="resolvedAtMs", ge=0)]
    related_event_ids: Annotated[list[UUID], Field(alias="relatedEventIds")]
    carried_over: Annotated[bool, Field(alias="carriedOver")]
    exposure: Exposure
    forecast: Annotated[
        dict[str, object] | None,
        Field(
            description="SPEC-028 risk_forecast 결과(`events/_risk-forecast.schema.json`). 화면에는 참고용으로만 표시한다"
        ),
    ]


class RiskList(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    job_id: Annotated[UUID | None, Field(alias="jobId")]
    items: list[Risk]


class Snapshot(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    snapshot_id: Annotated[UUID, Field(alias="snapshotId")]
    zone_id: Annotated[str, Field(alias="zoneId")]
    zone_name: Annotated[str | None, Field(alias="zoneName")]
    timestamp_ms: Annotated[int, Field(alias="timestampMs", ge=0)]
    previous_state: Annotated[str | None, Field(alias="previousState")]
    current_state: Annotated[str | None, Field(alias="currentState")]
    related_event_id: Annotated[UUID, Field(alias="relatedEventId")]


class SnapshotList(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    job_id: Annotated[UUID | None, Field(alias="jobId")]
    items: list[Snapshot]


class MissingCounts(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    events: Annotated[int | None, Field(ge=0)] = None
    tracks: Annotated[int | None, Field(ge=0)] = None


class UnverifiedCounts(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    budget_exceeded: Annotated[int | None, Field(ge=0)] = None
    call_cap_reached: Annotated[int | None, Field(ge=0)] = None
    model_unavailable: Annotated[int | None, Field(ge=0)] = None


class Highlight(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    at_ms: Annotated[int, Field(alias="atMs", ge=0)]
    text: str


class Summary(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    status: Literal["generated", "not_configured", "failed", "empty"]
    text: str | None
    highlights: list[Highlight]


type CountsAdditionalProperty = Annotated[int, Field(ge=0)]


class Session(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    session_id: Annotated[UUID, Field(alias="sessionId")]
    role: Literal["BEFORE", "DURING", "AFTER"]
    previous_source_id: Annotated[UUID | None, Field(alias="previousSourceId")]
    initial_state_used: Annotated[bool, Field(alias="initialStateUsed")]


class TimelineItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    at_ms: Annotated[int, Field(alias="atMs", ge=0)]
    kind: Literal["event", "rule_event", "risk_activated", "risk_resolved", "inventory_event"]
    ref_id: Annotated[UUID, Field(alias="refId")]
    event_type: Annotated[str | None, Field(alias="eventType")]
    zone_id: Annotated[str | None, Field(alias="zoneId")]
    severity: str | None


class SourceSummary(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    partial: Annotated[
        bool | None,
        Field(
            description="개수 대조 재시도 상한 뒤 빠진 이벤트가 있는 채로 집계됨(v5.1, source.completed.partial 그대로). 없으면 false. 생산자 analysis-api 확인 필요 (SPEC-031 R-22)"
        ),
    ] = None
    missing_counts: Annotated[
        MissingCounts | None,
        Field(
            alias="missingCounts",
            description="partial일 때 빠진 수(source.completed.missingCounts 그대로)",
        ),
    ] = None
    revision: Annotated[
        int | None,
        Field(description="활성 결과의 발행 차수(source.completed.revision). 없으면 1", ge=1),
    ] = None
    unverified_counts: Annotated[
        UnverifiedCounts | None,
        Field(
            alias="unverifiedCounts",
            description="활성 분석 버전에서 unverified로 남은 이벤트 수를 사유별로 센 값(v5.1). 화면이 첫 쪽만 세지 않게 한다. 생산자 analysis-api 확인 필요 (SPEC-031 R-23)",
        ),
    ] = None
    source_id: Annotated[UUID, Field(alias="sourceId")]
    status: Annotated[
        Literal["NOT_ANALYZED", "READY"],
        Field(description="활성 분석 버전이 있으면 READY. 진행 상태는 알림(job.status)으로 본다"),
    ]
    job_id: Annotated[UUID | None, Field(alias="jobId", description="활성 분석 버전의 job")]
    summary: Summary | None
    counts: Annotated[
        dict[str, CountsAdditionalProperty],
        Field(description="events·confirmedEvents·activeRisks·resolvedRisks 등"),
    ]
    unsupported_events: Annotated[
        dict[str, str],
        Field(
            alias="unsupportedEvents",
            description='판정하지 않은 이벤트 유형과 이유. 화면에 "이 분석에서 판정하지 않은 이벤트"로 표시',
        ),
    ]
    session: Session | None
    timeline: list[TimelineItem]
    analyzed_at: Annotated[AwareDatetime | None, Field(alias="analyzedAt")]


class NormBox(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    x: Annotated[float, Field(ge=0.0, le=1.0)]
    y: Annotated[float, Field(ge=0.0, le=1.0)]
    w: Annotated[float, Field(ge=0.0, le=1.0)]
    h: Annotated[float, Field(ge=0.0, le=1.0)]


class Box1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    pts_ms: Annotated[int, Field(alias="ptsMs", ge=0)]
    box: NormBox


class OverlayTrack(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    track_id: Annotated[UUID, Field(alias="trackId")]
    class_name: Annotated[str, Field(alias="className")]
    has_event: Annotated[
        bool, Field(alias="hasEvent", description="이 트랙에 이벤트가 있으면 true(강조 표시)")
    ]
    boxes: list[Box1]


class Overlay(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: Annotated[UUID, Field(alias="sourceId")]
    from_ms: Annotated[int, Field(alias="fromMs", ge=0)]
    to_ms: Annotated[int, Field(alias="toMs", ge=0)]
    tracks: Annotated[list[OverlayTrack], Field(max_length=500)]


class TrackAttributes(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    track_id: Annotated[UUID, Field(alias="trackId")]
    class_name: Annotated[str, Field(alias="className")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]
    attribute_status: Annotated[
        Literal["NONE", "REQUESTED", "READY", "FAILED"],
        Field(alias="attributeStatus", description="트랙 속성 상태(overview 상태 표 트랙 속성 행)"),
    ]
    attributes: Annotated[
        dict[str, object] | None, Field(description="READY면 `events/_track-attribute.schema.json`")
    ]
    request_id: Annotated[UUID | None, Field(alias="requestId")]


class InspectResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    t_ms: Annotated[int, Field(alias="tMs", ge=0)]
    x: float
    y: float
    hit: Annotated[bool, Field(description="그 점을 덮는 트랙 박스가 있으면 true")]
    box: NormBox | None
    track: TrackAttributes | None


class AskRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    question: Annotated[str, Field(max_length=200, min_length=1)]
    t_ms: Annotated[
        int,
        Field(alias="tMs", description="질문할 때 화면 시각. 장면 프레임을 고르는 데 쓴다", ge=0),
    ]


class AskAccepted(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    request_id: Annotated[UUID, Field(alias="requestId")]
    status: Literal["REQUESTED"]


class QuestionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    request_id: Annotated[UUID, Field(alias="requestId")]
    track_id: Annotated[UUID, Field(alias="trackId")]
    question: str
    status: Literal["REQUESTED", "READY", "FAILED"]
    answer: Annotated[
        dict[str, object] | None, Field(description="READY면 `events/_object-answer.schema.json`")
    ]
    asked_at: Annotated[AwareDatetime, Field(alias="askedAt")]


class PolygonItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    x: Annotated[float, Field(ge=0.0, le=1.0)]
    y: Annotated[float, Field(ge=0.0, le=1.0)]


class ZoneDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zone_id: Annotated[
        UUID | None, Field(alias="zoneId", description="새 구역은 null로 보내고 서버가 만든다")
    ]
    name: Annotated[str, Field(max_length=100, min_length=1)]
    kind: Literal["hazard", "aisle", "general"]
    polygon: Annotated[
        list[PolygonItem],
        Field(
            description="프레임 크기로 나눈 꼭짓점. 자기 자신과 교차하지 않아야 한다",
            max_length=32,
            min_length=3,
        ),
    ]


class ZoneList(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: Annotated[UUID, Field(alias="sourceId")]
    items: Annotated[list[ZoneDefinition], Field(max_length=20)]
    updated_at: Annotated[AwareDatetime | None, Field(alias="updatedAt")]
    reanalysis_needed: Annotated[
        bool,
        Field(
            alias="reanalysisNeeded",
            description="구역을 바꾼 뒤 아직 재분석하지 않았으면 true. 구역은 다음 분석부터 반영된다",
        ),
    ]


class PutZonesRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: Annotated[list[ZoneDefinition], Field(max_length=20)]


class Error(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    field: Annotated[str, Field(examples=["email"])]
    message: Annotated[str, Field(examples=["올바른 형식의 이메일 주소여야 합니다"])]


class ProblemDetail(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: AnyUrl
    title: Annotated[str, Field(examples=["Conflict"])]
    status: Annotated[int, Field(examples=[409])]
    detail: Annotated[str | None, Field(examples=["이미 가입된 이메일입니다."])] = None
    instance: Annotated[str | None, Field(examples=["/api/v1/auth/signup"])] = None
    code: Annotated[
        str,
        Field(
            description="서비스별 오류 코드 (<DOMAIN>_<REASON>)", examples=["AUTH_EMAIL_DUPLICATED"]
        ),
    ]
    trace_id: Annotated[str | None, Field(alias="traceId", description="요청 추적 ID")] = None
    errors: Annotated[
        list[Error] | None,
        Field(description="요청 본문 검증 오류일 때 필드별 오류 목록. 그 밖의 오류에는 없다."),
    ] = None


type Role = Literal["USER", "OPERATOR", "ADMIN"]


class PageMeta(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    page: Annotated[int, Field(ge=0)]
    size: Annotated[int, Field(ge=1)]
    total_elements: Annotated[int, Field(alias="totalElements")]
    total_pages: Annotated[int, Field(alias="totalPages")]


class InternalV1SourcesSourceIdJobsJobIdZonePresencesGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[ZonePresence]
    next_cursor: Annotated[str | None, Field(alias="nextCursor", max_length=512)]


class InternalV1SourcesSourceIdZonesGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: Annotated[list[Zone], Field(max_length=100)]


class ApiV1AnalysisSourcesSourceIdEventsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[EventSummary]
    page: PageMeta


class ApiV1AnalysisEventsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[EventSummary]
    page: PageMeta


class JobEvent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    event_id: Annotated[UUID, Field(alias="eventId")]
    chunk_id: Annotated[
        UUID | None,
        Field(alias="chunkId", description="이벤트를 낸 청크. 청크 밖에서 만든 이벤트면 null"),
    ]
    event_type: Annotated[EventType, Field(alias="eventType")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]
    track_id: Annotated[UUID | None, Field(alias="trackId")]
    zone_id: Annotated[UUID | None, Field(alias="zoneId")]
    zone_name: Annotated[str | None, Field(alias="zoneName", max_length=100)]
    judgment_method: Annotated[
        Literal["rule", "rule+vlm", "rule+vlm+escalate", "vector+vlm", "vector+vlm+escalate"],
        Field(alias="judgmentMethod"),
    ]
    score: Annotated[
        float | None,
        Field(
            description="후보 점수(탐지기 후보는 detectorCandidate.score, 벡터 후보는 vectorCandidate.score)",
            ge=-1.0,
            le=1.0,
        ),
    ]
    review_required: Annotated[bool, Field(alias="reviewRequired")]
    analysis: Annotated[
        dict[str, object],
        Field(
            description="판정 결과 VLMAnalysis. 형식은 `events/_vlm-analysis.schema.json`이다(이 파일에서 직접 참조하지 않는 이유는 검사 도구가 그 스키마의 `$id` 기준 상대 참조를 풀지 못해서다)"
        ),
    ]


class JobTrack(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    track_id: Annotated[UUID, Field(alias="trackId")]
    class_name: Annotated[ClassName, Field(alias="className")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]


class ItemsModel1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    object_id: Annotated[
        str,
        Field(
            alias="objectId",
            description="영상 안 정적 객체 ID(처음 본 청크의 chunkId 앞 8자 + objectLocalId)",
            pattern="^[A-Za-z0-9_-]{1,64}$",
        ),
    ]
    class_name: Annotated[ClassName, Field(alias="className")]
    box: Annotated[Box, Field(description="프레임 크기로 나눈 0~1 좌표, 왼쪽 위 기준")]
    zone_id: Annotated[UUID | None, Field(alias="zoneId")]
    first_seen_ms: Annotated[int, Field(alias="firstSeenMs", ge=0, le=9223372036854775807)]
    last_seen_ms: Annotated[int, Field(alias="lastSeenMs", ge=0, le=9223372036854775807)]
    frame_key: Annotated[ImageKey, Field(alias="frameKey")]


class InternalV1SourcesSourceIdJobsJobIdEventsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[JobEvent]
    next_cursor: Annotated[str | None, Field(alias="nextCursor", max_length=512)]


class InternalV1SourcesSourceIdJobsJobIdTracksGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[JobTrack]
    next_cursor: Annotated[str | None, Field(alias="nextCursor", max_length=512)]


class FinalState(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: Annotated[UUID, Field(alias="sourceId")]
    job_id: Annotated[UUID, Field(alias="jobId", description="활성 분석 버전의 job")]
    zone_states: Annotated[list[Items], Field(alias="zoneStates")]
    active_risks: Annotated[list[ItemsModel], Field(alias="activeRisks")]
    static_objects: Annotated[
        list[ItemsModel1] | None,
        Field(
            alias="staticObjects",
            description="활성 분석 버전 끝의 정적 객체 재고(source.completed 결과 파일의 inventory). 저장된 재고가 없으면 빈 배열이다 (SPEC-028)",
        ),
    ] = None
