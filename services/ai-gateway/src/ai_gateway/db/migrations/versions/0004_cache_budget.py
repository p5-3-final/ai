"""결과 캐시·예산 사용량 테이블과 호출 로그의 캐시·비용 컬럼 (SPEC-016).

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_OLD_STATUSES = "status IN ('success', 'unavailable', 'failed', 'invalid_output')"
_NEW_STATUSES = (
    "status IN ('success', 'unavailable', 'failed', 'invalid_output', 'budget_exceeded')"
)


def upgrade() -> None:
    op.create_table(
        "ai_result_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid()),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="ai",
    )
    op.create_index("ix_ai_result_cache_source_id", "ai_result_cache", ["source_id"], schema="ai")
    op.create_table(
        "ai_budget_usage",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("spent_usd", sa.Numeric(18, 6), nullable=False, server_default="0"),
        schema="ai",
    )
    op.add_column("ai_call_log", sa.Column("cache_hit", sa.Boolean()), schema="ai")
    op.add_column("ai_call_log", sa.Column("reserved_cost_usd", sa.Numeric(18, 6)), schema="ai")
    op.add_column("ai_call_log", sa.Column("settled_cost_usd", sa.Numeric(18, 6)), schema="ai")
    op.drop_constraint("ck_ai_call_log_status", "ai_call_log", schema="ai")
    op.create_check_constraint("ck_ai_call_log_status", "ai_call_log", _NEW_STATUSES, schema="ai")


def downgrade() -> None:
    op.drop_constraint("ck_ai_call_log_status", "ai_call_log", schema="ai")
    op.create_check_constraint("ck_ai_call_log_status", "ai_call_log", _OLD_STATUSES, schema="ai")
    op.drop_column("ai_call_log", "settled_cost_usd", schema="ai")
    op.drop_column("ai_call_log", "reserved_cost_usd", schema="ai")
    op.drop_column("ai_call_log", "cache_hit", schema="ai")
    op.drop_table("ai_budget_usage", schema="ai")
    op.drop_index("ix_ai_result_cache_source_id", "ai_result_cache", schema="ai")
    op.drop_table("ai_result_cache", schema="ai")
