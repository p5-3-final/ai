"""ai-gateway 설정. 값은 환경변수에서만 읽는다(로컬은 compose, 운영은 ConfigMap·Secrets Manager)."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
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
    # 모델 저장소의 Registry 묶음(프롬프트·분류 체계·프로필) 키와 파일 해시(SPEC-009 R-26).
    # 둘 다 있으면 이미지의 registry_dir 대신 묶음을 쓰고, profile_path는 묶음 안의 상대 경로다.
    registry_bundle: str | None = None
    registry_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # mock Adapter는 CI·로컬 전용이다. 운영에서 실수로 고르지 않도록 따로 켜야 한다(SPEC-009 R-15).
    allow_mock: bool = False
    kafka_bootstrap: str = Field(validation_alias="KAFKA_BOOTSTRAP")
    group_id: str = "ai-gateway"
    # 처리 시간 측정 묶음을 stage.timing에 발행한다. 끄면 JSON 로그로만 남긴다(SPEC-013 R-5).
    stage_timing_publish: bool = False
    openai_api_key: SecretStr | None = None
    # 인코더 가중치를 받는 모델 버킷(SPEC-011 R-15). 영상 버킷(S3_BUCKET)과 권한을 나눈다.
    models_bucket: str | None = None
    # 가중치 로컬 캐시. 운영은 Pod의 emptyDir을 여기에 붙여 재시작 때 다시 받지 않는다.
    model_cache_dir: Path = Path("/var/cache/vlm-models")
    # 결과 캐시 앞단·동시 호출 잠금·분당 한도를 Pod 사이에 나누는 Redis(SPEC-016). 비우면 DB 캐시와
    # Pod별 한도만 쓴다. 비밀번호가 들어갈 수 있어 감싼다.
    redis_url: SecretStr | None = None

    @field_validator("registry_bundle", "registry_bundle_sha256", "redis_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # compose는 값이 없는 변수를 빈 문자열(${VAR:-})로 넘긴다. 묶음을 쓰지 않는다는 뜻이다.
        return None if value == "" else value
