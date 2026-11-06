"""frame-encoder 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from vlm_common.settings import ServiceSettings


class WorkerSettings(ServiceSettings):
    """Kafka 워커 공통 설정. 환경변수 `KAFKA_BOOTSTRAP`, `HEALTH_PORT`, `LOG_LEVEL`,
    `STAGE_TIMING_PUBLISH`."""

    kafka_bootstrap: str
    # docs 서비스 카탈로그 "포트 규칙": 워커는 컨테이너 포트 8000에 health 전용 서버만 연다.
    health_port: int = 8000
    # 처리 시간 측정 묶음을 stage.timing에 발행한다. 끄면 JSON 로그로만 남긴다(SPEC-013 R-5).
    stage_timing_publish: bool = False


class FrameEncoderSettings(BaseSettings):
    """frame-encoder 설정. 환경변수는 `FRAME_ENCODER_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="FRAME_ENCODER_", frozen=True, extra="ignore")

    group_id: str = "frame-encoder"
    # 인코더·프로토타입 프로필(TOML). 이미지에 넣은 profiles/ 아래 파일을 가리킨다.
    profile_path: Path
    # 인코더 가중치를 받는 모델 버킷. 영상 버킷(S3_BUCKET)과 권한을 나눈다(mock은 불필요).
    models_bucket: str | None = None
    # 가중치 로컬 캐시. 운영은 Pod의 emptyDir을 여기에 붙여 재시작 때 다시 받지 않는다.
    model_cache_dir: Path = Path("/var/cache/vlm-models")
    # mock 인코더는 CI·로컬 전용이다. 운영에서 실수로 고르지 않도록 따로 켜야 한다.
    allow_mock: bool = False

    @field_validator("models_bucket", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        # compose는 값이 없는 변수를 빈 문자열로 넘긴다.
        return None if value == "" else value
