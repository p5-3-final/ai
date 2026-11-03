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
    # docs 레포 docs/contracts. 이미지에는 빌드할 때 복사해 둔다.
    contracts_dir: Path = Path("/app/contracts")
    # mock Adapter는 CI·로컬 전용이다. 운영에서 실수로 고르지 않도록 따로 켜야 한다(SPEC-009 R-15).
    allow_mock: bool = False
    kafka_bootstrap: str = Field(validation_alias="KAFKA_BOOTSTRAP")
    group_id: str = "ai-gateway"
    openai_api_key: SecretStr | None = None
    port: int = 8000
