"""event-analyzer 워커 진입점. `python -m event_analyzer`."""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from vlm_ai_client.client import AiGatewayClient
from vlm_common.health import HealthState, serve_health
from vlm_common.http_call import CallRetry
from vlm_common.logging import configure_logging
from vlm_common.profile import load_profile
from vlm_contracts.events import AlertCandidate, EventCandidate, EventCandidateVector
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from event_analyzer.alerts import AlertService
from event_analyzer.detector import DetectorAnalyzerService
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rejudge_state import NoRejudgeState, RedisRejudgeState, RejudgeState
from event_analyzer.service import AnalyzerConfig, EventAnalyzerService
from event_analyzer.settings import EventAnalyzerSettings, WorkerSettings

SERVICE_NAME = "event-analyzer"


@dataclass(frozen=True, slots=True)
class Dependencies:
    """탐지기·긴급 경로가 쓰는 프로필과 외부 구성 요소. 테스트는 메모리 구현으로 바꾼다."""

    storage: ObjectStorage
    profile: EventAnalyzerProfile
    rejudge: RejudgeState


def load_dependencies(
    settings: EventAnalyzerSettings, storage: ObjectStorage | None = None
) -> Dependencies:
    """프로필을 읽어 검증하고 저장소·재판정 상태를 만든다.

    Raises:
        ProfileError: 프로필이 없거나 틀렸을 때(SPEC-023 R-16). 기동에 실패해야 한다.
    """
    profile = load_profile(settings.profile_path, EventAnalyzerProfile)
    rejudge: RejudgeState = (
        RedisRejudgeState(settings.redis_url) if settings.redis_url else NoRejudgeState()
    )
    return Dependencies(storage or S3Storage(StorageSettings()), profile, rejudge)


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
    deps: Dependencies,
    kafka: Mapping[str, str] | None = None,
) -> KafkaWorker:
    """설정과 클라이언트로 워커를 조립한다. 테스트는 클라이언트·health·의존성·Kafka 설정을 바꿔
    넣는다."""
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    config = AnalyzerConfig(concurrency=settings.concurrency)
    vector = EventAnalyzerService(client, publisher, config)
    detector = DetectorAnalyzerService(
        client, publisher, deps.storage, deps.profile, config, rejudge=deps.rejudge
    )
    alerts = AlertService(client, publisher, deps.profile)
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [
            route("event.candidate.vector", EventCandidateVector, vector.handle),
            route("event.candidate", EventCandidate, detector.handle),
            route("alert.candidate", AlertCandidate, alerts.handle),
        ],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = EventAnalyzerSettings()
    # 프로필이 틀리면 health를 열기 전에 멈춘다(SPEC-023 R-16).
    deps = load_dependencies(settings)
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    build_worker(worker_settings, settings, build_client(settings), health, deps).run()


if __name__ == "__main__":
    main()
