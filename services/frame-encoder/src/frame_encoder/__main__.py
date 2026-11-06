"""frame-encoder 워커 진입점. `python -m frame_encoder`."""

import logging
from dataclasses import dataclass

from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_common.profile import ProfileError, load_profile
from vlm_contracts.events import SegmentReady
from vlm_encoders.base import ImageTextEncoder
from vlm_encoders.loader import load_encoder
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from frame_encoder.profile import FrameEncoderProfile
from frame_encoder.prototypes import Prototypes
from frame_encoder.service import EncoderConfig, FrameEncoderService
from frame_encoder.settings import FrameEncoderSettings, WorkerSettings

SERVICE_NAME = "frame-encoder"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Prepared:
    """기동 단계에서 한 번 만든 인코더와 처리 설정."""

    encoder: ImageTextEncoder
    config: EncoderConfig


def prepare(settings: FrameEncoderSettings, models: ObjectStorage | None) -> Prepared:
    """프로필을 검증하고 인코더를 로드한 뒤 프로토타입을 만든다. 블로킹이다.

    잘못된 프로필이나 가중치로 벡터를 만들면 검색 인덱스가 조용히 틀어지므로 기동에 실패한다
    (SPEC-019 R-9, R-15).

    Raises:
        ProfileError: 프로필이 틀렸거나, 허용하지 않은 mock 인코더이거나, 모델 버킷 설정이 없을 때.
        EncoderLoadError: manifest·가중치가 프로필과 맞지 않을 때.
    """
    profile = load_profile(settings.profile_path, FrameEncoderProfile)
    if profile.encoder.kind == "mock" and not settings.allow_mock:
        raise ProfileError("mock encoder requires FRAME_ENCODER_ALLOW_MOCK=true")
    encoder = load_encoder(profile.encoder, models, settings.model_cache_dir)
    # SPEC-019 R-10: 프로토타입은 메시지마다가 아니라 기동 때 한 번 만든다.
    prototypes = (
        Prototypes.build(profile.prototypes, encoder) if profile.prototypes is not None else None
    )
    return Prepared(
        encoder, EncoderConfig(batch_size=profile.encoding.batch_size, prototypes=prototypes)
    )


def build_worker(
    worker: WorkerSettings,
    settings: FrameEncoderSettings,
    storage: ObjectStorage,
    prepared: Prepared,
    health: HealthState,
) -> KafkaWorker:
    """설정과 저장소로 워커를 조립한다. 테스트는 저장소와 health를 바꿔 넣는다."""
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    service = FrameEncoderService(storage, prepared.encoder, publisher, prepared.config)
    return KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap, group_id=settings.group_id, service=SERVICE_NAME
        ),
        [route("segment.ready", SegmentReady, service.handle)],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if worker.stage_timing_publish else None,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = FrameEncoderSettings()
    health = HealthState()
    # 모델을 받는 동안에도 liveness가 응답하도록 먼저 연다. ready는 워커가 소비를 시작한 뒤다.
    serve_health(health, port=worker_settings.health_port)
    models = (
        S3Storage(StorageSettings(bucket=settings.models_bucket))
        if settings.models_bucket
        else None
    )
    prepared = prepare(settings, models)
    logger.info(
        "encoder loaded",
        extra={
            "embeddingModel": prepared.encoder.model_id,
            "dimension": prepared.encoder.dimension,
        },
    )
    storage = S3Storage(StorageSettings())
    build_worker(worker_settings, settings, storage, prepared, health).run()


if __name__ == "__main__":
    main()
