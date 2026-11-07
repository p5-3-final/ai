import json
from collections.abc import Callable
from pathlib import Path

import pytest
from vlm_testing.contracts import contracts_dir as shared_contracts_dir

type JsonObject = dict[str, object]
type SchemaLoader = Callable[[str], JsonObject]


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    return shared_contracts_dir()


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
