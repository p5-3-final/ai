from collections.abc import Iterator

import pytest
from vlm_testing.containers import postgres


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """테스트 세션에서 함께 쓰는 PostgreSQL. 테스트는 각자 고유한 sourceId로 데이터를 나눈다."""
    with postgres() as url:
        yield url
