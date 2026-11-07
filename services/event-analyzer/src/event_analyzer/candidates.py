"""`event.candidate`의 후보 목록 읽기(SPEC-023 R-1, R-2).

후보가 256개를 넘으면 chunk-processor가 실행 폴더의 `candidates.json`에 둔다. 읽을 수 없는 파일은
다시 시도해도 같으므로 DLQ로 보내고, 저장소의 일시 장애는 그대로 던져 재시도 토픽으로 보낸다.
"""

from pydantic import ValidationError
from vlm_contracts.candidates import CandidateFile
from vlm_contracts.events import DetectorCandidate, EventCandidate, frames_outside
from vlm_messaging.worker import NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage


def load_candidates(message: EventCandidate, storage: ObjectStorage) -> list[DetectorCandidate]:
    """메시지의 후보, 없으면 후보 파일의 후보. 저장소 호출은 블로킹이다.

    Raises:
        NonRetryableError: 파일이 없거나, 계약에 맞지 않거나, 후보 수가 `candidateCount`와 다르거나,
            다른 실행의 파일일 때(R-2).
    """
    if message.candidates is not None:
        return message.candidates
    # 모델 검증이 candidates와 candidatesKey 중 하나가 있음을 보장한다.
    key = message.candidates_key or ""
    try:
        data = storage.get_bytes(key)
    except ObjectNotFoundError as e:
        raise NonRetryableError("candidate file not found") from e
    try:
        file = CandidateFile.model_validate_json(data)
    except ValidationError as e:
        raise NonRetryableError("candidate file does not match the contract") from e
    # spec에 따로 적히지 않았지만 다른 실행의 후보를 섞지 않도록 "계약에 맞지 않음"으로 본다.
    ids = (file.source_id, file.job_id, file.chunk_id, file.execution_id)
    if ids != (message.source_id, message.job_id, message.chunk_id, message.execution_id):
        raise NonRetryableError("candidate file belongs to another execution")
    if len(file.candidates) != message.candidate_count:
        raise NonRetryableError("candidate file count does not match candidateCount")
    # SPEC-023 D-10: 메시지에 실린 후보와 같은 근거 프레임 검사를 파일 후보에도 한다.
    if frames_outside(message.execution_prefix(), file.candidates):
        raise NonRetryableError("evidence frame key is outside the execution folder")
    return file.candidates
