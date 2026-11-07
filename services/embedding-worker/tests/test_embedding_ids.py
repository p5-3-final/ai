"""eventId와 텍스트 벡터 파일 경로(SPEC-025 R-6, R-8)."""

import json
import re
from uuid import UUID

from embedding_worker.files import vector_key
from embedding_worker.ids import ready_event_id
from ew_support import JOB, MODEL, SOURCE, TRACK, contracts_dir

ITEM = UUID("0192f100-0000-5000-8000-0000000000f1")


def test_spec025_r8_event_id_is_deterministic() -> None:
    assert ready_event_id("event", ITEM, MODEL) == ready_event_id("event", ITEM, MODEL)


def test_spec025_r8_event_id_differs_by_kind_item_and_model() -> None:
    ids = {
        ready_event_id("event", ITEM, MODEL),
        ready_event_id("track", ITEM, MODEL),
        ready_event_id("event", TRACK, MODEL),
        ready_event_id("event", ITEM, "other-model"),
    }

    assert len(ids) == 4


def test_spec025_r6_vector_key_follows_contract_pattern() -> None:
    schema = json.loads(
        (contracts_dir() / "events/embedding.ready.schema.json").read_text(encoding="utf-8")
    )
    pattern = schema["properties"]["vectorKey"]["pattern"]

    key = vector_key(SOURCE, JOB, "segment_annotation", ITEM)

    assert key == f"sources/{SOURCE}/jobs/{JOB}/text-vectors/segment_annotation/{ITEM}.json"
    assert re.fullmatch(pattern, key)
