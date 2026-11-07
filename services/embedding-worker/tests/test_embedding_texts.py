"""항목별 문장 규칙과 metadata(SPEC-025 R-1~R-5, R-7)."""

import json
from uuid import UUID

import pytest
from embedding_worker.texts import (
    Skip,
    TextItem,
    annotation_item,
    event_item,
    track_item,
    track_skip,
)
from ew_support import (
    ANNOTATION,
    ANNOTATION_TEXT,
    ATTRIBUTES,
    DESCRIPTION,
    JOB,
    OWNER,
    RISK_REASON,
    SEGMENT,
    SOLUTION,
    SOURCE,
    TRACK,
    TRACK_DESCRIPTION,
    event_analyzed,
    segment_annotated,
    track_analyzed,
)
from vlm_contracts.events import AttributeReason, TrackAnalyzed
from vlm_contracts.track_mapping import MappedTrackSummary

MAX = 2000
PLACE = MappedTrackSummary(track_id=TRACK, class_name="person", start_ms=101_000, end_ms=190_000)


def _item(result: TextItem | Skip) -> TextItem:
    assert isinstance(result, TextItem), result
    return result


def _metadata(item: TextItem) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(item.metadata.model_dump_json(by_alias=True))
    return loaded


# --- R-1 event ---------------------------------------------------------------------------------


def test_spec025_r1_event_text_joins_description_risk_reason_solution_in_order() -> None:
    message = event_analyzed()

    item = _item(event_item(message, MAX))

    assert item.text == f"{DESCRIPTION}\n{RISK_REASON}\n{SOLUTION}"
    assert (item.item_kind, item.item_id, item.text_format) == (
        "event",
        message.event_id,
        "event-v1",
    )
    assert (item.source_id, item.owner_id, item.job_id) == (
        message.source_id,
        message.owner_id,
        message.job_id,
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((DESCRIPTION, None, None), DESCRIPTION),
        ((None, RISK_REASON, SOLUTION), f"{RISK_REASON}\n{SOLUTION}"),
        ((DESCRIPTION, None, SOLUTION), f"{DESCRIPTION}\n{SOLUTION}"),
        ((None, None, SOLUTION), SOLUTION),
    ],
)
def test_spec025_r1_event_text_skips_null_parts(
    values: tuple[str | None, str | None, str | None], expected: str
) -> None:
    description, risk_reason, solution = values
    message = event_analyzed(description=description, risk_reason=risk_reason, solution=solution)

    assert _item(event_item(message, MAX)).text == expected


def test_spec025_r1_blank_parts_are_not_joined() -> None:
    message = event_analyzed(description="   ", risk_reason=None, solution=SOLUTION)

    assert _item(event_item(message, MAX)).text == SOLUTION


@pytest.mark.parametrize("blank", [None, "", "  \n "])
def test_spec025_r4_event_without_text_is_skipped(blank: str | None) -> None:
    message = event_analyzed(
        description=blank,
        risk_reason=None,
        solution=blank,
        severity=None,
        verification_status="unknown",
    )

    assert event_item(message, MAX) == Skip("event_no_text")


# --- R-2 track ---------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["event", "click"])
def test_spec025_r2_confirmed_track_description_is_the_text(reason: AttributeReason) -> None:
    message = track_analyzed(reason)

    assert track_skip(message) is None
    item = _item(track_item(message, PLACE, MAX))
    assert item.text == TRACK_DESCRIPTION
    assert (item.item_kind, item.item_id, item.text_format) == ("track", TRACK, "track-v1")
    assert (item.source_id, item.owner_id, item.job_id) == (SOURCE, OWNER, JOB)


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (track_analyzed("question"), "track_question"),
        (track_analyzed("click", outcome="FAILED"), "track_failed"),
        (
            track_analyzed("click", attributes={**ATTRIBUTES, "verificationStatus": "unverified"}),
            "track_not_confirmed",
        ),
        (
            track_analyzed("click", attributes={**ATTRIBUTES, "description": None}),
            "track_no_description",
        ),
        (
            track_analyzed("click", attributes={**ATTRIBUTES, "description": "  "}),
            "track_no_description",
        ),
    ],
)
def test_spec025_r4_track_outside_r2_is_skipped(message: TrackAnalyzed, reason: str) -> None:
    assert track_skip(message) == Skip(reason)


# --- R-3 segment_annotation --------------------------------------------------------------------


def test_spec025_r3_annotation_text_is_the_text() -> None:
    item = _item(annotation_item(segment_annotated(), MAX))

    assert item.text == ANNOTATION_TEXT
    assert (item.item_kind, item.item_id, item.text_format) == (
        "segment_annotation",
        ANNOTATION,
        "segment_annotation-v1",
    )


def test_spec025_r4_blank_annotation_is_skipped() -> None:
    assert annotation_item(segment_annotated(" \t "), MAX) == Skip("annotation_no_text")


# --- R-5 최대 글자 수 ---------------------------------------------------------------------------


def test_spec025_r5_long_event_text_is_cut_from_the_front() -> None:
    message = event_analyzed(description="가" * 1000, risk_reason="나" * 500, solution="다" * 500)

    text = _item(event_item(message, MAX)).text

    assert len(text) == MAX
    assert text == ("가" * 1000 + "\n" + "나" * 500 + "\n" + "다" * 500)[:MAX]


@pytest.mark.parametrize("limit", [1, 5, 17])
def test_spec025_r5_configured_limit_applies_to_every_kind(limit: int) -> None:
    items = [
        event_item(event_analyzed(), limit),
        track_item(track_analyzed(), PLACE, limit),
        annotation_item(segment_annotated(), limit),
    ]

    for item, full in zip(
        items,
        [f"{DESCRIPTION}\n{RISK_REASON}\n{SOLUTION}", TRACK_DESCRIPTION, ANNOTATION_TEXT],
        strict=True,
    ):
        assert _item(item).text == full[:limit]


# --- R-7 metadata -------------------------------------------------------------------------------


def test_spec025_r7_event_metadata_has_time_type_severity_status() -> None:
    message = event_analyzed()

    assert _metadata(_item(event_item(message, MAX))) == {
        "startMs": message.start_ms,
        "endMs": message.end_ms,
        "eventType": message.analysis.event_type,
        "severity": "high",
        "verificationStatus": "confirmed",
    }


def test_spec025_r7_event_metadata_omits_null_severity() -> None:
    message = event_analyzed(severity=None, verification_status="unverified")

    metadata = _metadata(_item(event_item(message, MAX)))

    assert "severity" not in metadata
    assert metadata["verificationStatus"] == "unverified"


def test_spec025_r7_track_metadata_has_mapping_time_class_and_attributes() -> None:
    assert _metadata(_item(track_item(track_analyzed(), PLACE, MAX))) == {
        "startMs": 101_000,
        "endMs": 190_000,
        "className": "person",
        "trackId": str(TRACK),
        "helmet": "not_worn",
        "vest": "worn",
        "role": "worker",
    }


def test_spec025_r7_track_metadata_omits_null_attributes() -> None:
    message = track_analyzed(attributes={**ATTRIBUTES, "helmet": None, "role": None})

    metadata = _metadata(_item(track_item(message, PLACE, MAX)))

    assert "helmet" not in metadata
    assert "role" not in metadata
    assert metadata["vest"] == "worn"


def test_spec025_r7_annotation_metadata_has_time_and_segment() -> None:
    message = segment_annotated()

    assert _metadata(_item(annotation_item(message, MAX))) == {
        "startMs": message.start_ms,
        "endMs": message.end_ms,
        "segmentId": str(SEGMENT),
    }


def test_track_item_uses_message_track_id() -> None:
    other = UUID("0192f100-0000-5000-8000-0000000000e9")
    place = MappedTrackSummary(track_id=other, class_name="forklift", start_ms=0, end_ms=1)

    item = _item(track_item(track_analyzed(track_id=other), place, MAX))

    assert item.item_id == other
