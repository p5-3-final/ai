from uuid import UUID

from normalize.ids import event_id, hls_playlist_key, hls_prefix, normalized_key

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OTHER = UUID("0192f0d2-0000-7c3d-8e4f-5a6b7c8d9e10")


def test_r14_keys_are_derived_from_source_id_only() -> None:
    assert normalized_key(SOURCE) == f"sources/{SOURCE}/normalized/video.mp4"
    assert hls_playlist_key(SOURCE) == f"sources/{SOURCE}/normalized/hls/index.m3u8"
    assert hls_playlist_key(SOURCE).startswith(hls_prefix(SOURCE))


def test_r14_keys_live_under_the_source_prefix_deleted_by_media() -> None:
    # media-service가 영상 삭제 때 sources/<id>/ 아래를 지우므로(SPEC-003 R-20)
    # 결과도 그 아래에 둔다.
    assert normalized_key(SOURCE).startswith(f"sources/{SOURCE}/")
    assert hls_prefix(SOURCE).startswith(f"sources/{SOURCE}/")


def test_r14_event_id_is_stable_per_source_and_type() -> None:
    assert event_id("video.normalized", SOURCE) == event_id("video.normalized", SOURCE)
    assert event_id("video.normalized", SOURCE) != event_id("video.normalizing", SOURCE)
    assert event_id("video.normalized", SOURCE) != event_id("video.normalized", OTHER)
    assert event_id("video.normalized", SOURCE).version == 5
