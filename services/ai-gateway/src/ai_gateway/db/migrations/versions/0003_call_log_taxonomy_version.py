"""ai.ai_call_log에 분류 체계 버전을 더함 (SPEC-009 R-29).

임베딩 호출과 분류 체계를 참조하지 않는 taskType은 값이 없어 빈 값을 허용한다.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_call_log", sa.Column("taxonomy_version", sa.Text()), schema="ai")


def downgrade() -> None:
    op.drop_column("ai_call_log", "taxonomy_version", schema="ai")
