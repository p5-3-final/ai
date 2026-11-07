"""이벤트 ID(SPEC-026 R-9).

같은 요청의 같은 구간을 다시 처리하면 같은 eventId라 소비자가 중복을 흡수한다. requestId와
outcome도 넣는다. 다른 검색 요청이 같은 클립을 재사용하거나 FAILED 뒤 다시 요청해 READY가 나오면
새 이벤트여야 소비자가 버리지 않는다(spec R-9 문구 수정 필요, 보고 C-9).
"""

from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5


def completed_event_id(
    grounding_id: UUID,
    grounding_model: str,
    request_id: UUID,
    outcome: Literal["READY", "FAILED"],
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"vlm:grounding.completed:{grounding_id}:{grounding_model}:{request_id}:{outcome}",
    )
