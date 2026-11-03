from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings
from vlm_testing.containers import postgres


# 루트 구조(project/repos/ai)에서 docs 레포는 두 단계 위에 있다.
# CI는 CONTRACTS_DIR로 checkout 경로를 준다.
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "docs" / "contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    """docs 계약 원본 폴더. 프롬프트·분류 체계·OpenAPI를 읽는다."""
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 프롬프트 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "prompts").is_dir(), f"계약 원본을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    return path


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """테스트 세션에서 함께 쓰는 PostgreSQL. 테스트는 각자 고유한 sourceId로 데이터를 나눈다."""
    with postgres() as url:
        yield url
