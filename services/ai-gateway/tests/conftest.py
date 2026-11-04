import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings
from vlm_testing.containers import postgres

# Prompt Registry 원본(docs ADR-0012). 분류 체계는 서비스 간 계약에서 가져와 합친다.
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "registry" / "prompts"


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 계약을 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "docs" / "contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    """서비스 간 계약 폴더. OpenAPI 대조 테스트가 읽는다."""
    path = _ContractSettings().contracts_dir
    assert (path / "openapi").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    return path


@pytest.fixture(scope="session")
def registry_dir(contracts_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """이미지의 `/app/registry`와 같은 배치. 프롬프트(ai 레포)와 분류 체계(계약)를 합친다."""
    taxonomy = contracts_dir / "taxonomy"
    # 원본이 없으면 프롬프트 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert taxonomy.is_dir(), f"계약을 찾을 수 없다: {taxonomy} (CONTRACTS_DIR 확인)"
    target = tmp_path_factory.mktemp("registry")
    shutil.copytree(_PROMPTS_DIR, target / "prompts")
    shutil.copytree(taxonomy, target / "taxonomy")
    return target


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """테스트 세션에서 함께 쓰는 PostgreSQL. 테스트는 각자 고유한 sourceId로 데이터를 나눈다."""
    with postgres() as url:
        yield url
