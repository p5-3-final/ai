import pytest
from ai_gateway.db.migrate import upgrade
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_creates_call_log_table(database_url: str) -> None:
    await upgrade(database_url)
    # 이미 최신이면 아무 것도 하지 않는다(기동할 때마다 부른다).
    await upgrade(database_url)

    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'ai' AND table_name = 'ai_call_log'"
            )
        )
        columns = {r[0] for r in rows}
    await engine.dispose()
    assert {
        "call_id",
        "caller",
        "source_id",
        "task_type",
        "adapter",
        "model_id",
        "prompt_version",
        "status",
        "error_code",
        "latency_ms",
        "image_keys",
        "request_config",
        "output",
        "raw_output",
        "input_tokens",
        "output_tokens",
        "trace_id",
        "created_at",
    } <= columns


async def test_rejects_unknown_status(database_url: str) -> None:
    await upgrade(database_url)
    engine = create_async_engine(database_url)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai.ai_call_log (call_id, caller, source_id, task_type,"
                    " prompt_version, status, latency_ms, image_keys, request_config)"
                    " VALUES (gen_random_uuid(), 'x', gen_random_uuid(), 'event_judge',"
                    " 'v', 'nope', 1, '{}', '{}')"
                )
            )
    await engine.dispose()
