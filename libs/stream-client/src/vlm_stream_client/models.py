# 생성 코드. scripts/generate-clients.sh로 만든다. 손으로 고치지 않는다.

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyUrl, AwareDatetime, BaseModel, ConfigDict, Field


class CameraCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: Annotated[str, Field(max_length=100, min_length=1)]
    rtsp_secret_ref: Annotated[
        str,
        Field(
            alias="rtspSecretRef",
            description="RTSP 주소·자격 증명이 든 Secrets Manager 키 이름. 주소 자체는 받지 않는다",
            max_length=200,
            pattern="^[A-Za-z0-9/_.:-]+$",
        ),
    ]
    profile_version: Annotated[
        str, Field(alias="profileVersion", pattern="^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    ]


class Camera(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    camera_id: Annotated[UUID, Field(alias="cameraId")]
    owner_id: Annotated[UUID, Field(alias="ownerId")]
    name: Annotated[str, Field(max_length=100, min_length=1)]
    rtsp_secret_ref: Annotated[
        str, Field(alias="rtspSecretRef", max_length=200, pattern="^[A-Za-z0-9/_.:-]+$")
    ]
    profile_version: Annotated[
        str, Field(alias="profileVersion", pattern="^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    ]
    status: Annotated[
        Literal["IDLE", "STARTING", "LIVE", "STOPPING", "FAILED"],
        Field(description="활성 세션의 상태. 없으면 IDLE"),
    ]
    active_session_id: Annotated[UUID | None, Field(alias="activeSessionId")]
    created_at: Annotated[AwareDatetime, Field(alias="createdAt")]


type PolygonItemItem = Annotated[float, Field(ge=0.0, le=1.0)]


type PolygonItem = Annotated[list[PolygonItemItem], Field(max_length=2, min_length=2)]


class Zone(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zone_id: Annotated[
        UUID,
        Field(alias="zoneId", description="새 구역은 클라이언트가 UUID를 만든다. 같은 ID면 갱신"),
    ]
    name: Annotated[str, Field(max_length=100, min_length=1)]
    kind: Literal["hazard", "aisle", "storage", "general"]
    polygon: Annotated[
        list[PolygonItem], Field(description="정규화 좌표(0~1) 꼭짓점", max_length=64, min_length=3)
    ]


class ZoneList(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zones: Annotated[list[Zone], Field(max_length=100)]
    updated_at: Annotated[AwareDatetime | None, Field(alias="updatedAt")]


class Session(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    session_id: Annotated[UUID, Field(alias="sessionId")]
    source_id: Annotated[UUID, Field(alias="sourceId", description="sessionId와 같다")]
    job_id: Annotated[UUID, Field(alias="jobId", description="STREAM Job")]
    camera_id: Annotated[UUID, Field(alias="cameraId")]
    status: Literal["STARTING", "LIVE", "STOPPING", "CLOSED", "FAILED"]
    previous_source_id: Annotated[
        UUID | None, Field(alias="previousSourceId", description="24시간 회전으로 이어진 직전 세션")
    ]
    started_at: Annotated[AwareDatetime, Field(alias="startedAt")]
    closed_at: Annotated[AwareDatetime | None, Field(alias="closedAt")]
    last_window_sequence: Annotated[int, Field(alias="lastWindowSequence", ge=0)]
    partial_windows: Annotated[int, Field(alias="partialWindows", ge=0)]


class Window(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    chunk_id: Annotated[UUID, Field(alias="chunkId")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]
    url: AnyUrl


class Playback(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    mode: Literal["live", "archive"]
    hls_url: Annotated[
        AnyUrl | None, Field(alias="hlsUrl", description="live일 때 라이브 HLS 서명 주소")
    ]
    expires_at: Annotated[AwareDatetime, Field(alias="expiresAt")]
    windows: Annotated[
        list[Window], Field(description="archive일 때 창 파일 서명 주소 목록", max_length=5000)
    ]


class Zone1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    zone_id: Annotated[str, Field(alias="zoneId")]
    zone_name: Annotated[str | None, Field(alias="zoneName")]
    current_state: Annotated[str | None, Field(alias="currentState", max_length=500)]
    updated_at_ms: Annotated[int, Field(alias="updatedAtMs", ge=0)]


class ActiveRisk(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    risk_id: Annotated[UUID, Field(alias="riskId")]
    zone_id: Annotated[str, Field(alias="zoneId")]
    risk_type: Annotated[str, Field(alias="riskType", max_length=64)]
    reason: Annotated[str | None, Field(max_length=500)]
    first_seen_ms: Annotated[int, Field(alias="firstSeenMs", ge=0)]
    last_seen_ms: Annotated[int, Field(alias="lastSeenMs", ge=0)]
    active_seconds: Annotated[float, Field(alias="activeSeconds", ge=0.0)]
    exposure_count: Annotated[int, Field(alias="exposureCount", ge=0)]
    exposure_seconds: Annotated[float, Field(alias="exposureSeconds", ge=0.0)]


class SessionState(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    sequence: Annotated[
        int, Field(description="반영한 마지막 state.updated.sequence. 0이면 아직 없음", ge=0)
    ]
    as_of_ms: Annotated[int, Field(alias="asOfMs", ge=0)]
    zones: list[Zone1]
    active_risks: Annotated[list[ActiveRisk], Field(alias="activeRisks")]
    partial_windows: Annotated[int, Field(alias="partialWindows", ge=0)]
    gap_ms_total: Annotated[int, Field(alias="gapMsTotal", ge=0)]


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


class Alert(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    candidate_id: Annotated[UUID, Field(alias="candidateId")]
    status: Literal["pending", "confirmed", "dismissed"]
    event_type: Annotated[EventType, Field(alias="eventType")]
    reason: Literal["severity_high", "active_risk_zone"]
    severity: Literal["low", "medium", "high"]
    observed_ms: Annotated[int, Field(alias="observedMs", ge=0)]
    pending_at: Annotated[AwareDatetime, Field(alias="pendingAt")]
    decided_at: Annotated[AwareDatetime | None, Field(alias="decidedAt")]
    dismiss_reason: Annotated[
        Literal["rejected", "unverified", "unknown"] | None, Field(alias="dismissReason")
    ]
    description: Annotated[str | None, Field(max_length=2000)]
    evidence_frame_urls: Annotated[list[AnyUrl], Field(alias="evidenceFrameUrls", max_length=4)]


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


class ApiV1StreamCamerasGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[Camera]
    page: PageMeta


class ApiV1StreamCamerasCameraIdSessionsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[Session]
    page: PageMeta


class ApiV1StreamSessionsSessionIdAlertsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[Alert]
    page: PageMeta


class LiveEvent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    event_id: Annotated[UUID, Field(alias="eventId")]
    chunk_id: Annotated[UUID, Field(alias="chunkId")]
    event_type: Annotated[EventType, Field(alias="eventType")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0)]
    severity: Literal["low", "medium", "high"] | None
    verification_status: Annotated[
        Literal["confirmed", "rejected", "unverified", "unknown"], Field(alias="verificationStatus")
    ]
    judgment_method: Annotated[str, Field(alias="judgmentMethod", max_length=50)]
    unverified_reason: Annotated[
        Literal["budget_exceeded", "model_unavailable", "call_cap_reached"] | None,
        Field(alias="unverifiedReason"),
    ]
    track_id: Annotated[UUID | None, Field(alias="trackId")]
    zone_id: Annotated[str | None, Field(alias="zoneId")]
    description: Annotated[str | None, Field(max_length=2000)]
    evidence_frame_urls: Annotated[list[AnyUrl], Field(alias="evidenceFrameUrls", max_length=12)]


class ApiV1StreamSessionsSessionIdEventsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[LiveEvent]
    page: PageMeta
