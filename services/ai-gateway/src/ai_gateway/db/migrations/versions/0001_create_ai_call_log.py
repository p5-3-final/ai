"""ai.ai_call_log 생성 (SPEC-009 R-17).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_call_log",
        sa.Column("call_id", sa.Uuid(), primary_key=True),
        sa.Column("caller", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text()),
        sa.Column("model_id", sa.Text()),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("image_keys", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("request_config", postgresql.JSONB(), nullable=False),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("raw_output", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("trace_id", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'unavailable', 'failed', 'invalid_output')",
            name="ck_ai_call_log_status",
        ),
        schema="ai",
    )
    # 영상 삭제 때 그 영상의 원문을 지운다(R-20).
    op.create_index("ix_ai_call_log_source_id", "ai_call_log", ["source_id"], schema="ai")


def downgrade() -> None:
    op.drop_table("ai_call_log", schema="ai")
