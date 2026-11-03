"""소비 그룹별 retry·DLQ 토픽 이름 규칙(docs overview "Kafka 토픽")."""


def retry_topic(topic: str, group: str) -> str:
    """`topic`을 소비하는 `group`의 재시도 토픽 이름."""
    return f"{topic}.{group}.retry"


def dlq_topic(group: str) -> str:
    """`group`이 끝내 처리하지 못한 메시지를 모으는 토픽 이름."""
    return f"{group}.dlq"
