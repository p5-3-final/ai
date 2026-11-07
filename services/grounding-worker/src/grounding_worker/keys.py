"""클립·결과 파일 경로(SPEC-026 R-6). 탐지기 버전이 경로에 들어가 버전마다 따로 만든다."""

from uuid import UUID


def _folder(source_id: UUID, grounding_id: UUID, grounding_model: str) -> str:
    return f"sources/{source_id}/groundings/{grounding_id}/{grounding_model}"


def clip_key(source_id: UUID, grounding_id: UUID, grounding_model: str) -> str:
    return f"{_folder(source_id, grounding_id, grounding_model)}/clip.mp4"


def result_key(source_id: UUID, grounding_id: UUID, grounding_model: str) -> str:
    return f"{_folder(source_id, grounding_id, grounding_model)}/result.json"
