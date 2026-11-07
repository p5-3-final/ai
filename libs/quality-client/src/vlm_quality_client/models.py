# 생성 코드. scripts/generate-clients.sh로 만든다. 손으로 고치지 않는다.

from __future__ import annotations

from datetime import date as date_aliased
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


type ReviewReason = Annotated[str, Field(max_length=100)]


type EvidenceFrameKey = Annotated[str, Field(max_length=1024)]


class CreateGoldenSetRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: Annotated[str, Field(pattern="^[a-z0-9][a-z0-9-]{0,62}$")]
    split: Literal["dev", "eval"]
    description: Annotated[str | None, Field(max_length=500)] = None


class GoldenSet(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    set_id: Annotated[UUID, Field(alias="setId")]
    name: str
    split: Literal["dev", "eval"]
    description: str | None
    draft_entry_count: Annotated[int, Field(alias="draftEntryCount", ge=0)]
    latest_version: Annotated[int | None, Field(alias="latestVersion", ge=1)]
    baseline_run_id: Annotated[UUID | None, Field(alias="baselineRunId")]
    created_at: Annotated[AwareDatetime, Field(alias="createdAt")]
    origin: Annotated[
        Literal["review", "import"],
        Field(description="review: 검토 라벨, import: 외부 데이터셋(SPEC-030)"),
    ]


class AddGoldenSetEntryRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    review_item_id: Annotated[UUID, Field(alias="reviewItemId")]


class GoldenSetVersion(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    set_id: Annotated[UUID, Field(alias="setId")]
    version: Annotated[int, Field(ge=1)]
    entry_count: Annotated[int, Field(alias="entryCount", ge=1)]
    manifest_key: Annotated[
        str,
        Field(
            alias="manifestKey",
            description="Golden Set 저장소의 매니페스트(`quality/v1/golden-manifest.schema.json`)",
            pattern="^golden/[0-9a-f-]{36}/v[0-9]+/manifest[.]json$",
        ),
    ]
    frozen_at: Annotated[AwareDatetime, Field(alias="frozenAt")]
    frozen_by: Annotated[UUID, Field(alias="frozenBy")]


class SetBaselineRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    run_id: Annotated[UUID, Field(alias="runId")]


class Metric(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: Annotated[
        str,
        Field(
            description="예: candidate_recall, judge_precision, temporal_iou_mean, recall_at_50",
            pattern="^[a-z][a-z0-9_]{0,63}$",
        ),
    ]
    event_type: Annotated[
        EventType | None, Field(alias="eventType", description="유형별 지표면 유형, 전체면 null")
    ]
    value: float
    higher_is_better: Annotated[bool, Field(alias="higherIsBetter")]


class RunVersions(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    taxonomy: Annotated[str | None, Field(max_length=200)]
    detector_profile: Annotated[str | None, Field(alias="detectorProfile", max_length=200)]
    model_id: Annotated[str | None, Field(alias="modelId", max_length=200)]
    prompt_version: Annotated[str | None, Field(alias="promptVersion", max_length=200)]
    embedding_model: Annotated[str | None, Field(alias="embeddingModel", max_length=200)]


class SubmitEvaluationRunRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    run_id: Annotated[
        UUID, Field(alias="runId", description="평가 잡이 만든 ID. 다시 보내도 한 번만 저장된다")
    ]
    golden_set_id: Annotated[UUID, Field(alias="goldenSetId")]
    golden_set_version: Annotated[int, Field(alias="goldenSetVersion", ge=1)]
    kind: Literal["event", "search", "track_attribute"]
    versions: RunVersions
    metrics: Annotated[list[Metric], Field(max_length=500, min_length=1)]
    started_at: Annotated[AwareDatetime, Field(alias="startedAt")]
    finished_at: Annotated[AwareDatetime, Field(alias="finishedAt")]
    trigger: Literal["ci", "scheduled", "manual"]
    commit_sha: Annotated[str | None, Field(alias="commitSha", pattern="^[0-9a-f]{7,40}$")]


class Failure(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    name: str
    event_type: Annotated[str | None, Field(alias="eventType")]
    baseline: float
    value: Annotated[
        float | None,
        Field(
            description="이번 실행의 값. 기준 실행에만 있는 지표(이번 실행에서 빠짐)면 null이다 (SPEC-029 R-18)"
        ),
    ]


class GateResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    baseline_run_id: Annotated[
        UUID | None, Field(alias="baselineRunId", description="받을 때의 기준 실행. 없으면 null")
    ]
    passed: Annotated[bool | None, Field(description="기준이 없으면 null")]
    tolerance: Annotated[float, Field(description="허용 하락폭(절댓값, 설정)", ge=0.0)]
    failures: list[Failure]


class StageStat(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    path: str
    stage_id: Annotated[str, Field(alias="stageId")]
    count: Annotated[int, Field(ge=0)]
    total_ms: Annotated[float, Field(alias="totalMs")]
    p50_ms: Annotated[float, Field(alias="p50Ms")]
    p95_ms: Annotated[float, Field(alias="p95Ms")]
    error_count: Annotated[int, Field(alias="errorCount", ge=0)]


class SourceStageReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    source_id: Annotated[UUID, Field(alias="sourceId")]
    record_count: Annotated[int, Field(alias="recordCount", ge=0)]
    stages: list[StageStat]


class Day(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    date: date_aliased
    pending_count: Annotated[int, Field(alias="pendingCount", ge=0)]
    confirmed_count: Annotated[int, Field(alias="confirmedCount", ge=0)]
    dismissed_count: Annotated[int, Field(alias="dismissedCount", ge=0)]
    first_alert_p50_ms: Annotated[
        float | None,
        Field(
            alias="firstAlertP50Ms",
            description="event-analyzer 단계 `analyzer.alert` 측정(긴급 후보를 받은 시각부터 pending alert.raised 발행까지)의 백분위. Kafka 대기 시간은 들어가지 않는다",
        ),
    ]
    first_alert_p95_ms: Annotated[float | None, Field(alias="firstAlertP95Ms")]
    judge_p50_ms: Annotated[
        float | None, Field(alias="judgeP50Ms", description="pending부터 confirmed·dismissed까지")
    ]
    judge_p95_ms: Annotated[float | None, Field(alias="judgeP95Ms")]


class AlertReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    from_: Annotated[date_aliased, Field(alias="from")]
    to: date_aliased
    camera_id: Annotated[UUID | None, Field(alias="cameraId")]
    days: list[Day]


class Day1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    date: date_aliased
    count: Annotated[int, Field(ge=0)]
    p50_ms: Annotated[float, Field(alias="p50Ms")]
    p95_ms: Annotated[float, Field(alias="p95Ms")]
    error_count: Annotated[int, Field(alias="errorCount", ge=0)]


class StageTrend(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    stage_id: Annotated[str, Field(alias="stageId")]
    from_: Annotated[date_aliased, Field(alias="from")]
    to: date_aliased
    version_key: Annotated[str | None, Field(alias="versionKey")]
    version_value: Annotated[str | None, Field(alias="versionValue")]
    days: list[Day1]


class EvaluationRun(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    run_id: Annotated[
        UUID, Field(alias="runId", description="평가 잡이 만든 ID. 다시 보내도 한 번만 저장된다")
    ]
    golden_set_id: Annotated[UUID, Field(alias="goldenSetId")]
    golden_set_version: Annotated[int, Field(alias="goldenSetVersion", ge=1)]
    kind: Literal["event", "search", "track_attribute"]
    versions: RunVersions
    metrics: Annotated[list[Metric], Field(max_length=500, min_length=1)]
    started_at: Annotated[AwareDatetime, Field(alias="startedAt")]
    finished_at: Annotated[AwareDatetime, Field(alias="finishedAt")]
    trigger: Literal["ci", "scheduled", "manual"]
    commit_sha: Annotated[str | None, Field(alias="commitSha", pattern="^[0-9a-f]{7,40}$")]
    gate: GateResult
    received_at: Annotated[AwareDatetime, Field(alias="receivedAt")]


class CreateDatasetImportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    manifest_key: Annotated[
        str,
        Field(
            alias="manifestKey",
            description="가져오기 버킷의 매니페스트 경로",
            pattern="^imports/[A-Za-z0-9._-]{1,100}/manifest[.]json$",
        ),
    ]
    eval_ratio: Annotated[float | None, Field(alias="evalRatio", ge=0.0, le=1.0)] = 0.2
    split_seed: Annotated[int | None, Field(alias="splitSeed", ge=0)] = 0


class Counts(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    videos: Annotated[int, Field(ge=0)]
    registered: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    labels: Annotated[int, Field(ge=0)]
    dev: Annotated[int, Field(ge=0)]
    eval: Annotated[int, Field(ge=0)]


class GoldenSets(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    dev_set_id: Annotated[UUID, Field(alias="devSetId")]
    eval_set_id: Annotated[UUID, Field(alias="evalSetId")]
    dev_version: Annotated[int | None, Field(alias="devVersion")]
    eval_version: Annotated[int | None, Field(alias="evalVersion")]


class Error(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    external_id: Annotated[str | None, Field(alias="externalId")]
    code: str
    message: Annotated[str, Field(max_length=500)]


class DatasetImport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    import_id: Annotated[UUID, Field(alias="importId")]
    dataset_name: Annotated[
        str | None, Field(alias="datasetName", description="매니페스트를 읽기 전에는 null")
    ]
    manifest_key: Annotated[str, Field(alias="manifestKey")]
    eval_ratio: Annotated[float, Field(alias="evalRatio")]
    split_seed: Annotated[int, Field(alias="splitSeed")]
    status: Annotated[
        Literal["PENDING", "VALIDATING", "REGISTERING", "COMPLETED", "FAILED"],
        Field(
            description="FAILED는 매니페스트 자체가 잘못됐거나 등록된 영상이 하나도 없을 때. 일부 영상 실패는 COMPLETED와 errors"
        ),
    ]
    counts: Counts
    golden_sets: Annotated[GoldenSets | None, Field(alias="goldenSets")]
    errors: Annotated[list[Error], Field(description="처음 100건", max_length=100)]
    requested_by: Annotated[UUID, Field(alias="requestedBy")]
    created_at: Annotated[AwareDatetime, Field(alias="createdAt")]
    finished_at: Annotated[AwareDatetime | None, Field(alias="finishedAt")]


class CreateFeatureExportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    golden_set_id: Annotated[UUID | None, Field(alias="goldenSetId")] = None
    from_: Annotated[date_aliased | None, Field(alias="from")] = None
    to: date_aliased | None = None


class FeatureExport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    export_id: Annotated[UUID, Field(alias="exportId")]
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    golden_set_id: Annotated[UUID | None, Field(alias="goldenSetId")]
    from_: Annotated[date_aliased | None, Field(alias="from")]
    to: date_aliased | None
    file_key: Annotated[
        str | None,
        Field(
            alias="fileKey",
            description="한 줄이 `quality/v1/feature-row.schema.json`인 JSON Lines",
            pattern="^exports/[0-9a-f-]{36}/features[.]jsonl$",
        ),
    ]
    row_count: Annotated[int, Field(alias="rowCount", ge=0)]
    source_count: Annotated[int, Field(alias="sourceCount", ge=0)]
    created_at: Annotated[AwareDatetime, Field(alias="createdAt")]
    finished_at: Annotated[AwareDatetime | None, Field(alias="finishedAt")]


type StageId = Annotated[
    Literal[
        "normalize.queue_wait",
        "normalize.handle",
        "normalize.probe",
        "normalize.transcode",
        "normalize.hls",
        "normalize.store",
        "ai-gateway.queue_wait",
        "ai-gateway.handle",
        "chunk-processor.queue_wait",
        "chunk-processor.handle",
        "frame-encoder.queue_wait",
        "frame-encoder.handle",
        "encoder.segments",
        "encoder.crops",
        "encoder.store",
        "encoder.prototype",
        "event-analyzer.queue_wait",
        "event-analyzer.handle",
        "analyzer.judge",
        "analyzer.escalate",
        "track-stitcher.queue_wait",
        "track-stitcher.handle",
        "stitcher.boundary",
        "stitcher.reid",
        "stitcher.finalize",
        "analyzer.merge",
        "track-analyzer.queue_wait",
        "track-analyzer.handle",
        "attribute.call",
        "embedding-worker.queue_wait",
        "embedding-worker.handle",
        "embedding.call",
        "embedding.store",
        "grounding-worker.queue_wait",
        "grounding-worker.handle",
        "grounding.sample",
        "grounding.detect",
        "grounding.render",
        "grounding.store",
        "aggregator.queue_wait",
        "aggregator.handle",
        "aggregate.read",
        "aggregate.compute",
        "aggregate.summary",
        "aggregate.store",
        "aggregate.inventory_store",
        "aggregate.inventory_compare",
        "aggregate.judge",
        "aggregate.forecast",
        "aggregate.fold",
        "analyzer.alert",
        "ingest.window",
        "ingest.alert",
        "stream.window-complete",
        "stream.state-apply",
    ],
    Field(description="처리 단계 ID"),
]


class Stage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    stage_id: Annotated[str, Field(alias="stageId")]
    baseline_p95_ms: Annotated[float | None, Field(alias="baselineP95Ms")]
    candidate_p95_ms: Annotated[float | None, Field(alias="candidateP95Ms")]
    ratio: Annotated[float | None, Field(description="후보 p95 / 기준 p95")]
    baseline_count: Annotated[int, Field(alias="baselineCount", ge=0)]
    candidate_count: Annotated[int, Field(alias="candidateCount", ge=0)]
    status: Annotated[
        Literal["passed", "failed", "skipped"],
        Field(description="skipped: 어느 한쪽 기록 수가 minSamples보다 적음"),
    ]


class StageGateResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    passed: Annotated[bool, Field(description="failed인 단계가 없으면 true")]
    max_regression_ratio: Annotated[float, Field(alias="maxRegressionRatio")]
    stages: list[Stage]


class PageMeta(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    page: Annotated[int, Field(ge=0)]
    size: Annotated[int, Field(ge=1)]
    total_elements: Annotated[int, Field(alias="totalElements")]
    total_pages: Annotated[int, Field(alias="totalPages")]


type Role = Literal["USER", "OPERATOR", "ADMIN"]


class Error1(BaseModel):
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
        list[Error1] | None,
        Field(description="요청 본문 검증 오류일 때 필드별 오류 목록. 그 밖의 오류에는 없다."),
    ] = None


class ApiV1QualityGoldenSetsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[GoldenSet]
    page: PageMeta


class ApiV1QualityGoldenSetsSetIdVersionsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[GoldenSetVersion]
    page: PageMeta


class ApiV1QualityEvaluationRunsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[EvaluationRun]
    page: PageMeta


class ApiV1QualityDatasetImportsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[DatasetImport]
    page: PageMeta


class ReviewLabel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    verdict: Literal["correct", "incorrect", "uncertain"]
    event_present: Annotated[
        bool, Field(alias="eventPresent", description="그 구간에 이벤트가 실제로 있었는지")
    ]
    event_type: Annotated[
        EventType | None, Field(alias="eventType", description="eventPresent=true면 필수")
    ]
    start_ms: Annotated[int | None, Field(alias="startMs", ge=0)]
    end_ms: Annotated[int | None, Field(alias="endMs", ge=0)]
    severity: Literal["low", "medium", "high"] | None
    note: Annotated[str | None, Field(max_length=500)]


class ReviewItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    item_id: Annotated[UUID, Field(alias="itemId")]
    event_id: Annotated[UUID, Field(alias="eventId")]
    source_id: Annotated[UUID, Field(alias="sourceId")]
    job_id: Annotated[UUID, Field(alias="jobId")]
    event_type: Annotated[EventType, Field(alias="eventType")]
    start_ms: Annotated[int, Field(alias="startMs", ge=0, le=9223372036854775807)]
    end_ms: Annotated[int, Field(alias="endMs", ge=0, le=9223372036854775807)]
    judgment_method: Annotated[str, Field(alias="judgmentMethod", max_length=50)]
    reason: Annotated[
        str,
        Field(
            description="열린 목록(ADR-0014). review_required: event.analyzed의 reviewRequired, sample: 표본 추출, budget_exceeded·call_cap_reached·model_unavailable: event.analyzed.unverifiedReason(v5.1, SPEC-029 R-27), alert_dismissed: alert.raised dismissed(SPEC-029 R-28)"
        ),
    ]
    review_reasons: Annotated[list[ReviewReason], Field(alias="reviewReasons")]
    evidence_frame_keys: Annotated[
        list[EvidenceFrameKey],
        Field(
            alias="evidenceFrameKeys",
            description="블러 사본 저장소 경로. 화면은 evidenceFrameUrls를 쓴다",
        ),
    ]
    evidence_frame_urls: Annotated[
        list[AnyUrl],
        Field(
            alias="evidenceFrameUrls",
            description="evidenceFrameKeys와 같은 순서의 서명 주소(1시간). 화면 표시용이고 로그·저장소에 남기지 않는다 (SPEC-035 R-4)",
        ),
    ]
    analysis: Annotated[
        dict[str, object] | None,
        Field(
            description="판정 결과 VLMAnalysis. 형식은 `events/_vlm-analysis.schema.json`이다. 판정 결과가 없는 항목(사유 `alert_dismissed`의 미판정 알림 등)은 null이다"
        ),
    ]
    status: Literal["UNASSIGNED", "ASSIGNED", "LABELED"]
    assignee_id: Annotated[UUID | None, Field(alias="assigneeId")]
    assigned_at: Annotated[AwareDatetime | None, Field(alias="assignedAt")]
    label: ReviewLabel | None
    labeled_at: Annotated[AwareDatetime | None, Field(alias="labeledAt")]
    created_at: Annotated[AwareDatetime, Field(alias="createdAt")]


class GoldenSetEntry(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    entry_id: Annotated[UUID, Field(alias="entryId")]
    set_id: Annotated[UUID, Field(alias="setId")]
    review_item_id: Annotated[UUID, Field(alias="reviewItemId")]
    source_id: Annotated[UUID, Field(alias="sourceId")]
    event_id: Annotated[UUID, Field(alias="eventId")]
    label: ReviewLabel
    added_at: Annotated[AwareDatetime, Field(alias="addedAt")]


class StageGateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    baseline_source_ids: Annotated[
        list[UUID],
        Field(
            alias="baselineSourceIds",
            description="기준 벤치마크 실행으로 처리한 영상",
            max_length=200,
            min_length=1,
        ),
    ]
    candidate_source_ids: Annotated[
        list[UUID], Field(alias="candidateSourceIds", max_length=200, min_length=1)
    ]
    stage_ids: Annotated[
        list[StageId] | None,
        Field(alias="stageIds", description="없으면 양쪽에 기록이 있는 모든 단계", max_length=200),
    ] = None
    max_regression_ratio: Annotated[
        float | None,
        Field(
            alias="maxRegressionRatio",
            description="후보 p95 / 기준 p95 − 1이 이 값을 넘으면 실패",
            gt=0.0,
            le=10.0,
        ),
    ] = 0.2
    min_samples: Annotated[int | None, Field(alias="minSamples", ge=1, le=1000)] = 5


class ApiV1QualityReviewItemsGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[ReviewItem]
    page: PageMeta


class ApiV1QualityGoldenSetsSetIdEntriesGetResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    items: list[GoldenSetEntry]
    page: PageMeta
