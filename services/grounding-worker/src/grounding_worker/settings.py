"""grounding-worker 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from vlm_common.settings import ServiceSettings

from grounding_worker.profile import SAFE_PATH_PATTERN


class WorkerSettings(ServiceSettings):
    """Kafka 워커 공통 설정. 환경변수 `KAFKA_BOOTSTRAP`, `HEALTH_PORT`, `LOG_LEVEL`,
    `STAGE_TIMING_PUBLISH`."""

    kafka_bootstrap: str
    # docs 서비스 카탈로그 "포트 규칙": 워커는 컨테이너 포트 8000에 health 전용 서버만 연다.
    health_port: int = 8000
    # 처리 시간 측정 묶음을 stage.timing에 발행한다. 끄면 JSON 로그로만 남긴다(SPEC-013 R-5).
    stage_timing_publish: bool = False


class GroundingWorkerSettings(BaseSettings):
    """grounding-worker 설정. 환경변수는 `GROUNDING_WORKER_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="GROUNDING_WORKER_", frozen=True, extra="ignore")

    group_id: str = "grounding-worker"
    # 탐지기·렌더링 프로필(TOML). 이미지에 넣은 profiles/ 아래 파일을 가리킨다.
    profile_path: Path
    # mock 탐지기는 CI·로컬 전용이다. 운영에서 실수로 고르지 않도록 따로 켜야 한다.
    allow_mock: bool = False
    # 탐지기 가중치를 받는 모델 버킷. 영상 버킷(S3_BUCKET)과 권한을 나눈다(mock은 불필요).
    models_bucket: str | None = None
    # 가중치 로컬 캐시. 운영은 Pod의 emptyDir을 여기에 붙여 재시작 때 다시 받지 않는다.
    model_cache_dir: Path = Path("/var/cache/vlm-models")
    # SPEC-026 R-13: 구간 하나(probe~렌더링)의 제한 시간
    item_timeout_s: float = Field(default=120, gt=0)
    # SPEC-026 R-2: 샘플 간격. 결과 파일 sampleFps 상한(5)에 맞춰 200ms 이상이다.
    sample_interval_ms: int = Field(default=1_000, ge=200)
    # SPEC-026 R-2: 구간당 최대 샘플 수. 계약 frames 상한이 12다.
    max_samples: int = Field(default=12, ge=1, le=12)
    # 구간마다 임시 폴더를 만드는 곳. 없으면 시스템 임시 폴더다. 필터 스크립트에 경로가 들어가므로
    # 특수 문자를 허용하지 않는다.
    work_dir: Path | None = None

    @field_validator("work_dir", "models_bucket", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # compose는 값이 없는 변수를 빈 문자열로 넘긴다.
        return None if value == "" else value

    @field_validator("work_dir")
    @classmethod
    def _safe_work_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not re.fullmatch(SAFE_PATH_PATTERN, str(value)):
            raise ValueError("work_dir must be an absolute path of [A-Za-z0-9_./-]")
        return value
