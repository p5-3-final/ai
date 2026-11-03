import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
from ai_gateway.call_log import CallLog, CallLogEntry, new_call_id
from ai_gateway.db.migrate import upgrade
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    created = create_async_engine(database_url)
    yield created
    await created.dispose()


def _entry() -> CallLogEntry:
    return CallLogEntry(
        call_id=new_call_id(),
        caller="event-analyzer",
        source_id=uuid4(),
        task_type="event_judge",
        adapter="ollama",
        model_id="qwen3-vl:8b",
        prompt_version="event_judge@0123456789ab",
        status="success",
        error_code=None,
        latency_ms=1234,
        image_keys=["sources/x/blurred/0.jpg"],
        request_config={"maxOutputTokens": 800, "timeoutS": 30.0, "imageCount": 1},
        output={"verificationStatus": "confirmed"},
        raw_output='{"verificationStatus": "confirmed"}',
        input_tokens=1500,
        output_tokens=120,
        trace_id="0123456789abcdef0123456789abcdef",
    )


async def test_spec009_r17_saves_every_field(engine: AsyncEngine) -> None:
    entry = _entry()

    await CallLog(engine).save(entry)

    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM ai.ai_call_log WHERE call_id = :id"), {"id": entry.call_id}
                )
            )
            .mappings()
            .one()
        )
    for field in (
        "caller",
        "source_id",
        "task_type",
        "adapter",
        "model_id",
        "prompt_version",
        "status",
        "latency_ms",
        "image_keys",
        "request_config",
        "output",
        "raw_output",
        "input_tokens",
        "output_tokens",
        "trace_id",
    ):
        assert row[field] == getattr(entry, field), field
    assert row["created_at"] is not None


async def test_spec009_r17_saves_unconfigured_call_without_model(engine: AsyncEngine) -> None:
    entry = replace(
        _entry(),
        adapter=None,
        model_id=None,
        status="unavailable",
        output={"verificationStatus": "unverified"},
        raw_output=None,
        input_tokens=None,
        output_tokens=None,
    )

    await CallLog(engine).save(entry)

    async with engine.connect() as conn:
        status = await conn.scalar(
            text("SELECT status FROM ai.ai_call_log WHERE call_id = :id"), {"id": entry.call_id}
        )
    assert status == "unavailable"


async def test_spec009_nfr6_call_ids_are_unique_across_concurrent_calls(
    engine: AsyncEngine,
) -> None:
    entries = [_entry() for _ in range(50)]

    await asyncio.gather(*(CallLog(engine).save(e) for e in entries))

    assert len({e.call_id for e in entries}) == 50
