"""chunk-processor 후보 파일 Pydantic 모델(SPEC-023 R-1).

원본은 docs 레포 `service-contracts/candidates/v1/candidate-file.schema.json`이고, 계약 테스트가
둘이 같은지 확인한다. 후보가 `event.candidate` 메시지 상한(256개)을 넘으면 이 파일에 둔다.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from vlm_contracts.events import DetectorCandidate


class CandidateFile(BaseModel):
    """실행 하나의 탐지기·규칙 후보 목록 파일(`candidates.json`)."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    candidates: list[DetectorCandidate] = Field(max_length=20000)
