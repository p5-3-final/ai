from uuid import UUID

import pytest
from normalize.ids import (
    duration_ms,
    hls_playlist_key,
    hls_prefix,
    normalization_id,
    normalized_event_id,
    normalized_key,
    normalizing_event_id,
)

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
UPLOADED = UUID("0192f0d3-0000-7000-8000-000000000001")
OTHER = UUID("0192f0d3-0000-7000-8000-000000000099")
NID = UUID("0192f0d4-0000-7000-8000-000000000001")


def test_r14_same_trigger_and_version_give_same_normalization() -> None:
    # 같은 video.uploaded를 다시 받으면(재전달) 같은 결과로 본다.
    assert normalization_id(UPLOADED, "normalize-v1") == normalization_id(UPLOADED, "normalize-v1")


def test_r14_new_version_or_trigger_is_a_new_normalization() -> None:
    first = normalization_id(UPLOADED, "normalize-v1")

    assert normalization_id(UPLOADED, "normalize-v2") != first
    assert normalization_id(OTHER, "normalize-v1") != first


def test_r14_keys_live_under_the_normalization_prefix() -> None:
    base = f"sources/{SOURCE}/normalizations/{NID}/"

    assert normalized_key(SOURCE, NID) == f"{base}normalized.mp4"
    assert hls_prefix(SOURCE, NID) == f"{base}hls/"
    assert hls_playlist_key(SOURCE, NID) == f"{base}hls/index.m3u8"


def test_r14_keys_stay_under_the_source_prefix_deleted_by_media() -> None:
    # media-service가 영상 삭제 때 sources/<id>/ 아래를 지운다(SPEC-003 R-20).
    assert normalized_key(SOURCE, NID).startswith(f"sources/{SOURCE}/")


def test_r14_normalized_event_id_follows_the_result() -> None:
    assert normalized_event_id(NID) == normalized_event_id(NID)
    assert normalized_event_id(NID) != normalized_event_id(UUID(int=1))
    assert normalized_event_id(NID).version == 5


def test_r14_normalizing_event_id_keeps_the_per_source_policy() -> None:
    assert normalizing_event_id(SOURCE) == normalizing_event_id(SOURCE)
    assert normalizing_event_id(SOURCE) != normalized_event_id(SOURCE)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(612.48, 612_480), (125.001, 125_001), (0.0004, 1), (3.0005, 3_001), (2.9994, 2_999)],
)
def test_r6_duration_is_rounded_to_whole_milliseconds(seconds: float, expected: int) -> None:
    assert duration_ms(seconds) == expected
