"""aggregator 워커 진입점. `python -m aggregator`."""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from vlm_ai_client.client import AiGatewayClient
from vlm_analysis_client.client import AnalysisApiClient
from vlm_common.health import HealthState, serve_health
from vlm_common.http_call import CallRetry
from vlm_common.logging import configure_logging
from vlm_contracts.events import AnalysisCompleted, ChunkManifest, EventAnalyzed, TrackDetected
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.retry import RetryPolicy
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage
from vlm_stream_client.client import StreamApiClient

from aggregator.kv import KeyValueStore, RedisStore
from aggregator.ports import ServiceConfig
from aggregator.profile import load_aggregate_profile
from aggregator.reader import JobReader
from aggregator.service import AggregatorService
from aggregator.settings import AggregatorSettings, WorkerSettings
from aggregator.stream import StreamAggregator

SERVICE_NAME = "aggregator"


@dataclass(frozen=True, slots=True)
class Transports:
    """테스트가 바꿔 넣는 전송 계층. None이면 실제 HTTP다."""

    gateway: httpx.AsyncBaseTransport | None = None
    analysis: httpx.AsyncBaseTransport | None = None
    stream: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True, slots=True)
class Clients:
    """내부 호출 클라이언트. 워커의 이벤트 루프에서만 쓴다(연결은 첫 호출 때 그 루프에서 열린다)."""

    gateway: AiGatewayClient
    analysis: AnalysisApiClient
    stream: StreamApiClient | None

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.analysis.aclose()
        if self.stream is not None:
            await self.stream.aclose()


def build_clients(settings: AggregatorSettings, transports: Transports | None = None) -> Clients:
    """ai-gateway·analysis-api·stream-service 클라이언트. `X-Caller`는 `aggregator`다."""
    t = transports or Transports()
    retry = CallRetry(max_retries=settings.max_retries, base_delay_s=settings.retry_base_delay_s)
    return Clients(
        gateway=AiGatewayClient(
            settings.ai_gateway_url,
            caller=SERVICE_NAME,
            timeout_s=settings.gateway_timeout_s,
            retry=retry,
            transport=t.gateway,
        ),
        analysis=AnalysisApiClient(
            settings.analysis_api_url,
            caller=SERVICE_NAME,
            timeout_s=settings.read_call_timeout_s,
            retry=retry,
            transport=t.analysis,
        ),
        stream=StreamApiClient(
            settings.stream_service_url,
            caller=SERVICE_NAME,
            timeout_s=settings.read_call_timeout_s,
            retry=retry,
            transport=t.stream,
        )
        if settings.stream_service_url
        else None,
    )


def build_worker(
    worker: WorkerSettings,
    settings: AggregatorSettings,
    storage: ObjectStorage,
    clients: Clients,
    health: HealthState,
    kafka: Mapping[str, str] | None = None,
    store: KeyValueStore | None = None,
) -> KafkaWorker:
    """설정과 의존성으로 워커를 조립한다. 테스트는 저장소·클라이언트·Redis·Kafka 설정을 바꿔 넣는다.

    Raises:
        ProfileError: 프로필을 읽지 못하면 기동에 실패한다.
    """
    profile = load_aggregate_profile(settings.profile_path)
    if store is None and settings.redis_url is not None:
        store = RedisStore(settings.redis_url.get_secret_value())
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    config = ServiceConfig(
        read_timeout_s=settings.read_timeout_s, max_attempts=settings.max_attempts
    )
    stream = (
        StreamAggregator(
            store=store,
            storage=storage,
            publisher=publisher,
            gateway=clients.gateway,
            zones=clients.stream,
            profile=profile,
            config=config,
        )
        if store is not None and clients.stream is not None
        else None
    )
    service = AggregatorService(
        reader=JobReader(clients.analysis, page_limit=settings.page_limit),
        gateway=clients.gateway,
        storage=storage,
        publisher=publisher,
        profile=profile,
        config=config,
        store=store,
        stream=stream,
    )
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            # 개수 대조의 partial 판단(R-4)과 같은 시도 수를 쓴다.
            retry=RetryPolicy(max_attempts=settings.max_attempts),
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [
            route("analysis.completed", AnalysisCompleted, service.on_analysis_completed),
            route("event.analyzed", EventAnalyzed, service.on_event_analyzed),
            route("track.detected", TrackDetected, service.on_track_detected),
            route("chunk.manifest", ChunkManifest, service.on_chunk_manifest),
        ],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = AggregatorSettings()
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    storage = S3Storage(StorageSettings())
    build_worker(worker_settings, settings, storage, build_clients(settings), health).run()


if __name__ == "__main__":
    main()
