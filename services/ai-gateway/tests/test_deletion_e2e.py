"""실제 Kafka·PostgreSQL로 video.deleted 소비부터 원문 삭제까지 검증한다."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from ai_gateway.call_log import CallLog, CallLogEntry, new_call_id
from ai_gateway.db.migrate import upgrade
from ai_gateway.deletion import start_deletion_worker
from ai_gateway.profile import CacheProfile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from vlm_common.health import HealthState
from vlm_contracts.events import VideoDeleted
from vlm_messaging.publisher import KafkaPublisher
from vlm_testing import containers

pytestmark = pytest.mark.integration

GROUP = "ai-gateway"


async def test_spec009_r20_consumes_video_deleted(database_url: str) -> None:
    await upgrade(database_url)
    source = uuid4()
    engine = create_async_engine(database_url)
    await CallLog(engine).save(
        CallLogEntry(
            call_id=new_call_id(),
            caller="event-analyzer",
            source_id=source,
            task_type="event_judge",
            adapter="fake",
            model_id="m",
            prompt_version="event_judge@0123456789ab",
            taxonomy_version="v2",
            status="success",
            error_code=None,
            latency_ms=1,
            image_keys=[],
            request_config={},
            output=None,
            raw_output="원문",
            input_tokens=None,
            output_tokens=None,
            trace_id=None,
        )
    )

    with containers.kafka() as bootstrap:
        containers.create_topics(
            bootstrap, ["video.deleted", f"video.deleted.{GROUP}.retry", f"{GROUP}.dlq"]
        )
        health = HealthState()
        running = start_deletion_worker(
            bootstrap, GROUP, database_url, health, cache_settings=CacheProfile()
        )
        try:
            KafkaPublisher(bootstrap=bootstrap, client_id="test").publish(
                "video.deleted",
                VideoDeleted(
                    event_id=uuid4(),
                    occurred_at=datetime.now(UTC),
                    source_id=source,
                    owner_id=uuid4(),
                ),
            )
            raw: str | None = "원문"
            for _ in range(60):
                async with engine.connect() as conn:
                    raw = await conn.scalar(
                        text("SELECT raw_output FROM ai.ai_call_log WHERE source_id = :s"),
                        {"s": source},
                    )
                if raw is None:
                    break
                await asyncio.sleep(0.5)
            assert raw is None
            assert health.is_ready()
        finally:
            running.stop()
    await engine.dispose()
