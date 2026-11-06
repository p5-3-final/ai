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
