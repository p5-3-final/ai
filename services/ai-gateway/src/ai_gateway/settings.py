"""ai-gateway 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap·Secrets Manager)."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from vlm_common.settings import ServiceSettings


class GatewaySettings(ServiceSettings):
    """환경변수는 `AI_GATEWAY_` 접두사를 붙인다. Kafka 주소만 워커와 같은 `KAFKA_BOOTSTRAP`이다."""

    model_config = SettingsConfigDict(env_prefix="AI_GATEWAY_", frozen=True, extra="ignore")

    # 비밀번호가 들어 있어 로그·repr에 나오지 않게 감싼다(SPEC-009 NFR-4).
    database_url: SecretStr
    profile_path: Path
    # Prompt Registry 폴더(prompts/ + taxonomy/). 이미지는 빌드할 때 레포의 프롬프트와
    # 잠금 파일 버전의 분류 체계를 합쳐 둔다(docs ADR-0012·0013).
    registry_dir: Path = Path("/app/registry")
    # mock Adapter는 CI·로컬 전용이다. 운영에서 실수로 고르지 않도록 따로 켜야 한다(SPEC-009 R-15).
    allow_mock: bool = False
    kafka_bootstrap: str = Field(validation_alias="KAFKA_BOOTSTRAP")
    group_id: str = "ai-gateway"
    openai_api_key: SecretStr | None = None
    # 인코더 가중치를 받는 모델 버킷(SPEC-011 R-15). 영상 버킷(S3_BUCKET)과 권한을 나눈다.
    models_bucket: str | None = None
    # 가중치 로컬 캐시. 운영은 Pod의 emptyDir을 여기에 붙여 재시작 때 다시 받지 않는다.
    model_cache_dir: Path = Path("/var/cache/vlm-models")
