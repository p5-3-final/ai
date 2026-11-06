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
        "taxonomy_version",
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


async def test_spec011_r23_accepts_embed_call_without_source_and_prompt(database_url: str) -> None:
    """[SPEC-011 R-23] 질의 임베딩에는 영상 ID·프롬프트가 없고 입력 수를 남긴다."""
    await upgrade(database_url)
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        call_id = await conn.scalar(
            text(
                "INSERT INTO ai.ai_call_log (call_id, caller, task_type, status, latency_ms,"
                " image_keys, request_config, input_count)"
                " VALUES (gen_random_uuid(), 'search-service', 'embed_visual_text', 'success',"
                " 3, '{}', '{}', 2) RETURNING call_id"
            )
        )
        count = await conn.scalar(
            text("SELECT input_count FROM ai.ai_call_log WHERE call_id = :id"), {"id": call_id}
        )
    await engine.dispose()
    assert count == 2


async def _columns(database_url: str, table: str) -> set[str]:
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'ai' AND table_name = :t"
            ),
            {"t": table},
        )
        columns = {r[0] for r in rows}
    await engine.dispose()
    return columns


async def test_spec016_creates_cache_and_budget_tables(database_url: str) -> None:
    """[SPEC-016 R-1, R-10, R-19] 결과 캐시·예산 테이블과 호출 로그의 캐시·비용 컬럼."""
    await upgrade(database_url)

    assert {"cache_key", "task_type", "source_id", "value", "created_at"} <= await _columns(
        database_url, "ai_result_cache"
    )
    assert {"day", "scope", "spent_usd"} <= await _columns(database_url, "ai_budget_usage")
    assert {"cache_hit", "reserved_cost_usd", "settled_cost_usd"} <= await _columns(
        database_url, "ai_call_log"
    )


async def test_spec016_accepts_budget_exceeded_status(database_url: str) -> None:
    """[SPEC-016 R-11] 호출 로그 상태에 budget_exceeded를 받는다."""
    await upgrade(database_url)
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ai.ai_call_log (call_id, caller, task_type, status, latency_ms,"
                " image_keys, request_config) VALUES (gen_random_uuid(), 'x', 'event_judge',"
                " 'budget_exceeded', 1, '{}', '{}')"
            )
        )
    await engine.dispose()
