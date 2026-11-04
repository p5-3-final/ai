import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from vlm_contracts.events import VideoDeleted
from vlm_contracts.stages import StageTimingBatch
from vlm_messaging.publisher import Headers, KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink, LogTimingSink, MessagePosition, StageRecorder
from vlm_messaging.worker import KafkaWorker, WorkerConfig

T0 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
SOURCE = uuid.UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


class FakeClock:
    """단계마다 시각을 정해서 넘기는 시계. 단조 시계와 벽시계가 같이 움직인다."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def advance(self, seconds: float) -> None:
        self.seconds += seconds

    def monotonic(self) -> float:
        return self.seconds

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _event(produced_at: datetime | None = None) -> VideoDeleted:
    return VideoDeleted(
        event_id=uuid.uuid4(),
        occurred_at=T0,
        source_id=SOURCE,
        owner_id=uuid.uuid4(),
        produced_at=produced_at,
    )


def _recorder(
    clock: FakeClock,
    *,
    attempt: int = 1,
    received_at: datetime = T0,
    broker_appended_at: datetime | None = None,
) -> StageRecorder:
    return StageRecorder(
        service="normalize",
        attempt=attempt,
        received_at=received_at,
        broker_appended_at=broker_appended_at,
        trace_id=TRACE,
        wall=clock.wall,
        monotonic=clock.monotonic,
    )


@pytest.fixture
def handled(clock: FakeClock) -> StageRecorder:
    recorder = _recorder(clock)
    recorder.begin(_event())
    return recorder


def test_spec013_r3_handle_records_whole_handler_as_ok(
    handled: StageRecorder, clock: FakeClock
) -> None:
    with handled.handle():
        clock.advance(1.5)

    [timing] = handled.timings
    assert (timing.stage_id, timing.path, timing.status) == (
        "normalize.handle",
        "normalize.handle",
        "ok",
    )
    assert timing.duration_ms == 1500
    assert (timing.started_at, timing.ended_at) == (T0, T0 + timedelta(seconds=1.5))
    assert timing.subject.source_id == SOURCE
    assert (timing.attempt, timing.trace_id) == (1, TRACE)


def test_spec013_r3_handler_exception_records_error_and_propagates(
    handled: StageRecorder,
) -> None:
    with pytest.raises(RuntimeError, match="boom"), handled.handle():
        raise RuntimeError("boom")

    [timing] = handled.timings
    assert timing.status == "error"


def test_spec013_r4_stage_block_records_nested_path_and_count(
    handled: StageRecorder, clock: FakeClock
) -> None:
    with handled.handle():
        with handled.stage("normalize.probe"):
            clock.advance(0.2)
        with handled.stage("normalize.store", unit="files") as block:
            clock.advance(0.3)
            block.count = 13

    probe, store, handle = handled.timings
    assert (probe.stage_id, probe.path) == ("normalize.probe", "normalize.handle/normalize.probe")
    assert probe.duration_ms == pytest.approx(200)
    assert (store.path, store.count, store.unit) == (
        "normalize.handle/normalize.store",
        13,
        "files",
    )
    assert handle.stage_id == "normalize.handle"


def test_spec013_r4_failed_stage_is_error_and_exception_propagates(
    handled: StageRecorder,
) -> None:
    with (
        pytest.raises(ValueError, match="decode"),
        handled.handle(),
        handled.stage("normalize.transcode"),
    ):
        raise ValueError("decode")

    transcode, handle = handled.timings
    assert (transcode.status, handle.status) == ("error", "error")


def test_spec013_r1_queue_wait_prefers_broker_append_time(clock: FakeClock) -> None:
    appended = T0 - timedelta(milliseconds=300)
    recorder = _recorder(clock, broker_appended_at=appended)

    recorder.begin(_event(produced_at=T0 - timedelta(seconds=5)))

    [wait] = recorder.timings
    assert (wait.stage_id, wait.path, wait.status) == (
        "normalize.queue_wait",
        "normalize.queue_wait",
        "ok",
    )
    assert (wait.started_at, wait.ended_at) == (appended, T0)
    assert wait.duration_ms == pytest.approx(300)


def test_spec013_r1_queue_wait_falls_back_to_produced_at(clock: FakeClock) -> None:
    recorder = _recorder(clock)

    recorder.begin(_event(produced_at=T0 - timedelta(milliseconds=120)))

    [wait] = recorder.timings
    assert wait.duration_ms == pytest.approx(120)


def test_spec013_r1_negative_queue_wait_is_kept(clock: FakeClock) -> None:
    # 다른 Pod의 시계가 앞서 있으면 음수가 된다. 지표 쪽에서 거른다.
    recorder = _recorder(clock)

    recorder.begin(_event(produced_at=T0 + timedelta(milliseconds=40)))

    [wait] = recorder.timings
    assert wait.duration_ms == pytest.approx(-40)


def test_spec013_r2_no_queue_wait_without_any_send_time(clock: FakeClock) -> None:
    recorder = _recorder(clock)

    recorder.begin(_event(produced_at=None))

    assert recorder.timings == []


POSITION = MessagePosition(topic="video.uploaded", partition=3, offset=42)


def _run_once(clock: FakeClock, event: VideoDeleted, attempt: int = 1) -> StageTimingBatch | None:
    recorder = _recorder(clock, attempt=attempt)
    recorder.begin(event)
    with recorder.handle():
        for _ in range(2):
            with recorder.stage("normalize.probe"):
                clock.advance(0.1)
    return recorder.batch(service="normalize", pod="pod-1", position=POSITION)


def test_spec013_r7_reprocessing_gives_same_ids(clock: FakeClock) -> None:
    event = _event(produced_at=T0)

    first = _run_once(clock, event)
    again = _run_once(clock, event)

    assert first is not None
    assert again is not None
    assert first.batch_id == again.batch_id
    assert [t.timing_id for t in first.timings] == [t.timing_id for t in again.timings]
    # 같은 단계가 두 번 돌아도 기록 ID는 겹치지 않는다.
    assert len({t.timing_id for t in first.timings}) == len(first.timings)


def test_spec013_r7_next_attempt_gets_new_ids(clock: FakeClock) -> None:
    event = _event(produced_at=T0)

    first = _run_once(clock, event, attempt=1)
    retried = _run_once(clock, event, attempt=2)

    assert first is not None
    assert retried is not None
    assert first.batch_id != retried.batch_id
    assert not {t.timing_id for t in first.timings} & {t.timing_id for t in retried.timings}


def test_spec013_r5_batch_carries_service_pod_and_all_timings(clock: FakeClock) -> None:
    batch = _run_once(clock, _event(produced_at=T0))

    assert batch is not None
    assert (batch.service, batch.pod) == ("normalize", "pod-1")
    assert [t.stage_id for t in batch.timings] == [
        "normalize.queue_wait",
        "normalize.probe",
        "normalize.probe",
        "normalize.handle",
    ]


def test_spec013_r5_no_batch_without_timings(clock: FakeClock) -> None:
    recorder = _recorder(clock)

    assert recorder.batch(service="normalize", pod="pod-1", position=POSITION) is None


def test_spec013_nfr1_batch_has_no_owner(clock: FakeClock) -> None:
    event = _event(produced_at=T0)
    recorder = _recorder(clock)
    recorder.begin(event)
    with recorder.handle():
        pass

    batch = recorder.batch(service="normalize", pod="pod-1", position=POSITION)

    assert batch is not None
    assert str(event.owner_id) not in batch.model_dump_json(by_alias=True)


def test_spec013_nfr2_ten_blocks_cost_under_5ms_p95() -> None:
    event = _event(produced_at=T0)
    costs: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        recorder = StageRecorder(service="normalize", attempt=1, received_at=T0)
        recorder.begin(event)
        with recorder.handle():
            for _ in range(10):
                with recorder.stage("normalize.store", unit="files") as block:
                    block.count = 1
        batch = recorder.batch(service="normalize", pod="pod-1", position=POSITION)
        assert batch is not None
        batch.model_dump_json(by_alias=True)
        costs.append(time.perf_counter() - started)

    p95 = sorted(costs)[int(len(costs) * 0.95)]
    assert p95 < 0.005


@dataclass
class SentRaw:
    topic: str
    key: str | bytes | None
    value: bytes
    headers: list[tuple[str, bytes]]


@dataclass
class FakeRawPublisher:
    sent: list[SentRaw] = field(default_factory=list)

    def send_raw(self, topic: str, key: str | bytes | None, value: bytes, headers: Headers) -> None:
        self.sent.append(SentRaw(topic, key, value, list(headers)))


def test_spec013_r5_log_sink_writes_one_line_per_batch(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    batch = _run_once(clock, _event(produced_at=T0))
    assert batch is not None

    with caplog.at_level(logging.INFO, logger="vlm_messaging.timing"):
        LogTimingSink().emit(batch, traceparent=None)

    [record] = [r for r in caplog.records if r.getMessage() == "stage timing"]
    logged = StageTimingBatch.model_validate(record.__dict__["stageTiming"])
    assert logged == batch


def test_spec013_r5_kafka_sink_publishes_keyed_by_source(clock: FakeClock) -> None:
    batch = _run_once(clock, _event(produced_at=T0))
    assert batch is not None
    publisher = FakeRawPublisher()

    KafkaTimingSink(publisher).emit(batch, traceparent=f"00-{TRACE}-00f067aa0ba902b7-01")

    [sent] = publisher.sent
    assert (sent.topic, sent.key) == ("stage.timing", str(SOURCE))
    assert StageTimingBatch.model_validate_json(sent.value) == batch
    assert ("traceparent", f"00-{TRACE}-00f067aa0ba902b7-01".encode()) in sent.headers


def test_spec013_r10_worker_rejects_service_without_listed_stages() -> None:
    publisher = KafkaPublisher(bootstrap="127.0.0.1:1", client_id="test")

    with pytest.raises(ValueError, match="stage"):
        KafkaWorker(
            WorkerConfig(bootstrap="127.0.0.1:1", group_id="g", service="unlisted"), [], publisher
        )
