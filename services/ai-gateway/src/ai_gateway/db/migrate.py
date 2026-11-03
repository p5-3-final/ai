"""Alembic 마이그레이션 실행. 기동 전에 `python -m ai_gateway.db.migrate`로 부른다."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

_MIGRATIONS = Path(__file__).parent / "migrations"


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    # env.py가 새 엔진을 만들지 않고 이 연결을 그대로 쓴다.
    config.attributes["connection"] = connection
    return config


async def upgrade(database_url: str) -> None:
    """최신 버전까지 올린다. 이미 최신이면 아무 것도 하지 않는다.

    Args:
        database_url: SQLAlchemy asyncpg URL(`postgresql+asyncpg://...`).
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: command.upgrade(_config(sync), "head"))
    finally:
        await engine.dispose()


def main() -> None:
    from ai_gateway.settings import GatewaySettings

    asyncio.run(upgrade(GatewaySettings().database_url.get_secret_value()))


if __name__ == "__main__":
    main()
