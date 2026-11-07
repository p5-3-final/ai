"""analysis-api 내부 읽기 API로 job 입력을 모은다(R-3~R-5, R-26).

모든 호출은 같은 마감(핸들러 시작 + 설정한 제한 시간)을 싣는다. 응답은 생성 모델로 검증된 뒤 계산용
값 객체로 바꾼다. 이벤트의 `analysis`는 계약에서 자유 객체라 `VLMAnalysis`로 다시 검증한다.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError
from vlm_analysis_client.client import (
    AnalysisApiError,
    EventPage,
    PresencePage,
    TrackPage,
    ZoneList,
)
from vlm_analysis_client.models import FinalState, JobEvent
from vlm_common.errors import VlmError
from vlm_common.http_call import Deadline
from vlm_contracts.aggregates import InitialState, InventoryObject
from vlm_contracts.events import AnalysisCompleted, AnalysisCounts, MissingCounts, VLMAnalysis

from aggregator.model import AggregateInput, InputEvent, Presence, TrackSpan, ZoneInfo

EMPTY_INITIAL = InitialState(from_source_id=None, zone_states=[], active_risks=[])


class InvalidInputError(VlmError):
    """읽은 응답이 계약과 맞지 않는다. 다시 읽어도 같아 DLQ로 보낸다(R-23)."""

    code = "AGGREGATOR_INVALID_INPUT"


class AnalysisReadClient(Protocol):
    """analysis-api 내부 읽기. 운영은 `vlm_analysis_client.client.AnalysisApiClient`다."""

    async def list_job_events(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> EventPage: ...

    async def list_job_tracks(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> TrackPage: ...

    async def list_job_zone_presences(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> PresencePage: ...

    async def list_source_zones(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> ZoneList: ...

    async def get_source_final_state(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> FinalState: ...


class _Page[T](Protocol):
    @property
    def items(self) -> Sequence[T]: ...

    @property
    def next_cursor(self) -> str | None: ...


class JobReader:
    """job 하나의 입력을 읽는다.

    Args:
        client: analysis-api 읽기 클라이언트.
        page_limit: 쪽 크기(`limit`, 계약 상한 1000).
    """

    def __init__(self, client: AnalysisReadClient, *, page_limit: int) -> None:
        self._client = client
        self._limit = page_limit

    async def read(
        self,
        message: AnalysisCompleted,
        *,
        deadline: Deadline,
        traceparent: str | None = None,
        extra_events: Sequence[InputEvent] = (),
    ) -> AggregateInput:
        """이벤트·트랙·구역 머문 구간을 마지막 쪽까지, 구역 목록과 (세션이면) 직전 영상의 마지막
        상태를 읽는다.

        Args:
            extra_events: 대기 집합의 늦은 벡터 이벤트(R-26). 읽은 이벤트와 eventId가 같으면 읽은
                쪽을 쓴다.

        Raises:
            AnalysisApiError: 읽기 실패. 404 `final-state`는 빈 초기 상태로 바꾼다(R-5, D-16).
            InvalidInputError: 이벤트의 `analysis`가 `VLMAnalysis`가 아니다(R-23).
        """
        source, job = message.source_id, message.job_id

        def call[P: BaseModel](
            fetch: Callable[..., Awaitable[P]],
        ) -> Callable[[str | None], Awaitable[P]]:
            async def one(cursor: str | None) -> P:
                return await fetch(
                    source,
                    job,
                    deadline=deadline,
                    cursor=cursor,
                    limit=self._limit,
                    traceparent=traceparent,
                )

            return one

        raw_events = await _all(call(self._client.list_job_events))
        raw_tracks = await _all(call(self._client.list_job_tracks))
        raw_presences = await _all(call(self._client.list_job_zone_presences))
        zones = await self._client.list_source_zones(
            source, deadline=deadline, traceparent=traceparent
        )
        initial, previous_inventory = await self._initial(message, deadline, traceparent)
        events = {e.event_id: e for e in extra_events}
        events.update((e.event_id, _event(e)) for e in raw_events)
        return AggregateInput(
            source_id=source,
            owner_id=message.owner_id,
            job_id=job,
            normalization_id=message.normalization_id,
            duration_ms=message.duration_ms,
            session=message.session,
            initial_state=initial,
            events=tuple(sorted(events.values(), key=lambda e: (e.start_ms, e.event_id))),
            tracks=tuple(
                TrackSpan(t.track_id, t.class_name, t.start_ms, t.end_ms) for t in raw_tracks
            ),
            presences=tuple(
                Presence(p.track_id, p.zone_id, p.enter_ms, p.exit_ms) for p in raw_presences
            ),
            zones=tuple(ZoneInfo(z.zone_id, z.name, z.kind) for z in zones.items),
            previous_inventory=previous_inventory,
        )

    async def _initial(
        self, message: AnalysisCompleted, deadline: Deadline, traceparent: str | None
    ) -> tuple[InitialState, tuple[InventoryObject, ...]]:
        """직전 영상의 마지막 상태와 끝 재고(SPEC-028 R-6). 재고가 없으면 빈 목록이다."""
        previous = message.session.previous_source_id if message.session else None
        if previous is None:
            return EMPTY_INITIAL, ()
        try:
            final = await self._client.get_source_final_state(
                previous, deadline=deadline, traceparent=traceparent
            )
        except AnalysisApiError as e:
            # 활성 분석 버전이 없거나(R-5) 직전 영상이 지워졌으면(D-16) 다시 읽어도 같다.
            if e.status == 404:
                return EMPTY_INITIAL, ()
            raise
        initial = InitialState.model_validate(
            {
                "fromSourceId": previous,
                "zoneStates": [z.model_dump(mode="json", by_alias=True) for z in final.zone_states],
                "activeRisks": [
                    r.model_dump(mode="json", by_alias=True) for r in final.active_risks
                ],
            }
        )
        try:
            stocked = tuple(
                InventoryObject.model_validate(o.model_dump(mode="json", by_alias=True))
                for o in final.static_objects or ()
            )
        except ValidationError as e:
            raise InvalidInputError("final-state staticObjects is not an inventory") from e
        return initial, stocked


async def _all[T](fetch: Callable[[str | None], Awaitable[_Page[T]]]) -> list[T]:
    items: list[T] = []
    cursor: str | None = None
    while True:
        page = await fetch(cursor)
        items.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            return items


def _event(item: JobEvent) -> InputEvent:
    try:
        analysis = VLMAnalysis.model_validate(item.analysis)
    except ValidationError as e:
        raise InvalidInputError(f"event {item.event_id} analysis is not VLMAnalysis") from e
    return InputEvent(
        event_id=item.event_id,
        chunk_id=item.chunk_id,
        event_type=item.event_type,
        start_ms=item.start_ms,
        end_ms=item.end_ms,
        track_id=item.track_id,
        zone_id=item.zone_id,
        zone_name=item.zone_name,
        judgment_method=item.judgment_method,
        score=item.score,
        analysis=analysis,
    )


def missing_counts(
    counts: AnalysisCounts, events: Sequence[InputEvent], tracks: Sequence[TrackSpan]
) -> MissingCounts | None:
    """판정 방식 `rule*` 이벤트 수와 트랙 수를 `counts`와 대조한다(R-4, v5.1).

    벡터 이벤트는 완료 뒤에도 올 수 있어 세지 않는다. 같으면 None, 다르면 빠진 수(0 이상)다.
    """
    rules = sum(1 for e in events if e.judgment_method.startswith("rule"))
    if rules == counts.events and len(tracks) == counts.tracks:
        return None
    return MissingCounts(
        events=max(0, counts.events - rules), tracks=max(0, counts.tracks - len(tracks))
    )


def with_events(source: AggregateInput, events: Sequence[InputEvent]) -> AggregateInput:
    """같은 입력에 이벤트만 바꾼 값(늦은 벡터 이벤트를 더해 다시 계산할 때, R-27)."""
    return replace(source, events=tuple(sorted(events, key=lambda e: (e.start_ms, e.event_id))))
