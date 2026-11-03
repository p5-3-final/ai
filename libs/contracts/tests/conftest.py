import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

# 루트 구조(project/repos/ai)에서 docs 레포는 두 단계 위에 있다.
# CI는 CONTRACTS_DIR로 checkout 경로를 준다.
_DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[5] / "docs" / "contracts"


class _ContractSettings(BaseSettings):
    contracts_dir: Path = _DEFAULT_CONTRACTS_DIR


type JsonObject = dict[str, object]
type SchemaLoader = Callable[[str], JsonObject]


@pytest.fixture(scope="session")
def load_schema() -> SchemaLoader:
    """이름(`video.uploaded`, `_envelope`)으로 이벤트 JSON Schema 원본을 읽는다."""
    events_dir = _ContractSettings().contracts_dir / "events"
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert events_dir.is_dir(), f"계약 원본을 찾을 수 없다: {events_dir} (CONTRACTS_DIR 확인)"

    def load(name: str) -> JsonObject:
        loaded: JsonObject = json.loads(
            (events_dir / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        return loaded

    return load
