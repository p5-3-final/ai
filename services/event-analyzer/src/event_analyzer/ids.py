"""이벤트 ID(SPEC-020 R-9, SPEC-023 R-8·R-10).

입력이 같으면 같은 eventId를 쓴다. 같은 메시지를 다시 처리해도 소비자가 eventId로 중복을 흡수한다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5

from vlm_contracts.events import AlertStatus


def analyzed_event_id(candidate_id: UUID) -> UUID:
    """후보 하나의 `event.analyzed`."""
    return uuid5(NAMESPACE_URL, f"vlm:event.analyzed:{candidate_id}")


def manifest_event_id(execution_id: UUID) -> UUID:
    """청크 실행 하나의 `event.manifest`."""
    return uuid5(NAMESPACE_URL, f"vlm:event.manifest:{execution_id}")


def alert_event_id(candidate_id: UUID, status: AlertStatus) -> UUID:
    """긴급 후보 하나의 상태별 `alert.raised`(SPEC-023 R-21). 다시 처리해도 상태마다 한 건이다."""
    return uuid5(NAMESPACE_URL, f"vlm:alert.raised:{candidate_id}:{status}")
