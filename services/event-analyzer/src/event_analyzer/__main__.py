"""event-analyzer 워커 진입점. `python -m event_analyzer`."""

from collections.abc import Mapping

import httpx
from vlm_ai_client.client import AiGatewayClient, CallRetry
from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_contracts.events import EventCandidateVector
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route

from event_analyzer.service import AnalyzerConfig, EventAnalyzerService
from event_analyzer.settings import EventAnalyzerSettings, WorkerSettings

SERVICE_NAME = "event-analyzer"


def build_client(
    settings: EventAnalyzerSettings, transport: httpx.AsyncBaseTransport | None = None
) -> AiGatewayClient:
    """ai-gateway 클라이언트. 테스트는 전송 계층을 바꿔 넣는다.

    클라이언트는 워커의 이벤트 루프에서만 쓴다. 연결은 처음 호출할 때 그 루프에서 열린다.
    """
    return AiGatewayClient(
        settings.ai_gateway_url,
        # ai-gateway 프로필의 허용 호출자 이름이다.
        caller=SERVICE_NAME,
        timeout_s=settings.call_timeout_s,
        retry=CallRetry(max_retries=settings.max_retries, base_delay_s=settings.retry_base_delay_s),
        transport=transport,
    )


def build_worker(
    worker: WorkerSettings,
    settings: EventAnalyzerSettings,
    client: AiGatewayClient,
    health: HealthState,
    kafka: Mapping[str, str] | None = None,
) -> KafkaWorker:
    """설정과 클라이언트로 워커를 조립한다. 테스트는 클라이언트·health·Kafka 설정을 바꿔 넣는다."""
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    service = EventAnalyzerService(
        client, publisher, AnalyzerConfig(concurrency=settings.concurrency)
    )
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [route("event.candidate.vector", EventCandidateVector, service.handle)],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = EventAnalyzerSettings()
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    build_worker(worker_settings, settings, build_client(settings), health).run()


if __name__ == "__main__":
    main()
