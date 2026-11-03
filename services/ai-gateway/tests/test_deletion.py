from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from ai_gateway.call_log import CallLog, CallLogEntry, new_call_id
from ai_gateway.db.migrate import upgrade
from ai_gateway.deletion import DeletionHandler
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from vlm_contracts.events import VideoDeleted
from vlm_messaging.worker import MessageContext

pytestmark = pytest.mark.integration

CTX = MessageContext(
    topic="video.deleted", partition=0, offset=0, key=None, attempt=1, traceparent=None
)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    created = create_async_engine(database_url)
    yield created
    await created.dispose()


def _entry(source_id: UUID) -> CallLogEntry:
    return CallLogEntry(
        call_id=new_call_id(),
        caller="event-analyzer",
        source_id=source_id,
        task_type="event_judge",
        adapter="fake",
        model_id="m",
        prompt_version="event_judge@0123456789ab",
        status="success",
        error_code=None,
        latency_ms=1,
        image_keys=[],
        request_config={},
        output={"verificationStatus": "confirmed"},
        raw_output="작업자가 넘어져 있다",
        input_tokens=None,
        output_tokens=None,
        trace_id=None,
    )


def _deleted(source_id: UUID) -> VideoDeleted:
    return VideoDeleted(
        event_id=uuid4(),
        occurred_at=datetime(2026, 11, 3, tzinfo=UTC),
        source_id=source_id,
        owner_id=uuid4(),
    )


async def _raw_outputs(engine: AsyncEngine, source_id: UUID) -> list[str | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT raw_output FROM ai.ai_call_log WHERE source_id = :s"), {"s": source_id}
        )
        return [r[0] for r in rows]


async def test_spec009_r20_deleted_video_raw_output_is_cleared(engine: AsyncEngine) -> None:
    deleted, kept = uuid4(), uuid4()
    log = CallLog(engine)
    for entry in (_entry(deleted), _entry(deleted), _entry(kept)):
        await log.save(entry)

    await DeletionHandler(log).handle(_deleted(deleted), CTX)

    assert await _raw_outputs(engine, deleted) == [None, None]
    assert await _raw_outputs(engine, kept) == ["작업자가 넘어져 있다"]


async def test_spec009_r21_repeated_event_ends_the_same(engine: AsyncEngine) -> None:
    source = uuid4()
    log = CallLog(engine)
    await log.save(_entry(source))
    handler = DeletionHandler(log)

    await handler.handle(_deleted(source), CTX)
    await handler.handle(_deleted(source), CTX)
    # 호출 기록이 없는 영상도 오류 없이 끝난다(video.deleted가 분석보다 먼저 올 수 있다).
    await handler.handle(_deleted(uuid4()), CTX)

    assert await _raw_outputs(engine, source) == [None]


async def test_spec009_r20_call_log_metadata_is_kept(engine: AsyncEngine) -> None:
    source = uuid4()
    log = CallLog(engine)
    entry = replace(_entry(source), output={"verificationStatus": "rejected"})
    await log.save(entry)

    await DeletionHandler(log).handle(_deleted(source), CTX)

    async with engine.connect() as conn:
        status = await conn.scalar(
            text("SELECT status FROM ai.ai_call_log WHERE call_id = :c"), {"c": entry.call_id}
        )
    # 사용량·장애 집계용 기록은 남기고 영상 내용이 담긴 원문만 지운다.
    assert status == "success"
