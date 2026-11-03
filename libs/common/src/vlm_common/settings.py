"""서비스 설정의 공통 기반."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """모든 서비스 설정의 기본 클래스. 서비스는 이 클래스를 상속해 필드를 더한다.

    값은 환경변수에서만 읽는다. `.env` 파일은 읽지 않는다(로컬은 compose가 환경변수로 넣는다).
    """

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
