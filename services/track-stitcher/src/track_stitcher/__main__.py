"""track-stitcher 워커 진입점. `python -m track_stitcher`."""

import logging
import threading
from collections.abc import Mapping

from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_common.profile import load_profile
from vlm_contracts.events import ChunkBoundary, SegmentEmbedded, VideoDeleted
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink, LogTimingSink, TimingSink
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from track_stitcher.profile import StitchProfile
from track_stitcher.redis_state import RedisJobStateStore
from track_stitcher.service import StitcherService
from track_stitcher.settings import TrackStitcherSettings, WorkerSettings
from track_stitcher.state import JobStateStore
from track_stitcher.sweeper import SERVICE_NAME, Sweeper

logger = logging.getLogger(__name__)


def load_stitch_profile(settings: TrackStitcherSettings) -> StitchProfile:
    """기준값 프로필을 읽는다. 없거나 범위 밖이면 `ProfileError`로 기동에 실패한다(R-22)."""
    return load_profile(settings.profile_path, StitchProfile)


def build(
    worker: WorkerSettings,
    settings: TrackStitcherSettings,
    storage: ObjectStorage,
    store: JobStateStore,
    profile: StitchProfile,
    health: HealthState,
    kafka: Mapping[str, str] | None = None,
) -> tuple[KafkaWorker, Sweeper]:
    """워커와 주기 확인을 조립한다. 테스트는 저장소·상태 저장소·health·Kafka 설정을 바꿔 넣는다."""
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    timing: TimingSink = (
        KafkaTimingSink(publisher) if worker.stage_timing_publish else LogTimingSink()
    )
    service = StitcherService(store, storage, publisher, profile, lease_s=settings.finalize_lease_s)
    consumer = KafkaWorker(
        WorkerConfig(
            bootstrap=worker.kafka_bootstrap,
            group_id=settings.group_id,
            kafka=kafka or {},
            service=SERVICE_NAME,
        ),
        [
            route("chunk.boundary", ChunkBoundary, service.on_chunk_boundary),
            route("segment.embedded", SegmentEmbedded, service.on_segment_embedded),
            route("video.deleted", VideoDeleted, service.on_video_deleted),
        ],
        publisher,
        health=health,
        timing=timing,
    )
    sweeper = Sweeper(store, service, interval_s=settings.sweep_interval_s, timing=timing)
    return consumer, sweeper


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    settings = TrackStitcherSettings()
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    profile = load_stitch_profile(settings)
    logger.info("stitch profile loaded", extra={"stitchProfile": profile.version})
    store = RedisJobStateStore(settings.redis_url.get_secret_value())
    consumer, sweeper = build(
        worker_settings, settings, S3Storage(StorageSettings()), store, profile, health
    )
    stop = threading.Event()
    threading.Thread(target=sweeper.run, args=(stop,), name="sweeper", daemon=True).start()
    try:
        consumer.run()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
