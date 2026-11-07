"""embedding-worker 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap)."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from vlm_common.settings import ServiceSettings

# 텍스트 벡터 파일 계약의 `text.maxLength`. 이보다 긴 문장은 파일이 계약을 어긴다.
TEXT_MAX_LENGTH = 2000


class WorkerSettings(ServiceSettings):
    """Kafka 워커 공통 설정. 환경변수 `KAFKA_BOOTSTRAP`, `HEALTH_PORT`, `LOG_LEVEL`,
    `STAGE_TIMING_PUBLISH`."""

    kafka_bootstrap: str
    # docs 서비스 카탈로그 "포트 규칙": 워커는 컨테이너 포트 8000에 health 전용 서버만 연다.
    health_port: int = 8000
    # 처리 시간 측정 묶음을 stage.timing에 발행한다. 끄면 JSON 로그로만 남긴다(SPEC-013 R-5).
    stage_timing_publish: bool = False


class EmbeddingWorkerSettings(BaseSettings):
    """embedding-worker 설정. 환경변수는 `EMBEDDING_WORKER_` 접두사를 붙인다."""

    model_config = SettingsConfigDict(env_prefix="EMBEDDING_WORKER_", frozen=True, extra="ignore")

    group_id: str = "embedding-worker"
    # ai-gateway 주소(예: http://ai-gateway:8000)
    ai_gateway_url: str = Field(min_length=1)
    # 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값이 X-Request-Deadline이다
    # (SPEC-021 R-8). 문장 하나의 임베딩이라 VLM 판정(180초)보다 훨씬 짧게 잡는다.
    call_timeout_s: float = Field(default=30, gt=0)
    # 503·연결 실패의 클라이언트 재시도 횟수와 첫 대기(SPEC-021 R-10, SPEC-025 US-3의 2회).
    max_retries: int = Field(default=2, ge=0)
    retry_base_delay_s: float = Field(default=1.0, ge=0)
    # SPEC-025 R-5: 문장 최대 글자 수. 계약 상한을 넘길 수 없다.
    max_chars: int = Field(default=TEXT_MAX_LENGTH, ge=1, le=TEXT_MAX_LENGTH)
    # 메모리에 남겨 둘 Job별 트랙 번역표 수. 같은 Job의 트랙 메시지가 이어서 오므로 작게 둔다.
    track_mapping_cache: int = Field(default=8, ge=1)
