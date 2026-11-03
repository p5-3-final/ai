"""ai.ai_call_log에 임베딩 호출을 남길 수 있게 넓힘 (SPEC-011 R-23).

질의 문장·검색용 이미지에는 영상 ID가 없고 임베딩에는 프롬프트가 없다. 입력 수를 더한다.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("ai_call_log", "source_id", nullable=True, schema="ai")
    op.alter_column("ai_call_log", "prompt_version", nullable=True, schema="ai")
    op.add_column("ai_call_log", sa.Column("input_count", sa.Integer()), schema="ai")


def downgrade() -> None:
    # NOT NULL로 되돌리려면 값이 빈 행(임베딩 호출)이 없어야 한다.
    op.execute("DELETE FROM ai.ai_call_log WHERE source_id IS NULL OR prompt_version IS NULL")
    op.drop_column("ai_call_log", "input_count", schema="ai")
    op.alter_column("ai_call_log", "prompt_version", nullable=False, schema="ai")
    op.alter_column("ai_call_log", "source_id", nullable=False, schema="ai")
