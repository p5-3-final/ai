"""normalize 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from vlm_common.settings import ServiceSettings


class WorkerSettings(ServiceSettings):
    """Kafka 워커 공통 설정. 환경변수 `KAFKA_BOOTSTRAP`, `HEALTH_PORT`, `LOG_LEVEL`."""

    kafka_bootstrap: str
    # docs 서비스 카탈로그 "포트 규칙": 워커는 컨테이너 포트 8000에 health 전용 서버만 연다.
    health_port: int = 8000


class NormalizeSettings(BaseSettings):
    """정규화 기준(SPEC-007 용어). 환경변수는 `NORMALIZE_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="NORMALIZE_", frozen=True, extra="ignore")

    group_id: str = "normalize"
    # 원본과 결과를 잠시 두는 폴더. 운영은 emptyDir 볼륨을 붙인다.
    work_dir: Path = Path("/work")
    min_duration_s: float = Field(default=1.0, gt=0)
    # 4시간
    max_duration_s: float = Field(default=14_400.0, gt=0)
    max_fps: float = Field(default=30.0, gt=0)
    hls_segment_s: float = Field(default=6.0, gt=0)
