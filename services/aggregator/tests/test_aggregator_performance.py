"""SPEC-027 NFR-1: 큰 job 하나의 순수 계산 시간."""

import statistics
import time
from dataclasses import replace

from agg_support import (
    AISLE,
    CHUNK_A,
    CHUNK_B,
    HAZARD,
    event,
    inputs,
    person,
    profile,
    risk_event,
    stay,
)
from aggregator.compute import compute
from aggregator.model import AggregateInput, InputEvent, Presence, TrackSpan

# NFR-1의 입력 크기
EVENTS = 2_000
TRACKS = 5_000
PRESENCES = 20_000
DURATION_MS = 3_600_000
RUNS = 10


def _events() -> tuple[InputEvent, ...]:
    step = DURATION_MS // EVENTS
    made: list[InputEvent] = []
    for i in range(EVENTS):
        at = 10_000 + i * step
        if i % 10 == 0:
            # 위험 감지와 해소를 번갈아 넣어 상태 전이가 일어나게 한다
            made.append(risk_event(i + 1, at, detected=(i // 10) % 2 == 0))
        else:
            chunk = CHUNK_A if i % 2 == 0 else CHUNK_B
            made.append(
                event(i + 1, start=at, chunk=chunk, track=person(i % TRACKS, 0, 1).track_id)
            )
    return tuple(made)


def _tracks() -> tuple[TrackSpan, ...]:
    span = DURATION_MS // TRACKS
    return tuple(person(n, n * span, n * span + 120_000) for n in range(TRACKS))


def _presences(tracks: tuple[TrackSpan, ...]) -> tuple[Presence, ...]:
    per_track = PRESENCES // TRACKS
    made: list[Presence] = []
    for track in tracks:
        for k in range(per_track):
            enter = track.start_ms + k * 25_000
            made.append(stay(0, HAZARD if k % 2 == 0 else AISLE, enter, enter + 20_000))
            made[-1] = replace(made[-1], track_id=track.track_id)
    return tuple(made)


def _source() -> AggregateInput:
    tracks = _tracks()
    return inputs(*_events(), tracks=tracks, presences=_presences(tracks), duration_ms=DURATION_MS)


def test_spec027_nfr1_compute_p95_under_5s() -> None:
    source, loaded = _source(), profile()
    assert (len(source.events), len(source.tracks), len(source.presences)) == (
        EVENTS,
        TRACKS,
        PRESENCES,
    )
    timings: list[float] = []

    for _ in range(RUNS):
        started = time.perf_counter()
        computed = compute(source, loaded)
        timings.append(time.perf_counter() - started)

    assert computed.risks
    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 <= 5.0, f"p95 {p95:.2f}s"
