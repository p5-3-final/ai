"""track-stitcher 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap·Secrets)."""

from pathlib import Path

from pydantic import Field, SecretStr
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


class TrackStitcherSettings(BaseSettings):
    """track-stitcher 설정. 환경변수는 `TRACK_STITCHER_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="TRACK_STITCHER_", frozen=True, extra="ignore")

    group_id: str = "track-stitcher"
    # 기준값 프로필(TOML). 이미지에 넣은 profiles/ 아래 파일을 가리킨다(R-22).
    profile_path: Path
    # Job 대기 상태 Redis. 운영 주소에 인증 정보가 들어갈 수 있어 비밀값으로 다룬다.
    redis_url: SecretStr
    # 대기 제한 시간 확인 간격(R-15). 제한 시간(기본 30분)에 비해 짧으면 된다.
    sweep_interval_s: float = Field(default=30, gt=0)
    # 확정 임대 시간(R-16). 1시간 영상 확정(NFR-1, 30초)보다 넉넉히 길어야 두 Pod가 함께 확정하지
    # 않는다. Pod가 죽으면 이 시간 뒤 다른 Pod가 이어 받는다.
    finalize_lease_s: float = Field(default=300, gt=0)
