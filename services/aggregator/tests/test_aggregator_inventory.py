"""재고 이어 맞추기와 변화 후보(SPEC-028 R-3~R-6, R-13, R-17)."""

from agg_support import CHUNK_A, CHUNK_B, HAZARD, JOB, PREVIOUS_SOURCE, profile, uid
from aggregator import ids
from aggregator.inventory import Comparison, compare
from inv_support import chunk_inventory, previous_object, static
from vlm_contracts.aggregates import ChunkInventory, InventoryObject

CHUNK_C = uid(0x0C)
FAR = (0.7, 0.1, 0.2, 0.15)


def _compare(*chunks: ChunkInventory, previous: tuple[InventoryObject, ...] = ()) -> Comparison:
    return compare(chunks, previous, job_id=JOB, profile=profile().profile)


def _kinds(result: Comparison) -> list[tuple[str, str | None, str | None]]:
    return [(c.kind, c.before_object_id, c.after_object_id) for c in result.changes]


def test_spec028_r3_overlapping_same_class_objects_are_one_chain() -> None:
    """[SPEC-028 R-3] 이웃 청크의 같은 클래스·IoU 기준 이상 객체는 같은 사슬이고 변화가 없다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, last=59_000)),
        chunk_inventory(
            CHUNK_B,
            60_000,
            120_000,
            static("s9", CHUNK_B, box=(0.11, 0.5, 0.2, 0.15), first=60_000, last=110_000),
        ),
    )

    assert result.changes == ()
    [chain] = result.chains
    assert chain.object_id == ids.object_id(CHUNK_A, "s1")
    assert (chain.first_seen_ms, chain.last_seen_ms) == (0, 110_000)


def test_spec028_r3_different_class_or_low_iou_is_not_matched() -> None:
    """[SPEC-028 R-3] 클래스가 다르거나 IoU가 기준보다 낮으면 잇지 않는다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A)),
        chunk_inventory(
            CHUNK_B, 60_000, 120_000, static("s1", CHUNK_B, cls="forklift", first=60_000)
        ),
    )

    assert [c.kind for c in result.changes] == ["removed", "appeared"]


def test_spec028_r3_highest_iou_pair_wins() -> None:
    """[SPEC-028 R-3] 한 객체에 후보가 둘이면 IoU가 큰 쌍부터 하나씩 짝짓는다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A)),
        chunk_inventory(
            CHUNK_B,
            60_000,
            120_000,
            static("near", CHUNK_B, box=(0.12, 0.5, 0.2, 0.15)),
            static("same", CHUNK_B),
        ),
    )

    kept = ids.object_id(CHUNK_A, "s1")
    assert [(c.kind, c.after_object_id) for c in result.changes] == [
        ("appeared", ids.object_id(CHUNK_B, "near"))
    ]
    assert any(ch.object_id == kept and len(ch.sightings) == 2 for ch in result.chains)


def test_spec028_r4_unmatched_objects_become_removed_and_appeared() -> None:
    """[SPEC-028 R-4] 짝 없는 앞 객체는 removed(직전 = 마지막 관측), 뒤 객체는 appeared다.

    appeared의 이후 시각은 처음 관측이다.
    """
    loaded = profile().profile.model_copy(update={"move_window_ms": 0})
    result = compare(
        (
            chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, last=41_000)),
            chunk_inventory(CHUNK_B, 60_000, 120_000, static("s2", CHUNK_B, box=FAR, first=70_000)),
        ),
        (),
        job_id=JOB,
        profile=loaded,
    )

    removed, appeared = result.changes
    assert (removed.kind, removed.before_ms, removed.after_ms) == ("removed", 41_000, 60_000)
    assert removed.before_frame is not None
    assert removed.before_frame.endswith("static-s1.jpg")
    # 이후 시각(사라진 청크의 시작)에 가장 가까운 기준 프레임
    assert removed.after_frame is not None
    assert removed.after_frame.endswith("ref-60000.jpg")
    assert (appeared.kind, appeared.before_ms, appeared.after_ms) == ("appeared", None, 70_000)


def test_spec028_r5_removed_and_appeared_within_window_become_moved() -> None:
    """[SPEC-028 R-5] 같은 클래스 제거·등장이 이동 창(120초) 안이면 moved 하나로 합친다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, last=41_000)),
        chunk_inventory(CHUNK_B, 60_000, 120_000, static("s2", CHUNK_B, box=FAR, first=70_000)),
    )

    [moved] = result.changes
    assert _kinds(result) == [("moved", ids.object_id(CHUNK_A, "s1"), ids.object_id(CHUNK_B, "s2"))]
    assert (moved.before_ms, moved.after_ms) == (41_000, 70_000)
    assert moved.after_frame is not None
    assert moved.after_frame.endswith("static-s2.jpg")
    assert moved.change_id == ids.change_id(
        JOB, "moved", ids.object_id(CHUNK_A, "s1"), ids.object_id(CHUNK_B, "s2")
    )


def test_spec028_r5_outside_window_or_earlier_appearance_stays_apart() -> None:
    """[SPEC-028 R-5] 창 밖이거나 등장이 제거보다 이르면 합치지 않는다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, last=10_000)),
        chunk_inventory(CHUNK_B, 60_000, 200_000),
        chunk_inventory(CHUNK_C, 200_000, 300_000, static("s2", CHUNK_C, box=FAR, first=200_000)),
    )

    assert [c.kind for c in result.changes] == ["removed", "appeared"]


def test_spec028_r5_closest_pair_is_merged_first() -> None:
    """[SPEC-028 R-5] 후보가 여럿이면 시간이 가까운 쌍부터 하나씩 합친다."""
    result = _compare(
        chunk_inventory(
            CHUNK_A,
            0,
            60_000,
            static("s1", CHUNK_A, last=20_000),
            static("s2", CHUNK_A, box=(0.4, 0.4, 0.1, 0.1), last=50_000),
        ),
        chunk_inventory(CHUNK_B, 60_000, 120_000, static("n1", CHUNK_B, box=FAR, first=61_000)),
    )

    moved = [c for c in result.changes if c.kind == "moved"]
    assert [m.before_object_id for m in moved] == [ids.object_id(CHUNK_A, "s2")]
    assert [c.kind for c in result.changes] == ["removed", "moved"]


def test_spec028_r6_previous_barrier_missing_is_not_restored() -> None:
    """[SPEC-028 R-6] 직전 영상 끝 재고의 방호벽이 이번 영상에 없으면 not_restored다."""
    before = previous_object("prev-1", source=PREVIOUS_SOURCE, zone=HAZARD)
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000),
        chunk_inventory(CHUNK_B, 60_000, 120_000),
        previous=(before,),
    )

    [change] = result.changes
    assert (change.kind, change.before_object_id, change.after_object_id) == (
        "not_restored",
        "prev-1",
        None,
    )
    assert (change.before_ms, change.after_ms, change.zone_id) == (None, 0, HAZARD)
    assert change.before_frame == before.frame_key
    assert change.after_frame is not None
    assert change.after_frame.endswith("ref-0.jpg")


def test_spec028_r6_previous_object_still_there_keeps_its_id() -> None:
    """[SPEC-028 R-6] 직전 재고와 이어진 사슬은 직전 objectId를 그대로 쓴다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A)),
        previous=(previous_object("prev-1", source=PREVIOUS_SOURCE),),
    )

    assert result.changes == ()
    assert [o.object_id for o in result.end_inventory] == ["prev-1"]


def test_spec028_r6_non_barrier_or_moved_previous_is_not_not_restored() -> None:
    """[SPEC-028 R-6] 방호 클래스가 아니면 removed, 창 안에 다시 나타나면 moved다."""
    result = _compare(
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, box=FAR, first=5_000)),
        previous=(
            previous_object("prev-1", source=PREVIOUS_SOURCE),
            previous_object(
                "prev-2", source=PREVIOUS_SOURCE, cls="ladder", box=(0.4, 0.4, 0.1, 0.1)
            ),
        ),
    )

    assert sorted(c.kind for c in result.changes) == ["moved", "removed"]


def test_spec028_r6_appearance_in_first_chunk_needs_previous_inventory() -> None:
    """[SPEC-028 R-4·R-6] 직전 재고가 없으면 첫 청크 객체는 appeared가 아니다."""
    result = _compare(chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A)))

    assert result.changes == ()


def test_spec028_r13_end_inventory_is_the_last_chunk_chains() -> None:
    """[SPEC-028 R-13] 영상 끝 재고는 마지막 청크에 있는 사슬이다."""
    result = _compare(
        chunk_inventory(
            CHUNK_A, 0, 60_000, static("s1", CHUNK_A), static("gone", CHUNK_A, box=FAR)
        ),
        chunk_inventory(
            CHUNK_B, 60_000, 120_000, static("s1", CHUNK_B, first=60_000, last=119_000)
        ),
    )

    [kept] = result.end_inventory
    assert (kept.object_id, kept.first_seen_ms, kept.last_seen_ms) == (
        ids.object_id(CHUNK_A, "s1"),
        0,
        119_000,
    )
    assert kept.frame_key.endswith(
        f"{CHUNK_B}/executions/{uid(CHUNK_B.int + 0xE000)}/blurred/static-s1.jpg"
    )


def test_spec028_r13_no_chunks_means_empty_result() -> None:
    """[SPEC-028 R-13] 재고 파일이 없으면(청크 0) 빈 결과다."""
    result = _compare(previous=(previous_object("prev-1", source=PREVIOUS_SOURCE),))

    assert (result.changes, result.end_inventory, result.chains) == ((), (), ())


def test_spec028_r17_input_order_does_not_change_candidates() -> None:
    """[SPEC-028 R-17] 청크·객체 순서를 섞어도 후보·ID·순서가 같다."""
    chunks = [
        chunk_inventory(
            CHUNK_A,
            0,
            60_000,
            *(static(f"a{i}", CHUNK_A, box=(i / 10, 0.1, 0.05, 0.05)) for i in range(8)),
        ),
        chunk_inventory(
            CHUNK_B,
            60_000,
            120_000,
            *(static(f"b{i}", CHUNK_B, box=(i / 10, 0.1, 0.05, 0.05)) for i in range(0, 8, 2)),
        ),
        chunk_inventory(
            CHUNK_C,
            120_000,
            180_000,
            *(static(f"c{i}", CHUNK_C, box=(i / 10, 0.5, 0.05, 0.05)) for i in range(3)),
        ),
    ]
    expected = _compare(*chunks)
    shuffled = [
        c.model_copy(update={"static_objects": list(reversed(c.static_objects))})
        for c in reversed(chunks)
    ]

    assert _compare(*shuffled) == expected
    assert len(expected.changes) > 0
