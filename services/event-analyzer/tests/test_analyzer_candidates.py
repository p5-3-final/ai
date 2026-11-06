"""후보 목록 읽기(SPEC-023 R-1, R-2, US-4)."""

from uuid import UUID

import pytest
from ea_support import (
    CHUNK,
    PREFIX,
    candidate_file,
    detected,
    detector_message,
    file_message,
)
from event_analyzer.candidates import load_candidates
from vlm_messaging.worker import NonRetryableError
from vlm_storage.memory import InMemoryStorage

KEY = f"{PREFIX}candidates.json"


def _storage(data: bytes | None) -> InMemoryStorage:
    storage = InMemoryStorage()
    if data is not None:
        storage.put_bytes(KEY, data, "application/json")
    return storage


def test_spec023_r1_inline_candidates_are_used_as_is() -> None:
    candidates = [detected(1), detected(2, start_s=40)]

    assert load_candidates(detector_message(*candidates), InMemoryStorage()) == candidates


def test_spec023_r1_us4_candidates_are_read_from_the_file() -> None:
    candidates = [detected(1), detected(2, start_s=40)]
    storage = _storage(candidate_file(*candidates))

    assert load_candidates(file_message(*candidates), storage) == candidates


@pytest.mark.parametrize(
    "data",
    [
        None,
        b"not json",
        b'{"schemaVersion": 1}',
        candidate_file(detected(1), execution_id=UUID(int=9)),
        candidate_file(detected(1), source_id=UUID(int=9)),
    ],
    ids=["missing", "not-json", "not-contract", "other-execution", "other-source"],
)
def test_spec023_r2_us4_unusable_file_is_not_retried(data: bytes | None) -> None:
    with pytest.raises(NonRetryableError):
        load_candidates(file_message(detected(1)), _storage(data))


def test_spec023_r2_file_count_must_match_candidate_count() -> None:
    storage = _storage(candidate_file(detected(1), detected(2, start_s=40)))

    with pytest.raises(NonRetryableError, match="candidateCount"):
        load_candidates(file_message(detected(1), count=3), storage)


def test_spec023_d10_file_frames_outside_execution_folder_are_rejected() -> None:
    other_chunk = PREFIX.replace(str(CHUNK), str(UUID(int=7)))
    outside = detected(1).model_copy(
        update={"evidence_frame_keys": [f"{other_chunk}blurred/a.jpg"]}
    )
    storage = _storage(candidate_file(outside))

    with pytest.raises(NonRetryableError):
        load_candidates(file_message(outside), storage)


class _BrokenStorage(InMemoryStorage):
    def get_bytes(self, key: str) -> bytes:
        raise ConnectionError("storage down")


def test_spec023_r2_other_storage_errors_are_raised_for_retry() -> None:
    with pytest.raises(ConnectionError):
        load_candidates(file_message(detected(1)), _BrokenStorage())
