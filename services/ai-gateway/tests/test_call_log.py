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
        taxonomy_version="v2",
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
    """[SPEC-009 R-17, R-29] 분류 체계 버전도 함께 남는다."""
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
        "taxonomy_version",
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


async def test_spec011_r23_saves_embed_call_without_source_or_prompt(engine: AsyncEngine) -> None:
    """[SPEC-011 R-23] 임베딩 호출은 영상 ID·프롬프트 없이 입력 수와 함께 남는다."""
    entry = replace(
        _entry(),
        source_id=None,
        task_type="embed_visual_text",
        adapter="open_clip",
        model_id="open_clip:MobileCLIP2-S0:dfndr2b:mean-4",
        prompt_version=None,
        image_keys=[],
        request_config={},
        output={"dimension": 512},
        raw_output=None,
        input_tokens=None,
        output_tokens=None,
        input_count=2,
    )

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
    assert (row["source_id"], row["prompt_version"], row["input_count"]) == (None, None, 2)
    assert row["output"] == {"dimension": 512}
