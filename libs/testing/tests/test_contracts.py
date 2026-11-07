import json
from pathlib import Path

import pytest
from vlm_testing.contracts import body_event_errors, contracts_dir, schema_errors

EVENT_SCHEMA = "events/embedding.ready.schema.json"


def _example() -> dict[str, object]:
    raw = json.loads((contracts_dir() / EVENT_SCHEMA).read_text(encoding="utf-8"))
    example: dict[str, object] = raw["examples"][0]
    return example


def test_contract_example_event_has_no_errors() -> None:
    assert body_event_errors(_example()) == []


def test_missing_required_field_is_reported_with_path() -> None:
    body = {k: v for k, v in _example().items() if k != "vectorKey"}

    errors = body_event_errors(body)

    assert any("vectorKey" in e for e in errors)


def test_schema_can_be_named_by_id_or_relative_path() -> None:
    schema_id = json.loads((contracts_dir() / EVENT_SCHEMA).read_text(encoding="utf-8"))["$id"]
    body = {**_example(), "dimension": "wide"}

    assert schema_errors(schema_id, body) == schema_errors(EVENT_SCHEMA, body) != []


def test_missing_contracts_dir_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CONTRACTS_DIR", str(tmp_path / "nowhere"))

    with pytest.raises(FileNotFoundError):
        contracts_dir()
