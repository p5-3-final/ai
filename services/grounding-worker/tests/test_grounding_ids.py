"""eventId와 클립·결과 경로(SPEC-026 R-6, R-9)."""

from uuid import UUID

from grounding_worker.ids import completed_event_id
from grounding_worker.keys import clip_key, result_key

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
GROUNDING = UUID("0192f300-0000-5000-8000-0000000000a1")
REQUEST = UUID("0192f310-0000-7000-8000-000000000001")


def test_spec026_r9_event_id_is_deterministic() -> None:
    first = completed_event_id(GROUNDING, "m-v1", REQUEST, "READY")

    assert first == completed_event_id(GROUNDING, "m-v1", REQUEST, "READY")
    assert first.version == 5


def test_spec026_r9_event_id_changes_with_model_request_and_outcome() -> None:
    base = completed_event_id(GROUNDING, "m-v1", REQUEST, "READY")

    assert base != completed_event_id(GROUNDING, "m-v2", REQUEST, "READY")
    assert base != completed_event_id(GROUNDING, "m-v1", UUID(int=1), "READY")
    assert base != completed_event_id(GROUNDING, "m-v1", REQUEST, "FAILED")


def test_spec026_r6_paths_follow_the_contract() -> None:
    folder = f"sources/{SOURCE}/groundings/{GROUNDING}/m-v1"

    assert clip_key(SOURCE, GROUNDING, "m-v1") == f"{folder}/clip.mp4"
    assert result_key(SOURCE, GROUNDING, "m-v1") == f"{folder}/result.json"
