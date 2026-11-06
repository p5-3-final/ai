"""track-analyzer 워커 진입점. `python -m track_analyzer`."""

from collections.abc import Mapping

import httpx
from vlm_ai_client.client import AiGatewayClient
from vlm_common.health import HealthState, serve_health
from vlm_common.http_call import CallRetry
from vlm_common.logging import configure_logging
from vlm_contracts.events import AttributeRequested
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route

from track_analyzer.service import TrackAnalyzerService
from track_analyzer.settings import TrackAnalyzerSettings, WorkerSettings

SERVICE_NAME = "track-analyzer"


def build_client(
    settings: TrackAnalyzerSettings, transport: httpx.AsyncBaseTransport | None = None
) -> AiGatewayClient:
    """ai-gateway 클라이언트. 테스트는 전송 계층을 바꿔 넣는다.

    클라이언트는 워커의 이벤트 루프에서만 쓴다. 연결은 처음 호출할 때 그 루프에서 열린다.
    제한 시간이 `X-Request-Deadline`이 되어 ai-gateway도 같은 시각에 멈춘다(R-9).
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
    settings: TrackAnalyzerSettings,
    client: AiGatewayClient,
    health: HealthState,
    kafka: Mapping[str, str] | None = None,
) -> KafkaWorker:
    """설정과 클라이언트로 워커를 조립한다. 테스트는 클라이언트·health·Kafka 설정을 바꿔 넣는다.

    계약에 맞지 않거나 다른 영상의 이미지를 가리키는 요청은 모델 검증에서 걸려 DLQ로 간다
    (R-6, R-7).
    """
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    service = TrackAnalyzerService(client, publisher)
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [route("attribute.requested", AttributeRequested, service.handle)],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = TrackAnalyzerSettings()
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    build_worker(worker_settings, settings, build_client(settings), health).run()


if __name__ == "__main__":
    main()
