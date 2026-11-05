"""SPEC-014 경계의 요청/종료 불변 조건."""

from uuid import uuid4

import pytest
from chunk_support import request
from pydantic import ValidationError
from vlm_contracts.events import ChunkRequested


@pytest.mark.parametrize("field", ["requestId", "normalizedKey", "ownedRange", "extra"])
def test_spec014_request_rejects_conflicting_or_unknown_input(field: str) -> None:
    value = request().model_dump(by_alias=True)
    if field == "requestId":
        value[field] = uuid4()
    elif field == "normalizedKey":
        value[field] = "sources/other/unsafe.mp4"
    elif field == "ownedRange":
        value[field] = {"startMs": 0, "endMs": 100000}
    else:
        value[field] = "unexpected"
    with pytest.raises(ValidationError):
        ChunkRequested.model_validate(value)
