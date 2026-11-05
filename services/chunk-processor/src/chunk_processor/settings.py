"""worker 환경 설정. 운영의 test 대역 사용을 거부한다."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CHUNK_PROCESSOR_ 접두사. 기본 disabled이며 운영 adapter는 아직 없다."""

    model_config = SettingsConfigDict(env_prefix="CHUNK_PROCESSOR_", frozen=True, extra="ignore")
    enabled: bool = False
    environment: Literal["test", "production"] = "production"
    adapter: Literal["verification", "unconfigured"] = "unconfigured"
    database_url: SecretStr
    kafka_bootstrap: str
    group_id: str = "chunk-processor-v1"
    verification_outcome: Literal["success", "failure", "unknown"] = "success"
    work_dir: Path = Path("/work")
    publish_interval_s: float = Field(default=1, gt=0)
    health_port: int = 8000

    @model_validator(mode="after")
    def explicit_adapter(self) -> Self:
        if self.enabled and (self.environment != "test" or self.adapter != "verification"):
            raise ValueError(
                "Only explicit test verification adapter is available; "
                "production inference is not connected"
            )
        return self
