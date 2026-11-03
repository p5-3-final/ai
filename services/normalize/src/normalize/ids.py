"""정규화 결과의 저장 경로와 이벤트 ID. 같은 영상이면 항상 같은 값이다(SPEC-007 R-14).

재처리해도 같은 경로에 덮어쓰고 같은 eventId로 발행하므로, 소비자가 중복을 흡수할 수 있다.
경로는 media-service가 영상 삭제 때 지우는 `sources/<id>/` 아래에 둔다(SPEC-003 R-20).
"""

from uuid import NAMESPACE_URL, UUID, uuid5


def normalized_key(source_id: UUID) -> str:
    return f"sources/{source_id}/normalized/video.mp4"


def hls_prefix(source_id: UUID) -> str:
    return f"sources/{source_id}/normalized/hls/"


def hls_playlist_key(source_id: UUID) -> str:
    return f"{hls_prefix(source_id)}index.m3u8"


def event_id(event_type: str, source_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:{event_type}:{source_id}")
