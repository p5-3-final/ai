"""grounding-worker 워커 진입점. `python -m grounding_worker`."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_common.profile import load_profile
from vlm_contracts.events import GroundingRequested
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from grounding_worker.detector import GroundingDetector, load_detector
from grounding_worker.media import FfmpegTool, MediaTool
from grounding_worker.profile import GroundingProfile
from grounding_worker.service import GroundingConfig, GroundingService
from grounding_worker.settings import GroundingWorkerSettings, WorkerSettings

SERVICE_NAME = "grounding-worker"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Prepared:
    """기동 단계에서 한 번 만든 탐지기와 처리 설정."""

    detector: GroundingDetector
    config: GroundingConfig


def prepare(settings: GroundingWorkerSettings) -> Prepared:
    """프로필을 검증하고 탐지기를 로드한다. 메시지마다가 아니라 기동 때 한 번 부른다.

    Raises:
        ProfileError: 프로필이 틀렸거나 허용하지 않은 mock 탐지기일 때.
    """
    profile = load_profile(settings.profile_path, GroundingProfile)
    detector = load_detector(profile.detector, allow_mock=settings.allow_mock)
    return Prepared(
        detector,
        GroundingConfig(
            score_threshold=profile.detector.score_threshold,
            render=profile.render,
            sample_interval_ms=settings.sample_interval_ms,
            max_samples=settings.max_samples,
            item_timeout_s=settings.item_timeout_s,
            work_dir=settings.work_dir,
        ),
    )


def build_worker(
    worker: WorkerSettings,
    settings: GroundingWorkerSettings,
    storage: ObjectStorage,
    prepared: Prepared,
    health: HealthState,
    kafka: Mapping[str, str] | None = None,
    media: MediaTool | None = None,
) -> KafkaWorker:
    """설정과 저장소로 워커를 조립한다. 테스트는 저장소·health·Kafka 설정·미디어 도구를 바꿔 넣는다.

    계약에 맞지 않거나 다른 영상의 정규화 사본을 가리키는 요청은 모델 검증에서 걸려 DLQ로 간다
    (R-15). 처리 중에는 워커가 파티션을 멈춰 두고 poll을 계속한다(R-16, `libs/messaging`).
    """
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    service = GroundingService(
        storage, media or FfmpegTool(), prepared.detector, publisher, prepared.config
    )
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [route("grounding.requested", GroundingRequested, service.handle)],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = GroundingWorkerSettings()
    health = HealthState()
    # 탐지기를 받는 동안에도 liveness가 응답하도록 먼저 연다. ready는 워커가 소비를 시작한 뒤다.
    serve_health(health, port=worker_settings.health_port)
    prepared = prepare(settings)
    logger.info("detector loaded", extra={"groundingModel": prepared.detector.version})
    storage = S3Storage(StorageSettings())
    build_worker(worker_settings, settings, storage, prepared, health).run()


if __name__ == "__main__":
    main()
