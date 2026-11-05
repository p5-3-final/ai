"""실제 Kafka 소비와 내구 결과 Outbox를 기동한다. 운영 활성화는 별도다."""

import asyncio
import logging
import threading

from sqlalchemy.exc import SQLAlchemyError
from vlm_common.errors import VlmError
from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_contracts.events import ChunkRequested
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings

from chunk_processor.processor import VerificationProcessor
from chunk_processor.service import ProcessingService
from chunk_processor.settings import Settings
from chunk_processor.store import ExecutionStore

logger = logging.getLogger(__name__)


async def drain(
    store: ExecutionStore, publisher: KafkaPublisher, stop: threading.Event, interval: float
) -> None:
    """요청 재전달 없이도 재시작 뒤 미발행 종료를 복구한다."""
    while not stop.is_set():
        try:
            await store.publish_pending(publisher)
        except (VlmError, SQLAlchemyError):
            logger.exception("execution outbox publish failed")
        await asyncio.sleep(interval)


def main() -> None:
    settings = Settings()
    if not settings.enabled:
        raise SystemExit("chunk-processor is disabled")
    configure_logging("chunk-processor", "INFO")
    store = ExecutionStore(settings.database_url.get_secret_value())
    asyncio.run(store.migrate())
    publisher = KafkaPublisher(settings.kafka_bootstrap, "chunk-processor")
    processor = VerificationProcessor(
        S3Storage(StorageSettings()), settings.work_dir, outcome=settings.verification_outcome
    )
    service = ProcessingService(store, processor, publisher)
    health = HealthState()
    serve_health(health, port=settings.health_port)
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: asyncio.run(drain(store, publisher, stop, settings.publish_interval_s)),
        daemon=True,
    )
    thread.start()
    try:
        KafkaWorker(
            WorkerConfig(settings.kafka_bootstrap, settings.group_id, service="chunk-processor"),
            [route("chunk.requested", ChunkRequested, service.handle)],
            publisher,
            health=health,
        ).run()
    finally:
        stop.set()
        thread.join(timeout=15)
        asyncio.run(store.engine.dispose())


if __name__ == "__main__":
    main()
