"""JSON 구조화 로그 설정."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

# LogRecord가 기본으로 가진 속성. 이 밖의 속성은 `extra=`로 넘긴 값이라 그대로 내보낸다.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # 직렬화할 수 없는 extra 값 때문에 로그 한 줄을 잃지 않도록 문자열로 바꾼다.
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(service: str, level: str, stream: TextIO = sys.stdout) -> None:
    """루트 로거가 한 줄에 JSON 객체 하나씩 쓰도록 설정한다.

    다시 불러도 핸들러가 겹치지 않도록 기존 루트 핸들러를 바꾼다.

    Args:
        service: 모든 로그에 넣을 서비스 이름.
        level: 로그 수준 이름(`INFO`, `WARNING` 등).
        stream: 출력 대상. 컨테이너에서는 표준 출력이다.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter(service))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
