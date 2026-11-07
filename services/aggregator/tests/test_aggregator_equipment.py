"""장비 방치 규칙(SPEC-028 R-11)."""

from uuid import UUID

from agg_support import AISLE, CHUNK_A, CHUNK_B, HAZARD, JOB, person, profile, stay, uid
from aggregator import ids
from aggregator.equipment import abandoned_equipment
from aggregator.inventory import Chain, compare
from inv_support import chunk_inventory, static
from vlm_contracts.aggregates import ChunkInventory

GENERAL = uid(0xF4)


def _chains(*chunks: ChunkInventory) -> tuple[Chain, ...]:
    return compare(chunks, (), job_id=JOB, profile=profile().profile).chains


def _forklift(zone: UUID | None = GENERAL, *, last_b: int = 720_000) -> tuple[Chain, ...]:
    return _chains(
        chunk_inventory(
            CHUNK_A, 0, 600_000, static("f1", CHUNK_A, cls="forklift", zone=zone, last=599_000)
        ),
        chunk_inventory(
            CHUNK_B,
            600_000,
            900_000,
            static("f1", CHUNK_B, cls="forklift", zone=zone, first=600_000, last=last_b),
        ),
    )


def test_spec028_r11_equipment_stationary_without_people_is_abandoned() -> None:
    """[SPEC-028 R-11] 장비 클래스가 사람 없이 기준(10분) 이상 머물면 장비 방치다."""
    [found] = abandoned_equipment(_forklift(), (), (), profile().profile, job_id=JOB)

    assert found.event_id == ids.equipment_event_id(JOB, ids.object_id(CHUNK_A, "f1"))
    assert (found.start_ms, found.end_ms, found.zone_id) == (0, 720_000, GENERAL)
    assert found.frame_key.endswith("static-f1.jpg")
    assert str(CHUNK_B) in found.frame_key


def test_spec028_r11_short_stay_or_other_class_is_not_abandoned() -> None:
    """[SPEC-028 R-11] 기준보다 짧거나 장비 클래스가 아니면 대상이 아니다."""
    short = _chains(
        chunk_inventory(
            CHUNK_A, 0, 600_000, static("f1", CHUNK_A, cls="forklift", zone=GENERAL, last=599_000)
        )
    )
    barrier = _chains(
        chunk_inventory(CHUNK_A, 0, 900_000, static("b1", CHUNK_A, zone=GENERAL, last=899_000))
    )

    assert abandoned_equipment(short, (), (), profile().profile, job_id=JOB) == ()
    assert abandoned_equipment(barrier, (), (), profile().profile, job_id=JOB) == ()


def test_spec028_r11_person_in_same_zone_or_no_zone_excludes() -> None:
    """[SPEC-028 R-11] 그동안 같은 구역에 사람 트랙이 있거나 구역이 없으면 대상이 아니다."""
    people = (person(10, 100_000, 200_000),)
    inside = (stay(10, AISLE, 100_000, 200_000),)
    elsewhere = (stay(10, HAZARD, 100_000, 200_000),)
    loaded = profile().profile

    assert abandoned_equipment(_forklift(AISLE), people, inside, loaded, job_id=JOB) == ()
    assert len(abandoned_equipment(_forklift(AISLE), people, elsewhere, loaded, job_id=JOB)) == 1
    assert abandoned_equipment(_forklift(None), (), (), loaded, job_id=JOB) == ()
