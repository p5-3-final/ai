"""번역표(SPEC-022 R-13, R-14)."""

from uuid import UUID

from stitcher_support import JOB, NORMALIZATION, SOURCE, mapping_errors, unit
from track_stitcher.boundary import BoundaryLink
from track_stitcher.ids import final_track_id
from track_stitcher.mapping import JobRef, build_mapping
from track_stitcher.model import LocalTrack, TrackRef
from track_stitcher.reid import Group, ReidLink
from vlm_contracts.tracks import TrackMapping

JOB_REF = JobRef(source_id=SOURCE, job_id=JOB, normalization_id=NORMALIZATION)


def _t(chunk: int, name: str, start: int, end: int) -> LocalTrack:
    return LocalTrack(TrackRef(UUID(int=chunk), UUID(int=100 + chunk), name), "person", start, end)


A0 = _t(0, "a", 100, 124_000)
B1 = _t(1, "b", 115_000, 200_000)
C1 = _t(1, "c", 130_000, 140_000)
D2 = _t(2, "d", 230_000, 260_000)
E2 = _t(2, "e", 300_000, 310_000)


def _mapping() -> TrackMapping:
    boundary = [BoundaryLink(A0, B1, 0.9)]
    earlier = Group.of([A0, B1], tuple(unit(1, 0, 0, 0)))
    later = Group.of([D2], tuple(unit(1, 0, 0, 0)))
    reid = [ReidLink(earlier, later, 0.95, D2.start_ms - earlier.end_ms)]
    return build_mapping(JOB_REF, "test-1", "m:a:b:c", [E2, D2, C1, B1, A0], boundary, reid)


def test_spec022_r13_every_local_track_appears_exactly_once() -> None:
    mapping = _mapping()

    members = [m.track_local_id for t in mapping.tracks for m in t.members]
    assert sorted(members) == ["a", "b", "c", "d", "e"]


def test_spec022_r13_links_record_kind_score_and_gap() -> None:
    mapping = _mapping()

    links = [
        (x.kind, x.from_.track_local_id, x.to.track_local_id, x.score, x.gap_ms)
        for x in mapping.links
    ]
    assert links == [("boundary", "a", "b", 0.9, None), ("reid", "b", "d", 0.95, 30_000)]


def test_spec022_r14_final_tracks_use_earliest_member_and_time_order() -> None:
    mapping = _mapping()

    [joined, alone_c, alone_e] = mapping.tracks
    assert [m.track_local_id for m in joined.members] == ["a", "b", "d"]
    assert joined.track_id == final_track_id(JOB, A0.ref)
    assert (joined.start_ms, joined.end_ms) == (100, 260_000)
    assert alone_c.track_id == final_track_id(JOB, C1.ref)
    assert [m.track_local_id for m in alone_e.members] == ["e"]


def test_spec022_r12_mapping_passes_contract_schema() -> None:
    mapping = _mapping()

    assert mapping_errors(mapping.model_dump_json(by_alias=True).encode()) == []
