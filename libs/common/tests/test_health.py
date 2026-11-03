import http.client
from collections.abc import Iterator

import pytest
from vlm_common.health import HealthState, serve_health


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state(clock: FakeClock) -> HealthState:
    return HealthState(liveness_window_s=60.0, clock=clock)


@pytest.fixture
def port(state: HealthState) -> Iterator[int]:
    server = serve_health(state, host="127.0.0.1", port=0)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _status(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


def test_r22_health_is_ok_while_loop_beats(state: HealthState, clock: FakeClock, port: int) -> None:
    clock.now += 30
    state.beat()
    clock.now += 59

    assert _status(port, "/health") == 200


def test_r22_health_fails_when_loop_stalls(state: HealthState, clock: FakeClock, port: int) -> None:
    state.beat()
    clock.now += 61

    assert _status(port, "/health") == 503


def test_r22_health_is_ok_right_after_start(port: int) -> None:
    # 첫 poll 전에 liveness가 실패해 기동 중인 Pod를 죽이지 않게 한다.
    assert _status(port, "/health") == 200


def test_r23_ready_only_after_marked(state: HealthState, port: int) -> None:
    assert _status(port, "/ready") == 503

    state.mark_ready()

    assert _status(port, "/ready") == 200


def test_unknown_path_is_404(port: int) -> None:
    assert _status(port, "/metrics") == 404
