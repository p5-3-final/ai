"""텍스트 벡터 파일 경로(SPEC-025 R-6).

경로에 모델 ID가 없다(계약 `embedding.ready`의 vectorKey 패턴). 같은 항목을 다른 모델로 다시
임베딩하면 같은 경로를 마지막 결과로 덮어쓴다.
"""

from uuid import UUID

from vlm_contracts.events import ItemKind


def vector_key(source_id: UUID, job_id: UUID, item_kind: ItemKind, item_id: UUID) -> str:
    return f"sources/{source_id}/jobs/{job_id}/text-vectors/{item_kind}/{item_id}.json"
