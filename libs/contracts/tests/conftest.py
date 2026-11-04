import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


type JsonObject = dict[str, object]
type SchemaLoader = Callable[[str], JsonObject]


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert path.is_dir(), f"계약 원본을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    return path


@pytest.fixture(scope="session")
def load_schema(contracts_dir: Path) -> SchemaLoader:
    """이름(`video.uploaded`, `_envelope`)으로 이벤트 JSON Schema 원본을 읽는다."""
    events_dir = contracts_dir / "events"

    def load(name: str) -> JsonObject:
        loaded: JsonObject = json.loads(
            (events_dir / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        return loaded

    return load
