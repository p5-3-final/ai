"""track-analyzer 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

from pydantic import Field
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


class TrackAnalyzerSettings(BaseSettings):
    """track-analyzer 설정. 환경변수는 `TRACK_ANALYZER_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="TRACK_ANALYZER_", frozen=True, extra="ignore")

    group_id: str = "track-analyzer"
    # ai-gateway 주소(예: http://ai-gateway:8000)
    ai_gateway_url: str = Field(min_length=1)
    # 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값이 X-Request-Deadline이다
    # (SPEC-024 R-9, SPEC-021 R-8). 화면이 기다리는 요청이라 event-analyzer(180초)보다 짧다.
    call_timeout_s: float = Field(default=60, gt=0)
    # 요청이 닿지 않은 연결 실패의 재시도 횟수와 첫 대기. 502·503·504·시간 초과는 VLM 비용이 두 번
    # 들 수 있어 다시 부르지 않는다(SPEC-021 R-9).
    max_retries: int = Field(default=2, ge=0)
    retry_base_delay_s: float = Field(default=1.0, ge=0)
