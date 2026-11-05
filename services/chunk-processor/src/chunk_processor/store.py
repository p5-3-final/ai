"""worker 자체 DB의 실행 점유·종료/Outbox 트랜잭션. Pipeline DB에 접근하지 않는다."""

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from vlm_contracts.events import ChunkExecutionFinished, ChunkRequested, Event
from vlm_messaging.worker import NonRetryableError


class Publisher(Protocol):
    """기존 KafkaPublisher와 테스트 대역이 공유하는 발행 경계."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


class ExecutionStore:
    """RUNNING은 자동 재점유하지 않는다. 불명 실행을 중복 시작하지 않기 위함이다."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)

    async def migrate(self) -> None:
        """worker 소유 versioned SQL을 checksum 확인 후 원자 적용한다."""
        sql = (Path(__file__).parent / "migrations" / "001_execution.sql").read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        async with self.engine.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(14,0)"))
            await db.execute(
                text("""CREATE TABLE IF NOT EXISTS processor_schema_versions (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                version INTEGER NOT NULL UNIQUE, checksum VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            )
            old = (
                await db.execute(
                    text("SELECT checksum FROM processor_schema_versions WHERE version=1")
                )
            ).scalar_one_or_none()
            if old is not None:
                if old != checksum:
                    raise RuntimeError("worker migration checksum changed")
                return
            for statement in sql.split(";"):
                if statement.strip():
                    await db.execute(text(statement))
            await db.execute(
                text("INSERT INTO processor_schema_versions(version,checksum) VALUES(1,:checksum)"),
                {"checksum": checksum},
            )

    async def now(self) -> datetime:
        """시작 기한 판정은 worker DB의 실제 UTC 시각을 사용한다."""
        async with self.engine.connect() as db:
            return cast(datetime, (await db.execute(text("SELECT clock_timestamp()"))).scalar_one())

    async def claim(self, request: ChunkRequested) -> bool:
        """별도 커밋으로 점유한다. 예외·프로세스 사망 뒤에도 RUNNING을 보존한다."""
        params = {
            "execution": request.execution_id,
            "request": request.model_dump_json(by_alias=True),
        }
        async with self.engine.begin() as db:
            inserted = (
                await db.execute(
                    text("""INSERT INTO processor_executions(execution_id,request,status)
                VALUES(:execution,CAST(:request AS jsonb),'RUNNING')
                ON CONFLICT(execution_id) DO NOTHING RETURNING id"""),
                    params,
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            matches = (
                await db.execute(
                    text(
                        "SELECT request=CAST(:request AS jsonb) FROM processor_executions "
                        "WHERE execution_id=:execution"
                    ),
                    params,
                )
            ).scalar_one()
            if not matches:
                raise NonRetryableError("execution request input conflicts")
            return False

    async def finish(self, event: ChunkExecutionFinished, traceparent: str | None) -> None:
        """결과 객체 저장이 끝난 뒤 종료 기록과 Outbox를 함께 커밋한다."""
        params = {
            "execution": event.execution_id,
            "status": event.outcome,
            "payload": event.model_dump_json(by_alias=True),
            "event": event.event_id,
            "trace": traceparent,
        }
        async with self.engine.begin() as db:
            changed = (
                await db.execute(
                    text("""UPDATE processor_executions
                SET status=:status,result=CAST(:payload AS jsonb),updated_at=now()
                WHERE execution_id=:execution AND status='RUNNING' RETURNING id"""),
                    params,
                )
            ).scalar_one_or_none()
            if changed is None:
                raise RuntimeError("execution is no longer running")
            await db.execute(
                text("""INSERT INTO processor_outbox(event_id,execution_id,payload,trace_parent)
                VALUES(:event,:execution,CAST(:payload AS jsonb),:trace)"""),
                params,
            )

    async def publish_pending(self, publisher: Publisher) -> None:
        """전송 후 확인 저장 전 장애는 같은 payload의 재발행으로 복구한다."""
        async with self.engine.begin() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT id,payload::text,trace_parent FROM processor_outbox "
                        "WHERE published_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED"
                    )
                )
            ).all()
            for row in rows:
                event = ChunkExecutionFinished.model_validate_json(cast(str, row[1]))
                await asyncio.to_thread(
                    publisher.publish, event.event_type, event, traceparent=cast(str | None, row[2])
                )
                await db.execute(
                    text(
                        "UPDATE processor_outbox SET published_at=now(),updated_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": row[0]},
                )
