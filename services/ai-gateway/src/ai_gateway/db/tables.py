"""ai_db 테이블 정의. 스키마 변경은 migrations/의 Alembic 버전으로만 한다."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

SCHEMA = "ai"

metadata = MetaData(schema=SCHEMA)

# docs overview "AI Gateway"의 호출 로그.
# 모델 원문(raw_output)은 영상이 지워지면 비운다(SPEC-009 R-20).
# 임베딩 호출은 영상 ID·프롬프트 버전이 없고 입력 수를 남긴다(SPEC-011 R-23).
# 분석 결과의 enum이 어느 분류 체계 버전인지 남긴다(SPEC-009 R-29).
call_log = Table(
    "ai_call_log",
    metadata,
    Column("call_id", Uuid, primary_key=True),
    Column("caller", Text, nullable=False),
    Column("source_id", Uuid, index=True),
    Column("task_type", Text, nullable=False),
    Column("adapter", Text),
    Column("model_id", Text),
    Column("prompt_version", Text),
    Column("taxonomy_version", Text),
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("latency_ms", Integer, nullable=False),
    Column("image_keys", ARRAY(Text), nullable=False),
    Column("request_config", JSONB, nullable=False),
    Column("output", JSONB),
    Column("raw_output", Text),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("trace_id", Text),
    Column("input_count", Integer),
    # 캐시 적중 여부와 예산 예약·정산 비용(SPEC-016 R-19). 캐시·예산 대상이 아니면 비어 있다.
    Column("cache_hit", Boolean),
    Column("reserved_cost_usd", Numeric(18, 6)),
    Column("settled_cost_usd", Numeric(18, 6)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('success', 'unavailable', 'failed', 'invalid_output', 'budget_exceeded')",
        name="ck_ai_call_log_status",
    ),
)

# 성공한 외부 모델 호출 결과(SPEC-016 R-1). source_id는 영상 삭제 때 지우는 기준이다(R-9).
result_cache = Table(
    "ai_result_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("task_type", Text, nullable=False),
    Column("source_id", Uuid, index=True),
    Column("value", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# 하루(UTC)·범위별 예산 사용량(SPEC-016 R-10). scope는 호출 서비스 이름이거나 전체 "*"다.
budget_usage = Table(
    "ai_budget_usage",
    metadata,
    Column("day", Date, primary_key=True),
    Column("scope", Text, primary_key=True),
    Column("spent_usd", Numeric(18, 6), nullable=False, server_default="0"),
)
