"""Alembic 실행 환경. 연결은 migrate.upgrade가 넘겨준다."""

from alembic import context
from sqlalchemy import Connection

from ai_gateway.db.tables import SCHEMA, metadata

connection: Connection = context.config.attributes["connection"]
# 버전 표도 서비스 스키마 안에 둬 public 스키마를 건드리지 않는다.
context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table_schema=SCHEMA,
    include_schemas=True,
)
connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
with context.begin_transaction():
    context.run_migrations()
