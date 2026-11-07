"""트랙 번역표에서 최종 트랙의 시각·클래스를 찾는다(SPEC-025 R-7, R-12)."""

from uuid import UUID

import pytest
from embedding_worker.tracks import TrackLookup
from ew_support import JOB, SOURCE, TRACK, mapping_key, track_mapping
from vlm_messaging.worker import NonRetryableError
from vlm_storage.memory import InMemoryStorage
from vlm_storage.storage import ObjectNotFoundError

OTHER = UUID("0192f100-0000-5000-8000-0000000000e2")


class CountingStorage(InMemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return super().get_bytes(key)


def _storage(*tracks: UUID) -> CountingStorage:
    storage = CountingStorage()
    storage.put_bytes(mapping_key(), track_mapping(*tracks), "application/json")
    return storage


def test_spec025_r7_finds_class_and_time_of_final_track() -> None:
    lookup = TrackLookup(_storage(TRACK), capacity=2)

    track = lookup.find(SOURCE, JOB, TRACK)

    assert (track.track_id, track.class_name, track.start_ms, track.end_ms) == (
        TRACK,
        "person",
        101000,
        190000,
    )


def test_spec025_r7_mapping_is_read_once_per_job() -> None:
    storage = _storage(TRACK, OTHER)
    lookup = TrackLookup(storage, capacity=2)

    lookup.find(SOURCE, JOB, TRACK)
    lookup.find(SOURCE, JOB, OTHER)

    assert storage.reads == [mapping_key()]


def test_spec025_r7_cache_keeps_only_capacity_jobs() -> None:
    storage = _storage(TRACK)
    job2 = UUID("0192f100-0000-7000-8000-000000000099")
    storage.put_bytes(
        mapping_key(job=job2),
        track_mapping(TRACK).replace(str(JOB).encode(), str(job2).encode()),
        "application/json",
    )
    lookup = TrackLookup(storage, capacity=1)

    lookup.find(SOURCE, JOB, TRACK)
    lookup.find(SOURCE, job2, TRACK)
    lookup.find(SOURCE, JOB, TRACK)

    assert storage.reads == [mapping_key(), mapping_key(job=job2), mapping_key()]


def test_spec025_r12_missing_mapping_is_retryable() -> None:
    lookup = TrackLookup(InMemoryStorage(), capacity=2)

    with pytest.raises(ObjectNotFoundError):
        lookup.find(SOURCE, JOB, TRACK)


def test_spec025_r7_unknown_track_goes_to_dlq_after_one_fresh_read() -> None:
    storage = _storage(OTHER)
    lookup = TrackLookup(storage, capacity=2)
    lookup.find(SOURCE, JOB, OTHER)

    with pytest.raises(NonRetryableError, match="track"):
        lookup.find(SOURCE, JOB, TRACK)

    # 메모리의 번역표가 오래됐을 수 있으므로 한 번 다시 읽고 나서 포기한다.
    assert storage.reads == [mapping_key(), mapping_key()]


def test_spec025_r7_track_added_after_cache_is_found_on_fresh_read() -> None:
    storage = _storage(OTHER)
    lookup = TrackLookup(storage, capacity=2)
    lookup.find(SOURCE, JOB, OTHER)
    storage.put_bytes(mapping_key(), track_mapping(OTHER, TRACK), "application/json")

    assert lookup.find(SOURCE, JOB, TRACK).track_id == TRACK


@pytest.mark.parametrize("content", [b"{}", b"not json"])
def test_spec025_r7_malformed_mapping_goes_to_dlq(content: bytes) -> None:
    storage = InMemoryStorage()
    storage.put_bytes(mapping_key(), content, "application/json")

    with pytest.raises(NonRetryableError, match="mapping"):
        TrackLookup(storage, capacity=2).find(SOURCE, JOB, TRACK)


def test_spec025_r7_mapping_of_other_job_goes_to_dlq() -> None:
    storage = InMemoryStorage()
    other_job = UUID("0192f100-0000-7000-8000-000000000077")
    storage.put_bytes(mapping_key(job=other_job), track_mapping(TRACK), "application/json")

    with pytest.raises(NonRetryableError, match="mapping"):
        TrackLookup(storage, capacity=2).find(SOURCE, other_job, TRACK)
