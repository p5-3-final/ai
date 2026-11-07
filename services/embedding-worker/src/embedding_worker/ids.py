"""이벤트 ID(SPEC-025 R-8).

같은 항목·같은 임베딩 모델이면 다시 처리해도 같은 eventId다. 소비자는 eventId로 중복을 흡수한다.
모델이 바뀌면 eventId가 달라져 새 벡터로 적재된다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5

from vlm_contracts.events import ItemKind


def ready_event_id(item_kind: ItemKind, item_id: UUID, model_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"vlm:embedding.ready:{item_kind}:{item_id}:{model_id}")
