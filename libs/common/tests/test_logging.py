import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime

import pytest
from vlm_common.logging import configure_logging


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    yield buffer
    logging.getLogger().handlers.clear()


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_writes_one_json_object_per_record(stream: io.StringIO) -> None:
    configure_logging(service="normalize", level="INFO", stream=stream)

    logging.getLogger("normalize.worker").info("video normalized")

    [record] = _lines(stream)
    assert record["message"] == "video normalized"
    assert record["level"] == "INFO"
    assert record["logger"] == "normalize.worker"
    assert record["service"] == "normalize"
    assert datetime.fromisoformat(str(record["timestamp"])).utcoffset() is not None


def test_extra_fields_are_top_level_keys(stream: io.StringIO) -> None:
    configure_logging(service="normalize", level="INFO", stream=stream)

    logging.getLogger("x").info("chunk done", extra={"sourceId": "s-1", "count": 3})

    [record] = _lines(stream)
    assert record["sourceId"] == "s-1"
    assert record["count"] == 3


def test_exception_is_serialized(stream: io.StringIO) -> None:
    configure_logging(service="normalize", level="INFO", stream=stream)

    try:
        raise RuntimeError("ffprobe failed")
    except RuntimeError:
        logging.getLogger("x").exception("probe error")

    [record] = _lines(stream)
    assert "RuntimeError: ffprobe failed" in str(record["exception"])


def test_records_below_level_are_dropped(stream: io.StringIO) -> None:
    configure_logging(service="normalize", level="WARNING", stream=stream)

    logging.getLogger("x").info("hidden")
    logging.getLogger("x").warning("shown")

    assert [r["message"] for r in _lines(stream)] == ["shown"]


def test_reconfiguring_does_not_duplicate_output(stream: io.StringIO) -> None:
    configure_logging(service="normalize", level="INFO", stream=stream)
    configure_logging(service="normalize", level="INFO", stream=stream)

    logging.getLogger("x").info("once")

    assert len(_lines(stream)) == 1
