"""[SPEC-014] 실제 worker DB로 실행 점유·종료 Outbox를 검증한다."""

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
from chunk_processor.service import ProcessingService
from chunk_processor.store import ExecutionStore
from chunk_support import FakeProcessor, RecordingPublisher, context, request
from sqlalchemy import text
from vlm_contracts.events import ChunkRequested
from vlm_testing import containers

pytestmark = pytest.mark.integration


@pytest.fixture
async def store() -> AsyncIterator[ExecutionStore]:
    with containers.postgres() as url:
        value = ExecutionStore(url)
        await value.migrate()
        yield value
        await value.engine.dispose()


async def test_spec014_r5_persists_terminal_outbox(store: ExecutionStore) -> None:
    r = request()
    processor = FakeProcessor()
    publisher = RecordingPublisher()
    await ProcessingService(store, processor, publisher).handle(r, context(r))
    async with store.engine.connect() as db:
        count = (await db.execute(text("SELECT count(*) FROM processor_outbox"))).scalar_one()
    assert count == 1
    assert processor.starts == 1


async def test_spec014_r2_r3_concurrent_claim_and_conflict(store: ExecutionStore) -> None:
    """같은 실행을 동시에 받아도 한 번 실행하고 변경 입력은 거부한다."""
    import asyncio

    from vlm_messaging.worker import NonRetryableError

    r = request()
    processor = FakeProcessor()
    publisher = RecordingPublisher()
    service = ProcessingService(store, processor, publisher)
    await asyncio.gather(*(service.handle(r, context(r)) for _ in range(8)))
    assert processor.starts == 1
    assert len(publisher.events) == 1
    changed = r.model_copy(update={"owner_id": uuid4()})
    with pytest.raises(NonRetryableError, match="input conflicts"):
        await service.handle(changed, context(changed))


async def test_spec014_r4_expired_never_starts(store: ExecutionStore) -> None:
    """과거 시작 기한은 실제 처리를 시작하지 않은 실패로 종료한다."""
    from vlm_contracts.events import ChunkExecutionFinished

    now = await store.now()
    r = request().model_copy(
        update={
            "occurred_at": now - timedelta(minutes=1),
            "start_before": now - timedelta(seconds=1),
        }
    )
    processor = FakeProcessor()
    publisher = RecordingPublisher()
    await ProcessingService(store, processor, publisher).handle(r, context(r))
    assert processor.starts == 0
    event = publisher.events[0]
    assert isinstance(event, ChunkExecutionFinished)
    assert event.outcome == "FAILED"
    assert event.error_code == "START_DEADLINE_EXCEEDED"


async def test_spec014_r6_known_failure_terminates(store: ExecutionStore) -> None:
    """종료가 확실한 오류만 실패 이벤트로 발행한다."""
    from chunk_processor.service import ProcessingFailedError
    from vlm_contracts.events import ChunkExecutionFinished

    class FailedProcessor:
        def execute(self, r: ChunkRequested) -> str:
            raise ProcessingFailedError("PROCESSING_FAILED")

    r = request()
    publisher = RecordingPublisher()
    await ProcessingService(store, FailedProcessor(), publisher).handle(r, context(r))
    event = publisher.events[0]
    assert isinstance(event, ChunkExecutionFinished)
    assert event.outcome == "FAILED"
    assert event.result_key is None


async def test_spec014_r7_unknown_timeout_keeps_running(store: ExecutionStore) -> None:
    """timeout 후 실제 작업이 끝났다고 가정하거나 같은 실행을 재점유하지 않는다."""
    from chunk_processor.service import OutcomeUnknownError

    class UnknownProcessor(FakeProcessor):
        def execute(self, r: ChunkRequested) -> str:
            self.starts += 1
            raise OutcomeUnknownError("inference timeout, still possibly running")

    r = request()
    processor = UnknownProcessor()
    publisher = RecordingPublisher()
    service = ProcessingService(store, processor, publisher)
    with pytest.raises(OutcomeUnknownError):
        await service.handle(r, context(r))
    await service.handle(r, context(r))
    assert processor.starts == 1
    assert publisher.events == []
    async with store.engine.connect() as db:
        assert (
            await db.execute(text("SELECT status FROM processor_executions"))
        ).scalar_one() == "RUNNING"
        assert (await db.execute(text("SELECT count(*) FROM processor_outbox"))).scalar_one() == 0


async def test_spec014_r5_finish_rollback_does_not_repeat_execution(store: ExecutionStore) -> None:
    """종료 Outbox 저장 실패는 종료 기록도 롤백하고 불명 실행을 다시 시작하지 않는다."""
    from sqlalchemy.exc import IntegrityError

    r = request()
    processor = FakeProcessor()
    publisher = RecordingPublisher()
    async with store.engine.begin() as db:
        await db.execute(
            text("ALTER TABLE processor_outbox ADD CONSTRAINT injected CHECK(false) NOT VALID")
        )
    service = ProcessingService(store, processor, publisher)
    with pytest.raises(IntegrityError):
        await service.handle(r, context(r))
    async with store.engine.begin() as db:
        assert (
            await db.execute(text("SELECT status FROM processor_executions"))
        ).scalar_one() == "RUNNING"
        assert (await db.execute(text("SELECT count(*) FROM processor_outbox"))).scalar_one() == 0
        await db.execute(text("ALTER TABLE processor_outbox DROP CONSTRAINT injected"))
    await service.handle(r, context(r))
    assert processor.starts == 1
    assert publisher.events == []


async def test_spec014_r8_republish_after_sent_before_db_commit(store: ExecutionStore) -> None:
    """전송 후 확인 저장 실패는 새 발행기가 최초 이벤트를 그대로 재발행한다."""
    from sqlalchemy.exc import IntegrityError

    r = request()
    processor = FakeProcessor()
    publisher = RecordingPublisher()
    async with store.engine.begin() as db:
        await db.execute(
            text(
                "ALTER TABLE processor_outbox ADD CONSTRAINT injected "
                "CHECK(published_at IS NULL) NOT VALID"
            )
        )
    with pytest.raises(IntegrityError):
        await ProcessingService(store, processor, publisher).handle(r, context(r))
    first = publisher.events[0].model_dump_json(by_alias=True)
    async with store.engine.begin() as db:
        await db.execute(text("ALTER TABLE processor_outbox DROP CONSTRAINT injected"))
    restarted = ExecutionStore(store.engine.url.render_as_string(hide_password=False))
    await restarted.publish_pending(publisher)
    await restarted.engine.dispose()
    assert [e.model_dump_json(by_alias=True) for e in publisher.events] == [first, first]
    assert processor.starts == 1
