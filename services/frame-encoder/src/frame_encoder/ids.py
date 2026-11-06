"""벡터 파일 경로와 이벤트 ID(SPEC-019 R-3, R-5).

실행(executionId) 하나에 결과가 하나라, 같은 메시지를 다시 처리하면 같은 경로·ID를 쓴다. 경로는
media-service가 영상 삭제 때 지우는 `sources/<id>/` 아래 실행 폴더다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5

from vlm_contracts.events import SegmentReady


def vectors_key(ready: SegmentReady) -> str:
    return f"{ready.execution_prefix()}vectors.json"


def embedded_event_id(execution_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:segment.embedded:{execution_id}")


def candidate_event_id(execution_id: UUID) -> UUID:
    """후보는 실행마다 한 건으로 묶어 발행한다(SPEC-019 R-12)."""
    return uuid5(NAMESPACE_URL, f"vlm:event.candidate.vector:{execution_id}")


def candidate_id(execution_id: UUID, segment_id: UUID, event_type: str) -> UUID:
    """(구간, 유형)마다 하나다. event-analyzer가 같은 후보를 다시 받아도 한 번만 판정한다."""
    return uuid5(NAMESPACE_URL, f"vlm:candidate.vector:{execution_id}:{segment_id}:{event_type}")
