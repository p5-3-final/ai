"""정규화 결과의 식별자·저장 경로·이벤트 ID와 연동 시간(SPEC-007 R-6, R-14, ADR-0009).

정규화 결과는 불변이다. 같은 결과를 다시 전달하면 같은 ID를 쓰고, 실제로 다시 정규화하면
새 normalizationId·경로·eventId를 쓴다. 경로는 media-service가 영상 삭제 때 지우는
`sources/<id>/` 아래에 둔다(SPEC-003 R-20).
"""

from decimal import ROUND_HALF_UP, Decimal
from uuid import NAMESPACE_URL, UUID, uuid5


def normalization_id(trigger_event_id: UUID, version: str) -> UUID:
    """정규화를 시작시킨 이벤트와 정규화 버전으로 결과 ID를 정한다.

    같은 `video.uploaded`를 다시 받으면(재시도·재배치) 같은 결과로 보고, 정규화 방식(버전)이
    바뀌면 새 결과로 본다.
    """
    return uuid5(NAMESPACE_URL, f"vlm:normalization:{trigger_event_id}:{version}")


def _prefix(source_id: UUID, normalization: UUID) -> str:
    return f"sources/{source_id}/normalizations/{normalization}/"


def normalized_key(source_id: UUID, normalization: UUID) -> str:
    return f"{_prefix(source_id, normalization)}normalized.mp4"


def hls_prefix(source_id: UUID, normalization: UUID) -> str:
    return f"{_prefix(source_id, normalization)}hls/"


def hls_playlist_key(source_id: UUID, normalization: UUID) -> str:
    return f"{hls_prefix(source_id, normalization)}index.m3u8"


def normalizing_event_id(source_id: UUID) -> UUID:
    """`video.normalizing`은 시작 신호라 영상마다 하나다(SPEC-007 R-14)."""
    return uuid5(NAMESPACE_URL, f"vlm:video.normalizing:{source_id}")


def normalized_event_id(normalization: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:video.normalized:{normalization}")


def duration_ms(seconds: float) -> int:
    """초를 정수 밀리초로 반올림한다. 0으로 내려가지 않게 최소 1이다.

    float 연산 오차로 경계값이 엇갈리지 않도록 10진수로 바꿔 반올림한다.
    """
    millis = (Decimal(repr(seconds)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return max(1, int(millis))
