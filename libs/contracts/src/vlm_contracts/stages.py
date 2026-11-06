"""처리 단계 측정 기록 Pydantic 모델(SPEC-013).

원본은 docs 레포 `service-contracts/stages/v1/`과 `events/stage.timing.schema.json`이고, 계약
테스트가 둘이 같은지 확인한다. JSON 필드 이름은 camelCase다.
"""

from typing import Literal, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# 계약의 단계 ID 목록 v1. 목록에 없는 ID는 mypy가 막는다(R-10).
type StageId = Literal[
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
]
STAGE_IDS: frozenset[str] = frozenset(get_args(StageId.__value__))

type StageStatus = Literal["ok", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class StageSubject(_Model):
    """측정 대상. 영상 내용·소유자 같은 개인정보는 싣지 않는다(NFR-1)."""

    source_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    chunk_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    candidate_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    query_id: str | None = Field(default=None, exclude_if=lambda v: v is None)


class StageVersions(_Model):
    """결과에 영향을 준 모델·설정 버전. 없는 값은 JSON에서 뺀다."""

    taxonomy: str | None = Field(default=None, exclude_if=lambda v: v is None)
    detector_profile: str | None = Field(default=None, exclude_if=lambda v: v is None)
    embedding_model: str | None = Field(default=None, exclude_if=lambda v: v is None)
    model_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    prompt_version: str | None = Field(default=None, exclude_if=lambda v: v is None)
    analysis_version: str | None = Field(default=None, exclude_if=lambda v: v is None)


class StageTiming(_Model):
    """처리 단계 하나의 측정 기록."""

    timing_id: UUID
    stage_id: StageId
    path: str
    subject: StageSubject
    attempt: int = Field(ge=1)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    # queue_wait은 다른 Pod의 시계를 비교해 음수일 수 있다. 그대로 남긴다.
    duration_ms: float
    count: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    unit: str | None = Field(default=None, exclude_if=lambda v: v is None)
    versions: StageVersions | None = Field(default=None, exclude_if=lambda v: v is None)
    status: StageStatus
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    ai_call_ids: list[UUID] = Field(default_factory=list)


class StageTimingBatch(_Model):
    """`stage.timing` 메시지. 소비 핸들러 하나의 기록 묶음."""

    batch_id: UUID
    service: str = Field(min_length=1)
    pod: str = Field(min_length=1)
    produced_at: AwareDatetime
    timings: list[StageTiming] = Field(min_length=1)
