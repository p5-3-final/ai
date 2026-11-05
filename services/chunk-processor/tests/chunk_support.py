"""청크 워커 테스트 입력과 실행 대역."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from vlm_contracts.events import ChunkRequested, Event
from vlm_messaging.worker import MessageContext


def request() -> ChunkRequested:
    source, normalization, execution = uuid4(), uuid4(), uuid4()
    return ChunkRequested.model_validate(
        {
            "eventId": str(uuid4()),
            "sourceId": str(source),
            "ownerId": str(uuid4()),
            "occurredAt": datetime.now(UTC),
            "jobId": str(uuid4()),
            "chunkId": str(uuid4()),
            "executionId": str(execution),
            "requestId": str(execution),
            "normalizationId": str(normalization),
            "normalizedKey": f"sources/{source}/normalizations/{normalization}/normalized.mp4",
            "ownedRange": {"startMs": 0, "endMs": 1000},
            "processingRange": {"startMs": 0, "endMs": 1100},
            "processingProfile": {
                "version": "test-v1",
                "mode": "local",
                "inferenceProfileVersion": "test-v1",
            },
            "startBefore": datetime.now(UTC) + timedelta(minutes=5),
        }
    )


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.events.append(event)


class FakeProcessor:
    def __init__(self) -> None:
        self.starts = 0

    def execute(self, r: ChunkRequested) -> str:
        self.starts += 1
        return (
            f"sources/{r.source_id}/jobs/{r.job_id}/chunks/{r.chunk_id}"
            f"/executions/{r.execution_id}/result.json"
        )


def context(r: ChunkRequested) -> MessageContext:
    return MessageContext(
        topic="chunk.requested",
        partition=0,
        offset=0,
        key=str(r.source_id).encode(),
        attempt=1,
        traceparent=None,
    )
