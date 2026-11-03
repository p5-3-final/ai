"""Kafka 워커용 health 서버(docs 서비스 카탈로그 "포트 규칙").

워커는 API를 두지 않으므로 kubelet 프로브만 받는 작은 HTTP 서버를 데몬 스레드로 띄운다.
"""

import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthState:
    """소비 루프의 생존 신호와 준비 상태.

    liveness는 루프가 최근 `liveness_window_s` 안에 돌았는지만 본다. Kafka 연결 여부를 넣으면
    브로커가 잠깐 끊길 때 모든 워커가 한꺼번에 재시작되므로 넣지 않는다.
    """

    def __init__(
        self, liveness_window_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._window = liveness_window_s
        self._clock = clock
        # 첫 poll 전에도 기동 중인 Pod가 죽지 않도록 만든 시각을 첫 신호로 친다.
        self._last_beat = clock()
        self._ready = threading.Event()

    def beat(self) -> None:
        """소비 루프가 한 바퀴 돌 때마다 부른다."""
        self._last_beat = self._clock()

    def mark_ready(self) -> None:
        """설정 검증·모델 로드·구독 시작이 끝나면 부른다."""
        self._ready.set()

    def is_alive(self) -> bool:
        return self._clock() - self._last_beat <= self._window

    def is_ready(self) -> bool:
        return self._ready.is_set()


def serve_health(state: HealthState, host: str = "", port: int = 8000) -> ThreadingHTTPServer:
    """`/health`, `/ready`에 응답하는 서버를 데몬 스레드로 시작하고 돌려준다.

    Args:
        state: 응답 근거가 되는 상태.
        host: 바인드할 주소. 빈 문자열은 모든 인터페이스다(kubelet이 Pod IP로 부른다).
        port: 포트. 0이면 빈 포트를 고른다(테스트용).
    """
    checks: dict[str, Callable[[], bool]] = {"/health": state.is_alive, "/ready": state.is_ready}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            check = checks.get(self.path)
            if check is None:
                status = HTTPStatus.NOT_FOUND
            else:
                status = HTTPStatus.OK if check() else HTTPStatus.SERVICE_UNAVAILABLE
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            # 프로브가 몇 초마다 부르므로 접근 로그를 남기지 않는다.
            return

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
