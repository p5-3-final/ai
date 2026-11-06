"""이벤트 ID(SPEC-024 R-5).

요청 하나에 `track.analyzed`가 하나라, 같은 요청을 다시 처리하면 같은 eventId를 쓴다. 소비자는
eventId로 중복을 흡수한다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5


def analyzed_event_id(request_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:track.analyzed:{request_id}")
