"""SPEC-028 NFR-1: 청크 60개·청크당 정적 객체 100개 job의 재고 이어 맞추기 시간."""

import statistics
import time

from agg_support import JOB, profile, uid
from aggregator.equipment import abandoned_equipment
from aggregator.inventory import compare
from inv_support import chunk_inventory, static
from vlm_contracts.aggregates import ChunkInventory

CHUNKS = 60
OBJECTS = 100
CHUNK_MS = 60_000
RUNS = 10
CLASSES = ("safety_barrier", "forklift", "ladder", "cone", "pallet")


def _chunks() -> list[ChunkInventory]:
    made = []
    for c in range(CHUNKS):
        chunk = uid(0x7000 + c)
        start = c * CHUNK_MS
        objects = []
        for o in range(OBJECTS):
            # 열마다 하나씩 청크에 따라 자리를 옮겨 제거·등장·이동 후보가 생기게 한다.
            moved = 0.05 if (o + c) % 17 == 0 else 0.0
            x, y = (o % 10) / 10 + moved, (o // 10) / 10
            objects.append(
                static(
                    f"o{o}",
                    chunk,
                    cls=CLASSES[o % len(CLASSES)],
                    box=(min(x, 0.92), y, 0.08, 0.08),
                    first=start,
                    last=start + CHUNK_MS - 1,
                )
            )
        made.append(chunk_inventory(chunk, start, start + CHUNK_MS, *objects))
    return made


def test_spec028_nfr1_inventory_compare_p95_under_2s() -> None:
    chunks, loaded = _chunks(), profile().profile
    timings: list[float] = []

    for _ in range(RUNS):
        started = time.perf_counter()
        result = compare(chunks, (), job_id=JOB, profile=loaded)
        abandoned_equipment(result.chains, (), (), loaded, job_id=JOB)
        timings.append(time.perf_counter() - started)

    assert result.changes
    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 <= 2.0, f"p95 {p95:.2f}s"
