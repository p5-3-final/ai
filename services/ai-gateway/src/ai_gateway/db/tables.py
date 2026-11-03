"""ai_db 테이블 정의. 스키마 변경은 migrations/의 Alembic 버전으로만 한다."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
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
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('success', 'unavailable', 'failed', 'invalid_output')",
        name="ck_ai_call_log_status",
    ),
)
