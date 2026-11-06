"""저장소의 트랙 요약·벡터 파일 읽기와 검증(SPEC-022 R-1, R-10, R-18).

내용(좌표·벡터)은 로그에 남기지 않는다(NFR-4). 오류 메시지에도 키와 ID만 싣는다.
"""

import logging

from pydantic import ValidationError
from vlm_common.errors import VlmError
from vlm_contracts.events import ChunkBoundary
from vlm_contracts.tracks import ChunkTracks
from vlm_contracts.vectors import VectorFile
from vlm_messaging.worker import NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from track_stitcher.state import ChunkRecord, JobKey, VectorRecord

logger = logging.getLogger(__name__)


class TrackFileUnavailableError(VlmError):
    """기록해 둔 청크의 트랙 파일을 다시 읽지 못했다. 저장소 일시 장애일 수 있어 재시도한다."""

    code = "STITCHER_TRACK_FILE_UNAVAILABLE"


def read_message_tracks(storage: ObjectStorage, event: ChunkBoundary) -> ChunkTracks:
    """`chunk.boundary`의 트랙 요약 파일을 읽어 메시지와 맞는지 확인한다.

    Raises:
        NonRetryableError: 파일이 없거나, JSON·계약이 틀리거나, 파일의 ID·범위가 메시지와 다를 때
            (R-18). 다시 읽어도 결과가 같으므로 기록하지 않고 DLQ로 보낸다.
    """
    try:
        raw = storage.get_bytes(event.tracks_key)
    except ObjectNotFoundError as e:
        raise NonRetryableError(f"tracks file not found: {event.tracks_key}") from e
    try:
        tracks = ChunkTracks.model_validate_json(raw)
    except ValidationError as e:
        # 검증 오류 본문에는 좌표 값이 들어 있어 개수만 남긴다(NFR-4).
        raise NonRetryableError(
            f"tracks file violates contract: {event.tracks_key} ({e.error_count()} errors)"
        ) from e
    expected = (
        event.source_id,
        event.job_id,
        event.chunk_id,
        event.execution_id,
        event.normalization_id,
        event.owned_range,
        event.processing_range,
    )
    actual = (
        tracks.source_id,
        tracks.job_id,
        tracks.chunk_id,
        tracks.execution_id,
        tracks.normalization_id,
        tracks.owned_range,
        tracks.processing_range,
    )
    if actual != expected:
        raise NonRetryableError(f"tracks file does not match message: {event.tracks_key}")
    return tracks


def read_recorded_tracks(storage: ObjectStorage, key: JobKey, chunk: ChunkRecord) -> ChunkTracks:
    """기록할 때 검증한 트랙 파일을 다시 읽는다(이웃 청크, 확정).

    Raises:
        TrackFileUnavailableError: 파일이 사라졌거나 바뀌었을 때. 메시지 탓이 아니라 재시도한다.
    """
    try:
        tracks = ChunkTracks.model_validate_json(storage.get_bytes(chunk.tracks_key))
    except (ObjectNotFoundError, ValidationError) as e:
        raise TrackFileUnavailableError(
            f"recorded tracks file unreadable: {chunk.tracks_key}"
        ) from e
    if (tracks.source_id, tracks.job_id, tracks.execution_id) != (
        key.source_id,
        key.job_id,
        chunk.execution_id,
    ):
        raise TrackFileUnavailableError(f"recorded tracks file changed: {chunk.tracks_key}")
    return tracks


def read_vectors(
    storage: ObjectStorage, key: JobKey, chunk: ChunkRecord, record: VectorRecord
) -> VectorFile | None:
    """실행의 벡터 파일. 없거나 계약·ID가 틀리면 경고를 남기고 None이다.

    확정은 메시지 하나의 처리가 아니라 DLQ로 보낼 대상이 없다. 그 실행의 트랙만 벡터 없음으로
    보고 Re-ID에서 뺀다(R-10, plan 8절 5번).
    """
    try:
        vectors = VectorFile.model_validate_json(storage.get_bytes(record.vectors_key))
    except (ObjectNotFoundError, ValidationError) as e:
        logger.warning(
            "vector file unreadable, its tracks skip re-id",
            extra={"vectorsKey": record.vectors_key, "error": type(e).__name__},
        )
        return None
    ids = (vectors.source_id, vectors.job_id, vectors.chunk_id, vectors.execution_id)
    if ids != (key.source_id, key.job_id, chunk.chunk_id, chunk.execution_id) or (
        vectors.embedding_model != record.embedding_model
    ):
        logger.warning(
            "vector file does not match its message, its tracks skip re-id",
            extra={"vectorsKey": record.vectors_key},
        )
        return None
    return vectors


def crop_sums(vectors: VectorFile) -> dict[str, list[float]]:
    """trackLocalId별 크롭 벡터 합. 원본 벡터를 버려 확정 메모리를 줄인다(plan 7절)."""
    sums: dict[str, list[float]] = {}
    for crop in vectors.crops:
        total = sums.get(crop.track_local_id)
        sums[crop.track_local_id] = (
            list(crop.vector)
            if total is None
            else [a + b for a, b in zip(total, crop.vector, strict=True)]
        )
    return sums
