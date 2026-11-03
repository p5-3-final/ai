"""Git으로 관리하는 TOML 프로필(모델 ID·차원·탐지 설정 등)을 읽고 검증한다."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from vlm_common.errors import VlmError


class Profile(BaseModel):
    """프로필 스키마의 기본 클래스.

    오타 난 키가 조용히 무시되어 기본값으로 다른 모델이 쓰이지 않도록 모르는 키를 거부하고,
    기동 후 값이 바뀌지 않도록 불변으로 둔다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileError(VlmError):
    """프로필을 읽거나 검증하지 못했다. 서비스는 이 예외로 기동에 실패해야 한다."""

    code = "PROFILE_INVALID"


def load_profile[M: Profile](path: Path, schema: type[M]) -> M:
    """TOML 프로필을 읽어 `schema`로 검증한다.

    Args:
        path: 프로필 파일 경로.
        schema: 검증에 쓸 `Profile` 하위 클래스.

    Returns:
        검증된 불변 프로필.

    Raises:
        ProfileError: 파일이 없거나, TOML 문법이 틀렸거나, 스키마와 맞지 않을 때.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProfileError(f"profile not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"invalid TOML in {path}: {e}") from e
    try:
        return schema.model_validate(raw)
    except ValidationError as e:
        raise ProfileError(f"profile {path} does not match {schema.__name__}: {e}") from e
