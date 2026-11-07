"""aggregator 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from vlm_common.settings import ServiceSettings

from aggregator.profile import DEFAULT_PROFILE_PATH


class WorkerSettings(ServiceSettings):
    """Kafka 워커 공통 설정. 환경변수 `KAFKA_BOOTSTRAP`, `HEALTH_PORT`, `LOG_LEVEL`,
    `STAGE_TIMING_PUBLISH`."""

    kafka_bootstrap: str
    # docs 서비스 카탈로그 "포트 규칙": 워커는 컨테이너 포트 8000에 health 전용 서버만 연다.
    health_port: int = 8000
    # 처리 시간 측정 묶음을 stage.timing에 발행한다. 끄면 JSON 로그로만 남긴다(SPEC-013 R-5).
    stage_timing_publish: bool = False


class AggregatorSettings(BaseSettings):
    """aggregator 설정. 환경변수는 `AGGREGATOR_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="AGGREGATOR_", frozen=True, extra="ignore")

    group_id: str = "aggregator"
    ai_gateway_url: str = Field(min_length=1)
    # analysis-api 내부 주소(예: http://analysis-api:8085)
    analysis_api_url: str = Field(min_length=1)
    # stream-service 내부 주소. 없으면 실시간(STREAM) 메시지를 처리하지 않는다(재시도 가능 오류).
    stream_service_url: str | None = None
    # 대기 집합·세션 상태·재판정 계수(R-25~R-31). 운영 필수다. 없으면 업로드 기본 경로만 동작한다.
    redis_url: SecretStr | None = None
    profile_path: Path = DEFAULT_PROFILE_PATH
    # 메시지 하나의 내부 읽기 마감(R-3 기본 300초)
    read_timeout_s: float = Field(default=300, gt=0)
    # 내부 읽기 시도 하나의 제한 시간 상한. 남은 마감이 더 짧으면 그쪽을 쓴다.
    read_call_timeout_s: float = Field(default=30, gt=0)
    # 쪽 크기(계약 limit 상한 1000)
    page_limit: int = Field(default=1000, ge=1, le=1000)
    # 503·연결 실패 재시도(SPEC-021 R-10 규약 2회)
    max_retries: int = Field(default=2, ge=0)
    retry_base_delay_s: float = Field(default=1.0, ge=0)
    # ai-gateway 호출(요약·재판정) 하나의 제한 시간. 로컬 CPU 모델 제한(120초)보다 길게 둔다.
    gateway_timeout_s: float = Field(default=180, gt=0)
    # 메시지 처리 최대 시도 수(R-4 재시도 상한 기본 5회). 마지막 시도에서 개수가 다르면 partial이다.
    max_attempts: int = Field(default=5, ge=1)

    @field_validator("stream_service_url", "redis_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # compose·ConfigMap은 빈 문자열로 "없음"을 나타낸다.
        return None if value == "" else value
