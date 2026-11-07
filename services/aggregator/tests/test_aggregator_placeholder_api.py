"""analysis-api 자리 표시 서버가 실제 읽기 클라이언트·reader와 맞물리는지.

SPEC-027 D-15 B안, NFR-2 로컬 측정 준비.
"""

import time
from uuid import UUID

import httpx
import pytest
from agg_support import (
    JOB,
    PREVIOUS_SOURCE,
    SOURCE,
    ZONES,
    completed,
    job_event_json,
    person,
    risk_event,
    stay,
)
from aggregator.reader import EMPTY_INITIAL, JobReader
from vlm_analysis_client.client import AnalysisApiClient, AnalysisApiError
from vlm_common.http_call import Deadline
from vlm_contracts.events import SessionRef
from vlm_testing.analysis_api import Scenario, create_app

EVENTS = [job_event_json(risk_event(n, 10_000 * n, detected=n % 2 == 1)) for n in range(1, 6)]
TRACKS = [person(n, 0, 60_000) for n in range(1, 4)]
PRESENCES = [stay(n, ZONES[0].zone_id, 1_000, 9_000) for n in range(1, 4)]


def _scenario(final_states: dict[UUID, dict[str, object]] | None = None) -> Scenario:
    return Scenario.model_validate(
        {
            "events": EVENTS,
            "tracks": [
                {
                    "trackId": str(t.track_id),
                    "className": t.class_name,
                    "startMs": t.start_ms,
                    "endMs": t.end_ms,
                }
                for t in TRACKS
            ],
            "presences": [
                {
                    "trackId": str(p.track_id),
                    "zoneId": str(p.zone_id),
                    "enterMs": p.enter_ms,
                    "exitMs": p.exit_ms,
                }
                for p in PRESENCES
            ],
            "zones": [{"zoneId": str(z.zone_id), "name": z.name, "kind": z.kind} for z in ZONES],
            "finalStates": {str(k): v for k, v in (final_states or {}).items()},
        }
    )


def _client(scenario: Scenario) -> AnalysisApiClient:
    transport = httpx.ASGITransport(app=create_app(scenario))
    return AnalysisApiClient(
        "http://analysis-api", caller="aggregator", timeout_s=5, transport=transport
    )


def _deadline() -> Deadline:
    return Deadline.start(30, time.time)


async def test_reader_pages_through_all_scenario_items() -> None:
    reader = JobReader(_client(_scenario()), page_limit=2)

    source = await reader.read(
        completed(events=len(EVENTS), tracks=len(TRACKS)), deadline=_deadline(), traceparent=None
    )

    assert len(source.events) == len(EVENTS)
    assert len(source.tracks) == len(TRACKS)
    assert len(source.presences) == len(PRESENCES)
    assert {z.zone_id for z in source.zones} == {z.zone_id for z in ZONES}


async def test_unknown_previous_source_final_state_is_404() -> None:
    client = _client(_scenario())

    with pytest.raises(AnalysisApiError) as raised:
        await client.get_source_final_state(PREVIOUS_SOURCE, deadline=_deadline())

    assert raised.value.status == 404


async def test_previous_final_state_becomes_initial_state() -> None:
    risk = "0192f0d2-0000-7000-8000-0000000000cc"
    final: dict[str, object] = {
        "sourceId": str(PREVIOUS_SOURCE),
        "jobId": str(JOB),
        "zoneStates": [],
        "activeRisks": [
            {
                "riskId": risk,
                "zoneId": "global",
                "riskType": "fall",
                "reason": "쓰러짐",
                "firstSeenMs": 0,
            }
        ],
        "staticObjects": [],
    }
    reader = JobReader(_client(_scenario({PREVIOUS_SOURCE: final})), page_limit=1000)
    session = SessionRef(session_id=SOURCE, role="AFTER", previous_source_id=PREVIOUS_SOURCE)

    source = await reader.read(
        completed(events=len(EVENTS), tracks=len(TRACKS), session=session),
        deadline=_deadline(),
        traceparent=None,
    )

    assert source.initial_state != EMPTY_INITIAL
    assert [str(r.risk_id) for r in source.initial_state.active_risks] == [risk]
