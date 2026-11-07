"""트랙 번역표 조회(SPEC-025 R-7).

`track.analyzed`에는 트랙 시각·클래스가 없다. track-stitcher가 Job마다 쓰는 번역표
(`tracks/v1/track-mapping`)에서 최종 trackId로 찾는다.
"""

from collections import OrderedDict
from uuid import UUID

from pydantic import ValidationError
from vlm_contracts.tracks import MappedTrack, TrackMapping
from vlm_messaging.worker import NonRetryableError
from vlm_storage.storage import ObjectStorage

type _Tracks = dict[UUID, MappedTrack]


def mapping_key(source_id: UUID, job_id: UUID) -> str:
    """track-stitcher가 번역표를 쓰는 경로(계약 `track.manifest`의 mappingKey)."""
    return f"sources/{source_id}/jobs/{job_id}/tracks/mapping.json"


class TrackLookup:
    """Job별 번역표를 읽어 최종 trackId의 클래스·시각을 돌려준다.

    같은 Job의 트랙 메시지가 이어서 오므로 최근 `capacity`개 Job의 번역표를 메모리에 둔다. 워커는
    메시지를 하나씩 처리하므로(NFR-3) 잠금이 없다.

    Args:
        storage: 번역표를 읽을 저장소.
        capacity: 메모리에 둘 Job 수.
    """

    def __init__(self, storage: ObjectStorage, *, capacity: int) -> None:
        self._storage = storage
        self._capacity = capacity
        self._cache: OrderedDict[UUID, _Tracks] = OrderedDict()

    def find(self, source_id: UUID, job_id: UUID, track_id: UUID) -> MappedTrack:
        """블로킹이다. 워커에서는 `asyncio.to_thread`로 부른다.

        Raises:
            ObjectNotFoundError: 번역표가 아직 없을 때. 재시도 토픽으로 다시 처리한다(R-12).
            NonRetryableError: 번역표가 계약과 맞지 않거나 trackId가 없을 때. 다시 읽어도 같다.
        """
        cached = self._cache.get(job_id)
        if cached is not None:
            self._cache.move_to_end(job_id)
            if track_id in cached:
                return cached[track_id]
        # 메모리의 번역표가 오래됐을 수 있으므로 없는 trackId는 한 번 다시 읽고 판단한다.
        tracks = self._load(source_id, job_id)
        found = tracks.get(track_id)
        if found is None:
            raise NonRetryableError(f"track {track_id} is not in the mapping of job {job_id}")
        return found

    def _load(self, source_id: UUID, job_id: UUID) -> _Tracks:
        data = self._storage.get_bytes(mapping_key(source_id, job_id))
        try:
            mapping = TrackMapping.model_validate_json(data)
        except ValidationError as e:
            raise NonRetryableError(f"track mapping of job {job_id} breaks the contract") from e
        if (mapping.source_id, mapping.job_id) != (source_id, job_id):
            raise NonRetryableError(f"track mapping of job {job_id} belongs to another job")
        tracks = {t.track_id: t for t in mapping.tracks}
        self._cache[job_id] = tracks
        self._cache.move_to_end(job_id)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return tracks
