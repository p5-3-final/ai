"""이벤트 ID(SPEC-020 R-9).

후보 하나에 `event.analyzed`가 하나라, 같은 메시지를 다시 처리하면 같은 eventId를 쓴다. 소비자는
eventId로 중복을 흡수한다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5


def analyzed_event_id(candidate_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:event.analyzed:{candidate_id}")
