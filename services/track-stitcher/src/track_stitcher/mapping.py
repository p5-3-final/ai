"""번역표 만들기(SPEC-022 R-13, R-14). 입력만 받는 순수 함수다."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.tracks import MappedTrack, TrackLink, TrackMapping

from track_stitcher.boundary import BoundaryLink
from track_stitcher.ids import final_track_id
from track_stitcher.model import LocalTrack, connected
from track_stitcher.reid import ReidLink


@dataclass(frozen=True, slots=True)
class JobRef:
    """번역표가 속한 Job."""

    source_id: UUID
    job_id: UUID
    normalization_id: UUID


def build_mapping(
    job: JobRef,
    stitch_profile: str,
    embedding_model: str | None,
    tracks: Sequence[LocalTrack],
    boundary: Sequence[BoundaryLink],
    reid: Sequence[ReidLink],
) -> TrackMapping:
    """경계·Re-ID 연결을 함께 묶어 최종 트랙과 연결 근거를 만든다.

    받은 모든 로컬 트랙이 정확히 한 번 나온다(연결이 없으면 혼자 최종 트랙, R-13). 최종 트랙과
    구성원은 R-14 순서이고 trackId는 가장 이른 구성원으로 정한다.
    """
    edges = [(link.earlier.ref, link.later.ref) for link in boundary]
    edges += [(link.earlier.first.ref, link.later.first.ref) for link in reid]
    groups = connected(tracks, edges)
    mapped = [
        MappedTrack(
            track_id=final_track_id(job.job_id, members[0].ref),
            class_name=members[0].class_name,
            start_ms=members[0].start_ms,
            end_ms=max(m.end_ms for m in members),
            members=[m.ref.member() for m in members],
        )
        for members in groups
    ]
    return TrackMapping(
        source_id=job.source_id,
        job_id=job.job_id,
        normalization_id=job.normalization_id,
        stitch_profile=stitch_profile,
        embedding_model=embedding_model,
        tracks=mapped,
        links=[*_boundary_links(boundary), *_reid_links(reid)],
    )


def _sort_key(pair: tuple[LocalTrack, LocalTrack]) -> tuple[tuple[int, str, str], ...]:
    return pair[0].order_key(), pair[1].order_key()


def _boundary_links(links: Iterable[BoundaryLink]) -> list[TrackLink]:
    ordered = sorted(links, key=lambda x: _sort_key((x.earlier, x.later)))
    return [
        TrackLink(
            kind="boundary", from_=x.earlier.ref.member(), to=x.later.ref.member(), score=x.score
        )
        for x in ordered
    ]


def _reid_links(links: Iterable[ReidLink]) -> list[TrackLink]:
    # 근거의 from은 앞 트랙에서 가장 늦게 끝난 구성원, to는 뒤 트랙에서 가장 이른 구성원이다.
    ordered = sorted(links, key=lambda x: _sort_key((x.earlier.last, x.later.first)))
    return [
        TrackLink(
            kind="reid",
            from_=x.earlier.last.ref.member(),
            to=x.later.first.ref.member(),
            score=x.score,
            gap_ms=x.gap_ms,
        )
        for x in ordered
    ]
