import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from confluent_kafka import Message
from vlm_contracts.events import VideoDeleted
from vlm_messaging.publisher import KafkaPublisher, PublishError

pytestmark = pytest.mark.integration


def _deleted() -> VideoDeleted:
    return VideoDeleted(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        source_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
    )


def test_publishes_camel_case_json_keyed_by_source_id(
    bootstrap: str,
    unique: Callable[[str], str],
    create_topics: Callable[..., None],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    topic = unique("video.deleted")
    create_topics(topic)
    event = _deleted()
    publisher = KafkaPublisher(bootstrap=bootstrap, client_id="test")

    publisher.publish(topic, event, traceparent="00-abc-def-01")

    [msg] = read_all(topic, 5.0)
    assert msg.key() == str(event.source_id).encode()
    payload = json.loads(msg.value() or b"")
    assert payload["eventType"] == "video.deleted"
    assert payload["sourceId"] == str(event.source_id)
    assert ("traceparent", b"00-abc-def-01") in (msg.headers() or [])


def test_key_can_be_overridden(
    bootstrap: str,
    unique: Callable[[str], str],
    create_topics: Callable[..., None],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    topic = unique("chunk.requested")
    create_topics(topic)

    KafkaPublisher(bootstrap=bootstrap, client_id="test").publish(topic, _deleted(), key="chunk-7")

    [msg] = read_all(topic, 5.0)
    assert msg.key() == b"chunk-7"


def test_unreachable_broker_raises_publish_error() -> None:
    publisher = KafkaPublisher(bootstrap="127.0.0.1:1", client_id="test", timeout_s=2.0)

    with pytest.raises(PublishError):
        publisher.publish("video.deleted", _deleted())
