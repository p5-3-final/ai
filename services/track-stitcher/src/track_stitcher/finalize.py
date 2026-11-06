"""Job 확정: 파일 읽기 → 경계 연결 → Re-ID → 번역표 저장 → `track.manifest`(SPEC-022 R-8~R-15).

확정은 경계 연결을 처음부터 다시 계산한다. 쌍 이벤트 발행이 실패·중복돼도 번역표가 입력 파일만의
함수로 남아 같은 입력이면 같은 결과다(NFR-2).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from vlm_contracts.events import ReidSkippedReason, TrackManifest
from vlm_contracts.tracks import TrackMapping
from vlm_messaging.timing import StageRecorder
from vlm_storage.storage import ObjectStorage

from track_stitcher import files, ids
from track_stitcher.boundary import BoundaryLink, adjacent_pairs, link_pair
from track_stitcher.coverage import covers, missing_ranges
from track_stitcher.mapping import JobRef, build_mapping
from track_stitcher.model import LocalTrack, TrackRef, connected, local_tracks
from track_stitcher.profile import StitchProfile
from track_stitcher.publishing import EventPublisher
from track_stitcher.reid import Group, ReidLink, link_reappearances, unit_or_none
from track_stitcher.state import JobSnapshot

TRACK_MANIFEST_TOPIC = "track.manifest"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Reid:
    links: tuple[ReidLink, ...]
    embedding_model: str | None
    skipped_reason: ReidSkippedReason | None


class Finalizer:
    """확정 임대를 얻은 Job 하나를 끝낸다. 블로킹이다."""

    def __init__(
        self,
        storage: ObjectStorage,
        publisher: EventPublisher,
        profile: StitchProfile,
        now: Callable[[], datetime],
    ) -> None:
        self._storage = storage
        self._publisher = publisher
        self._profile = profile
        self._now = now

    def finalize(self, snapshot: JobSnapshot, timing: StageRecorder) -> TrackManifest | None:
        """번역표를 저장한 뒤 `track.manifest`를 발행한다(R-12). 청크가 하나도 없으면 None이다.

        저장이 실패하면 발행하지 않고 예외를 던진다. 저장소·발행 오류는 잡지 않는다(R-20).
        """
        duration = snapshot.meta.source_duration_ms
        if duration is None or not snapshot.chunks:
            return None
        with timing.stage("stitcher.finalize") as block:
            key = snapshot.key
            chunk_files = [
                files.read_recorded_tracks(self._storage, key, c) for c in snapshot.chunks
            ]
            tracks = [t for f in chunk_files for t in local_tracks(f)]
            boundary = [
                link
                for a, b in adjacent_pairs(chunk_files)
                for link in link_pair(a, b, self._profile.boundary).links
            ]
            reid = self._reid(snapshot, tracks, boundary, timing)
            job = JobRef(key.source_id, key.job_id, snapshot.meta.normalization_id)
            mapping = build_mapping(
                job, self._profile.version, reid.embedding_model, tracks, boundary, reid.links
            )
            mapping_key = f"sources/{key.source_id}/jobs/{key.job_id}/tracks/mapping.json"
            self._storage.put_bytes(
                mapping_key, mapping.model_dump_json(by_alias=True).encode(), "application/json"
            )
            owned = [f.owned_range for f in chunk_files]
            manifest = TrackManifest(
                event_id=ids.track_manifest_event_id(key.job_id),
                occurred_at=self._now(),
                source_id=key.source_id,
                owner_id=snapshot.meta.owner_id,
                job_id=key.job_id,
                normalization_id=snapshot.meta.normalization_id,
                complete=covers(owned, duration),
                missing_ranges=missing_ranges(owned, duration),
                chunk_count=len(chunk_files),
                local_track_count=len(tracks),
                track_count=len(mapping.tracks),
                boundary_link_count=len(boundary),
                reid_link_count=len(reid.links),
                reid_skipped_reason=reid.skipped_reason,
                mapping_key=mapping_key,
                stitch_profile=self._profile.version,
                embedding_model=reid.embedding_model,
            )
            self._publisher.publish(
                TRACK_MANIFEST_TOPIC,
                manifest,
                key=str(key.source_id),
                traceparent=snapshot.meta.traceparent,
            )
            block.count = len(mapping.tracks)
        _log_finalized(manifest, mapping)
        return manifest

    def _reid(
        self,
        snapshot: JobSnapshot,
        tracks: Sequence[LocalTrack],
        boundary: Sequence[BoundaryLink],
        timing: StageRecorder,
    ) -> _Reid:
        recorded = {c.execution_id: c for c in snapshot.chunks}
        vectors = [v for v in snapshot.vectors if v.execution_id in recorded]
        models = sorted({v.embedding_model for v in vectors})
        if len(models) > 1:
            # 다른 인코더의 벡터는 같은 공간이 아니라 유사도를 비교할 수 없다(R-11).
            return _Reid((), None, "embedding_model_mismatch")
        if not models:
            # 벡터가 하나도 없으면(대기 제한 시간으로 끝남) Re-ID 연결이 0개다. 계약은 이를 건너뛴
            # 이유로 보지 않는다(track.manifest reidSkippedReason 설명).
            return _Reid((), None, None)
        with timing.stage("stitcher.reid") as block:
            sums: dict[TrackRef, list[float]] = {}
            for record in vectors:
                chunk = recorded[record.execution_id]
                file = files.read_vectors(self._storage, snapshot.key, chunk, record)
                if file is None:
                    continue
                for local_id, total in files.crop_sums(file).items():
                    sums[TrackRef(chunk.chunk_id, chunk.execution_id, local_id)] = total
            edges = [(link.earlier.ref, link.later.ref) for link in boundary]
            groups = [Group.of(g, unit_or_none(_sum(g, sums))) for g in connected(tracks, edges)]
            result = link_reappearances(groups, self._profile.reid)
            block.count = result.compared
        return _Reid(result.links, models[0], None)


def _sum(members: Sequence[LocalTrack], sums: dict[TrackRef, list[float]]) -> list[float] | None:
    total: list[float] | None = None
    for member in members:
        part = sums.get(member.ref)
        if part is None:
            continue
        total = part if total is None else [a + b for a, b in zip(total, part, strict=True)]
    return total


def _log_finalized(manifest: TrackManifest, mapping: TrackMapping) -> None:
    # 번역표 내용은 남기지 않고 개수만 남긴다(NFR-4).
    logger.info(
        "track manifest published",
        extra={
            "sourceId": str(manifest.source_id),
            "jobId": str(manifest.job_id),
            "complete": manifest.complete,
            "chunkCount": manifest.chunk_count,
            "trackCount": len(mapping.tracks),
            "reidLinkCount": manifest.reid_link_count,
        },
    )
