import pytest

from vlm_messaging.retry import RetryPolicy
from vlm_messaging.topics import dlq_topic, retry_topic


def test_retry_and_dlq_topic_names_follow_group_convention() -> None:
    assert retry_topic("video.uploaded", "normalize") == "video.uploaded.normalize.retry"
    assert dlq_topic("normalize") == "normalize.dlq"


@pytest.mark.parametrize(("attempt", "expected"), [(2, 1.0), (3, 2.0), (4, 4.0), (5, 8.0)])
def test_delay_doubles_per_attempt_without_jitter(attempt: int, expected: float) -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=1.0, max_delay_s=300.0)

    # rand=1.0이면 지터가 줄이지 않은 상한값이다.
    assert policy.delay_before(attempt, rand=1.0) == expected


def test_delay_is_capped() -> None:
    policy = RetryPolicy(max_attempts=20, base_delay_s=1.0, max_delay_s=30.0)

    assert policy.delay_before(15, rand=1.0) == 30.0


def test_jitter_keeps_at_least_half_of_the_delay() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_s=4.0, max_delay_s=300.0)

    assert policy.delay_before(2, rand=0.0) == 2.0


def test_exhausted_after_max_attempts() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_s=1.0, max_delay_s=10.0)

    assert not policy.exhausted(2)
    assert policy.exhausted(3)


def test_policy_rejects_meaningless_values() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0, base_delay_s=1.0, max_delay_s=10.0)
