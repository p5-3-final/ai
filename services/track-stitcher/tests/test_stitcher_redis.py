"""상태 저장소 의미(메모리·Redis 공통)와 Redis 고유 동작(SPEC-022 R-2, R-16, R-19, NFR-2, NFR-3).

같은 테스트를 두 구현에 돌려 단위 테스트가 쓰는 메모리 구현이 운영 Redis 구현과 같은 의미인지
확인한다. Redis 쪽은 testcontainer가 필요하다.
"""

import asyncio
import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from redis import Redis
from stitcher_support import (
    JOB,
    NORMALIZATION,
    OWNER,
    PROFILE,
    SOURCE,
    Clock,
    boundary_message,
    crossing_chunks,
    embedded_message,
    store_chunk,
    store_vectors,
    tracks_key,
    vector_file,
)
from track_stitcher.redis_state import KEY_TTL_S, RedisJobStateStore
from track_stitcher.service import StitcherService
from track_stitcher.state import (
    ChunkRecord,
    JobKey,
    JobMeta,
    JobStateStore,
    MemoryJobStateStore,
    VectorRecord,
)
from vlm_contracts.events import Event, TrackDetected, TrackManifest
from vlm_contracts.tracks import ChunkTracks
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage
from vlm_testing import containers

KEY = JobKey(SOURCE, JOB)
META = JobMeta(
    OWNER, NORMALIZATION, 600_000, "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
)
WAIT = 1_800_000


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with containers.redis() as url:
        yield url


_counter = iter(range(1, 1_000_000))


def _fresh_source() -> UUID:
    # 테스트끼리 Redis 키가 겹치지 않도록 영상마다 다른 ID를 쓴다.
    return UUID(int=SOURCE.int + next(_counter) * 0x10000)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.integration)])
def make_store(request: pytest.FixtureRequest) -> Callable[[], JobStateStore]:
    if request.param == "memory":
        shared = MemoryJobStateStore()
        return lambda: shared
    url: str = request.getfixturevalue("redis_url")
    return lambda: RedisJobStateStore(url)


def _record(file: ChunkTracks) -> ChunkRecord:
    return ChunkRecord(
        file.chunk_id,
        file.execution_id,
        file.owned_range,
        file.processing_range,
        tracks_key(file),
        file.detector_profile,
    )


def _files(source: UUID) -> list[ChunkTracks]:
    return crossing_chunks(source=source)


def test_spec022_r2_record_outcomes(make_store: Callable[[], JobStateStore]) -> None:
    store, source = make_store(), _fresh_source()
    key = JobKey(source, JOB)
    a, *_ = _files(source)
    [retried, *_] = crossing_chunks(source=source, attempt=1)

    first = store.record_chunk(key, META, _record(a), now_ms=1000, wait_ms=WAIT)
    again = store.record_chunk(key, META, _record(a), now_ms=2000, wait_ms=WAIT)
    other = store.record_chunk(key, META, _record(retried), now_ms=3000, wait_ms=WAIT)

    assert [first.outcome, again.outcome, other.outcome] == ["recorded", "duplicate", "conflict"]
    assert other.snapshot is not None
    assert [c.execution_id for c in other.snapshot.chunks] == [a.execution_id]
    assert other.snapshot.chunks[0] == _record(a)
    assert other.snapshot.meta == META
    # 마지막 입력은 새로 기록한 1000ms다(중복은 입력 시각을 바꾸지 않는다).
    assert key not in store.due(1000 + WAIT - 1, 1000)
    assert key in store.due(1000 + WAIT, 1000)


def test_spec022_r7_vectors_first_then_meta_completed(
    make_store: Callable[[], JobStateStore],
) -> None:
    store, source = make_store(), _fresh_source()
    key = JobKey(source, JOB)
    a, *_ = _files(source)
    vectors = VectorRecord(a.execution_id, "k/vectors.json", "a:b:c:d")

    store.record_vectors(
        key, replace(META, source_duration_ms=None), vectors, now_ms=1, wait_ms=WAIT
    )
    result = store.record_chunk(
        key, replace(META, source_duration_ms=7), _record(a), now_ms=2, wait_ms=WAIT
    )

    assert result.snapshot is not None
    assert result.snapshot.vectors == (vectors,)
    assert result.snapshot.meta.source_duration_ms == 7


def test_spec022_r16_claim_lease_and_late_input(make_store: Callable[[], JobStateStore]) -> None:
    store, source = make_store(), _fresh_source()
    key = JobKey(source, JOB)
    a, b, *_ = _files(source)
    store.record_chunk(key, META, _record(a), now_ms=1, wait_ms=WAIT)

    claimed = store.claim(key, now_ms=10, lease_ms=100)
    assert claimed is not None
    assert claimed.status == "finalizing"
    assert store.claim(key, now_ms=50, lease_ms=100) is None
    late = store.record_chunk(key, META, _record(b), now_ms=60, wait_ms=WAIT)
    assert (late.outcome, late.status) == ("late", "finalizing")
    # 임대 만료 시각에 다시 확정할 수 있도록 주기 확인 대상이 된다.
    assert key in store.due(110, 1000)
    assert store.claim(key, now_ms=111, lease_ms=100) is not None

    store.release(key, retry_at_ms=500)
    assert key not in store.due(499, 1000)
    assert key in store.due(500, 1000)
    assert store.claim(key, now_ms=600, lease_ms=100) is not None
    store.finish(key)
    assert store.claim(key, now_ms=10_000, lease_ms=100) is None
    # 같은 Redis를 쓰는 다른 테스트의 Job이 남아 있어 이 Job만 본다.
    assert key not in store.due(10**12, 1000)
    assert store.record_chunk(key, META, _record(b), now_ms=60, wait_ms=WAIT).outcome == "late"


def test_spec022_r16_only_one_concurrent_claim_wins(
    make_store: Callable[[], JobStateStore],
) -> None:
    source = _fresh_source()
    key = JobKey(source, JOB)
    a, *_ = _files(source)
    make_store().record_chunk(key, META, _record(a), now_ms=1, wait_ms=WAIT)
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        store = make_store()
        barrier.wait()
        wins.append(store.claim(key, now_ms=10, lease_ms=1000) is not None)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(wins) == [False] * 7 + [True]


def test_spec022_r19_delete_video_removes_jobs_and_blocks_input(
    make_store: Callable[[], JobStateStore],
) -> None:
    store, source = make_store(), _fresh_source()
    a, b, *_ = _files(source)
    other_job = JobKey(source, UUID(int=JOB.int + 1))
    store.record_chunk(JobKey(source, JOB), META, _record(a), now_ms=1, wait_ms=WAIT)
    store.record_chunk(other_job, META, _record(a), now_ms=1, wait_ms=WAIT)

    assert store.delete_video(source) == 2
    assert store.snapshot(JobKey(source, JOB)) is None
    assert store.snapshot(other_job) is None
    assert store.due(10**12, 100) == [] or all(
        k.source_id != source for k in store.due(10**12, 100)
    )
    after = store.record_chunk(JobKey(source, JOB), META, _record(b), now_ms=2, wait_ms=WAIT)
    assert after.outcome == "deleted"
    assert store.snapshot(JobKey(source, JOB)) is None


@pytest.mark.integration
def test_spec022_nfr3_keys_expire_after_seven_days(redis_url: str) -> None:
    store, source = RedisJobStateStore(redis_url), _fresh_source()
    key = JobKey(source, JOB)
    a, *_ = _files(source)
    store.record_chunk(key, META, _record(a), now_ms=1, wait_ms=WAIT)
    store.record_vectors(
        key, META, VectorRecord(a.execution_id, "k", "a:b:c:d"), now_ms=1, wait_ms=WAIT
    )
    client = Redis.from_url(redis_url, decode_responses=True)

    keys = client.keys(f"ts:{{{source}}}:*")

    assert len(keys) == 4  # meta, chunks, vectors, jobs
    assert KEY_TTL_S == 7 * 24 * 3600
    for k in keys:
        assert KEY_TTL_S - 5 <= client.ttl(k) <= KEY_TTL_S
    store.delete_video(source)
    assert client.ttl(f"ts:{{{source}}}:deleted") > KEY_TTL_S - 5


class _Publisher:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        with self._lock:
            self.events.append(event)


def _ctx(topic: str) -> MessageContext:
    recorder = StageRecorder(service=None, attempt=1, received_at=datetime.now(UTC))
    return MessageContext(topic, 0, 0, None, 1, None, recorder)


@pytest.mark.integration
async def test_spec022_r16_nfr2_two_instances_publish_one_manifest(redis_url: str) -> None:
    source = _fresh_source()
    files = _files(source)
    storage = InMemoryStorage()
    publisher = _Publisher()
    clock = Clock()
    pods = [
        StitcherService(
            RedisJobStateStore(redis_url), storage, publisher, PROFILE, lease_s=60, clock=clock
        )
        for _ in range(2)
    ]
    jobs = []
    for i, f in enumerate(files):
        vf = vector_file(f)
        store_chunk(storage, f)
        store_vectors(storage, vf)
        # 두 Pod가 메시지를 나눠 받고 동시에 처리한다.
        jobs.append(pods[i % 2].on_chunk_boundary(boundary_message(f), _ctx("chunk.boundary")))
        jobs.append(
            pods[(i + 1) % 2].on_segment_embedded(embedded_message(vf), _ctx("segment.embedded"))
        )

    await asyncio.gather(*jobs)

    manifests = [e for e in publisher.events if isinstance(e, TrackManifest)]
    assert len(manifests) == 1
    pairs = {
        (e.earlier_chunk_id, e.later_chunk_id)
        for e in publisher.events
        if isinstance(e, TrackDetected)
    }
    assert pairs == {(a.chunk_id, b.chunk_id) for a, b in itertools.pairwise(files)}
    mapping = storage.get_bytes(f"sources/{source}/jobs/{JOB}/tracks/mapping.json")

    # 한 Pod·메모리 상태로 같은 입력을 처리한 번역표와 같다.
    single_storage = InMemoryStorage()
    single = StitcherService(
        MemoryJobStateStore(), single_storage, _Publisher(), PROFILE, lease_s=60, clock=clock
    )
    for f in files:
        vf = vector_file(f)
        store_chunk(single_storage, f)
        store_vectors(single_storage, vf)
        await single.on_chunk_boundary(boundary_message(f), _ctx("chunk.boundary"))
        await single.on_segment_embedded(embedded_message(vf), _ctx("segment.embedded"))
    assert single_storage.get_bytes(f"sources/{source}/jobs/{JOB}/tracks/mapping.json") == mapping
