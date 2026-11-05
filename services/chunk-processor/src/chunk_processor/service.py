"""실행 종료 경계. AI timeout은 종료 실패로 바꾸지 않는다."""

import asyncio
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from vlm_common.errors import VlmError
from vlm_contracts.events import ChunkExecutionFinished, ChunkRequested
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext, NonRetryableError

from chunk_processor.store import ExecutionStore, Publisher


class ProcessingFailedError(VlmError):
    """실제 처리가 이미 종료됐음을 보장하는 실패만 사용한다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutcomeUnknownError(VlmError):
    """AI timeout 등 실제 실행 종료 여부를 모르는 상태. 슬롯은 반환하지 않는다."""


class Processor(Protocol):
    """성공은 resultKey, 확실한 종료 실패는 ProcessingFailedError, 그 외에는 불명 상태다."""

    def execute(self, request: ChunkRequested) -> str: ...


class ProcessingService:
    """Kafka 중복과 실제 실행을 내구 원장으로 구분한다."""

    def __init__(self, store: ExecutionStore, processor: Processor, publisher: Publisher) -> None:
        self.store = store
        self.processor = processor
        self.publisher = publisher

    async def handle(self, request: ChunkRequested, ctx: MessageContext) -> None:
        """최초 점유만 실행한다. 이미 점유된 불명 RUNNING은 임의 재실행하지 않는다."""
        if ctx.key != str(request.source_id).encode():
            raise NonRetryableError("chunk request key must equal sourceId")
        if not await self.store.claim(request):
            await self.store.publish_pending(self.publisher)
            return
        result_key: str | None = None
        error_code: str | None = None
        outcome: Literal["SUCCEEDED", "FAILED"] = "SUCCEEDED"
        if await self.store.now() >= request.start_before:
            error_code = "START_DEADLINE_EXCEEDED"
            outcome = "FAILED"
        else:
            try:
                result_key = await asyncio.to_thread(self.processor.execute, request)
            except ProcessingFailedError as e:
                outcome = "FAILED"
                error_code = e.code
        event = ChunkExecutionFinished(
            event_id=uuid5(NAMESPACE_URL, f"vlm:chunk.execution.finished:{request.execution_id}"),
            occurred_at=await self.store.now(),
            source_id=request.source_id,
            owner_id=request.owner_id,
            job_id=request.job_id,
            chunk_id=request.chunk_id,
            execution_id=request.execution_id,
            request_id=request.request_id,
            normalization_id=request.normalization_id,
            outcome=outcome,
            result_key=result_key,
            error_code=error_code,
        )
        event = prepare_event(event, await self.store.now())
        await self.store.finish(event, ctx.traceparent)
        await self.store.publish_pending(self.publisher)
