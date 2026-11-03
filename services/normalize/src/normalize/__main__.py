"""normalize 워커 진입점. `python -m normalize`."""

import shutil

from vlm_common.health import HealthState, serve_health
from vlm_common.logging import configure_logging
from vlm_contracts.events import VideoUploaded
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.worker import KafkaWorker, WorkerConfig, route
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from normalize.decision import Limits
from normalize.ffmpeg import FfmpegTool
from normalize.service import NormalizeConfig, NormalizeService
from normalize.settings import NormalizeSettings, WorkerSettings

SERVICE_NAME = "normalize"


def build_worker(
    worker: WorkerSettings,
    normalize: NormalizeSettings,
    storage: ObjectStorage,
    health: HealthState,
) -> KafkaWorker:
    """설정과 저장소로 워커를 조립한다. 테스트는 저장소와 health를 바꿔 넣는다."""
    normalize.work_dir.mkdir(parents=True, exist_ok=True)
    publisher = KafkaPublisher(bootstrap=worker.kafka_bootstrap, client_id=SERVICE_NAME)
    service = NormalizeService(
        storage,
        FfmpegTool(),
        publisher,
        NormalizeConfig(
            limits=Limits(
                min_duration_s=normalize.min_duration_s,
                max_duration_s=normalize.max_duration_s,
                max_fps=normalize.max_fps,
            ),
            hls_segment_s=normalize.hls_segment_s,
            work_dir=normalize.work_dir,
            normalization_version=normalize.version,
        ),
    )
    return KafkaWorker(
        WorkerConfig(bootstrap=worker.kafka_bootstrap, group_id=normalize.group_id),
        [route("video.uploaded", VideoUploaded, service.handle)],
        publisher,
        health=health,
    )


def main() -> None:
    worker_settings = WorkerSettings()
    configure_logging(SERVICE_NAME, worker_settings.log_level)
    # 이미지에 ffmpeg가 없으면 모든 영상을 거절하게 되므로 기동 단계에서 실패시킨다.
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise SystemExit(f"{binary} not found on PATH")
    health = HealthState()
    serve_health(health, port=worker_settings.health_port)
    storage = S3Storage(StorageSettings())
    build_worker(worker_settings, NormalizeSettings(), storage, health).run()


if __name__ == "__main__":
    main()
