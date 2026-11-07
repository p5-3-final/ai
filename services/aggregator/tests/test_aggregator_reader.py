"""analysis-api 읽기와 개수 대조·초기 상태(SPEC-027 R-3~R-5, R-26)."""

import pytest
from agg_support import (
    HAZARD,
    PREVIOUS_SOURCE,
    SESSION_ID,
    FakeAnalysisApi,
    api_error,
    event,
    job_event_json,
    person,
    risk_event,
    stay,
    uid,
)
from aggregator.reader import InvalidInputError, JobReader, missing_counts
from vlm_analysis_client.models import FinalState
from vlm_common.http_call import Deadline
from vlm_contracts.events import AnalysisCompleted, AnalysisCounts, SessionRef

T0 = 1_762_000_000.0


def _message(
    *, events: int = 0, tracks: int = 0, session: SessionRef | None = None
) -> AnalysisCompleted:
    return AnalysisCompleted.model_validate(
        {
            "eventId": str(uid(77)),
            "occurredAt": "2026-11-06T10:00:00Z",
            "sourceId": "0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
            "ownerId": "0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d",
            "jobId": "0192f0d5-0000-7000-8000-000000000001",
            "jobType": "FULL",
            "normalizationId": "0192f0d4-0000-7000-8000-000000000001",
            "durationMs": 600_000,
            "session": session.model_dump(mode="json", by_alias=True) if session else None,
            "counts": AnalysisCounts(chunks=1, segments=1, events=events, tracks=tracks),
        }
    )


SESSION = SessionRef(session_id=SESSION_ID, role="AFTER", previous_source_id=PREVIOUS_SOURCE)


async def test_spec027_r3_reads_every_page_with_one_deadline() -> None:
    """[SPEC-027 R-3] 세 목록을 마지막 쪽까지 읽고 구역 목록을 읽는다. 모든 호출이 같은 마감이다."""
    api = FakeAnalysisApi(
        events=[job_event_json(event(n, start=n * 1_000)) for n in range(1, 6)],
        tracks=[person(n, 0, 10_000) for n in range(1, 4)],
        presences=[stay(1, HAZARD, 0, 1_000)],
    )
    deadline = Deadline.start(300, lambda: T0)

    read = await JobReader(api, page_limit=2).read(_message(), deadline=deadline)

    assert [e.event_id for e in read.events] == [uid(n) for n in range(1, 6)]
    assert len(read.tracks) == 3
    assert len(read.presences) == 1
    assert [z.kind for z in read.zones] == ["hazard", "aisle"]
    assert [c[0] for c in api.calls].count("events") == 3
    assert {at for _, at, _ in api.calls} == {deadline.at_ms}


def test_spec027_r4_only_rule_events_and_tracks_are_compared() -> None:
    """[SPEC-027 R-4] 판정 방식이 rule로 시작하는 이벤트 수와 트랙 수만 counts와 대조한다."""
    rule = event(1, start=0, method="rule+vlm")
    vector = event(2, start=0, method="vector+vlm")
    tracks = (person(1, 0, 1), person(2, 0, 1))

    assert (
        missing_counts(
            AnalysisCounts(chunks=1, segments=1, events=1, tracks=2), [rule, vector], tracks
        )
        is None
    )
    missing = missing_counts(
        AnalysisCounts(chunks=1, segments=1, events=3, tracks=3), [rule], tracks
    )
    assert missing is not None
    assert (missing.events, missing.tracks) == (2, 1)


async def test_spec027_r5_final_state_of_previous_source_is_the_initial_state() -> None:
    """[SPEC-027 R-5] 세션 직전 영상의 마지막 상태로 시작한다."""
    api = FakeAnalysisApi(
        final_state=FinalState.model_validate(
            {
                "sourceId": str(PREVIOUS_SOURCE),
                "jobId": str(uid(5)),
                "zoneStates": [{"zoneId": str(HAZARD), "state": "방호벽 설치됨"}],
                "activeRisks": [
                    {
                        "riskId": str(uid(900)),
                        "zoneId": str(HAZARD),
                        "riskType": "ppe_missing",
                        "reason": "안전모 미착용",
                        "firstSeenMs": 1_000,
                    }
                ],
            }
        )
    )

    read = await JobReader(api, page_limit=10).read(
        _message(session=SESSION), deadline=Deadline.start(300, lambda: T0)
    )

    assert read.initial_state.from_source_id == PREVIOUS_SOURCE
    assert [r.risk_id for r in read.initial_state.active_risks] == [uid(900)]
    assert ("final-state" in {c[0] for c in api.calls}) is True


@pytest.mark.parametrize("code", ["ANALYSIS_NO_ACTIVE_VERSION", "ANALYSIS_SOURCE_NOT_FOUND"])
async def test_spec027_r5_no_active_version_starts_empty(code: str) -> None:
    """[SPEC-027 R-5] 직전 영상 final-state가 404면 빈 초기 상태, fromSourceId null(D-16)."""
    api = FakeAnalysisApi(final_state=api_error(404, code))

    read = await JobReader(api, page_limit=10).read(
        _message(session=SESSION), deadline=Deadline.start(300, lambda: T0)
    )

    assert read.initial_state.from_source_id is None
    assert read.initial_state.active_risks == []


async def test_spec027_r5_no_session_does_not_read_final_state() -> None:
    api = FakeAnalysisApi()

    await JobReader(api, page_limit=10).read(_message(), deadline=Deadline.start(300, lambda: T0))

    assert "final-state" not in {c[0] for c in api.calls}


async def test_spec027_r23_analysis_that_is_not_vlm_analysis_is_invalid_input() -> None:
    """[SPEC-027 R-23] 이벤트의 analysis가 VLMAnalysis가 아니면 계약과 맞지 않는 응답이다."""
    broken = job_event_json(event(1, start=0))
    broken["analysis"] = {"eventType": "fall"}
    api = FakeAnalysisApi(events=[broken])

    with pytest.raises(InvalidInputError):
        await JobReader(api, page_limit=10).read(
            _message(), deadline=Deadline.start(300, lambda: T0)
        )


async def test_spec027_r26_pending_vector_events_are_merged_by_event_id() -> None:
    """[SPEC-027 R-26] 읽은 이벤트와 대기 집합의 벡터 이벤트를 eventId로 중복 제거해 합친다."""
    read_one = risk_event(1, 10_000, detected=True)
    api = FakeAnalysisApi(events=[job_event_json(read_one)])
    late = event(2, start=20_000, method="vector+vlm", chunk=None)
    duplicate = event(1, start=99_000, method="vector+vlm")

    read = await JobReader(api, page_limit=10).read(
        _message(), deadline=Deadline.start(300, lambda: T0), extra_events=[late, duplicate]
    )

    assert [(e.event_id, e.start_ms) for e in read.events] == [(uid(1), 5_000), (uid(2), 20_000)]
