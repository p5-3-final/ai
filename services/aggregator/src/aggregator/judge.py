"""재고 변화·장비 방치 판정(SPEC-028 R-7~R-10, R-12).

변화 후보(`removed`·`moved`·`not_restored`)는 `state_compare`로, 통로·위험구역 장비 방치는
`event_judge`로 판정한다. 둘 다 job(실시간은 세션)당 호출 상한을 함께 센다(R-8). 판정 문장은 로그에
남기지 않는다.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, ImageRef
from vlm_contracts.aggregates import InventoryChange, InventoryEvent, InventoryEventType
from vlm_contracts.events import VLMAnalysis
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from aggregator import ids
from aggregator.equipment import Abandoned
from aggregator.inventory import Change, Comparison, change_order, judge_order
from aggregator.judge_budget import JudgeBudget
from aggregator.model import ZoneInfo
from aggregator.profile import AggregateProfile
from aggregator.summary import AnalyzeClient

STATE_COMPARE_TASK = "state_compare"
EVENT_JUDGE_TASK = "event_judge"
EQUIPMENT_EVENT: InventoryEventType = "equipment_abandoned"
# 장비 방치를 event_judge로 판정하는 구역 종류(R-12)
JUDGED_ZONE_KINDS = frozenset({"aisle", "hazard"})
_EVENT_OF: dict[str, InventoryEventType] = {
    "moved": "object_moved",
    "removed": "object_removed",
    "not_restored": "barrier_not_restored",
}
_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

logger = logging.getLogger(__name__)


class CallBudget(Protocol):
    """판정 호출 상한(R-8). 호출 하나를 세고 불러도 되면 True다."""

    async def try_acquire(self) -> bool: ...


class LocalBudget:
    """업로드 job의 프로세스 안 계수. job 하나를 한 번에 처리하므로 메시지마다 새로 만든다."""

    def __init__(self, limit: int) -> None:
        self._left = limit

    async def try_acquire(self) -> bool:
        if self._left <= 0:
            return False
        self._left -= 1
        return True


class SessionBudget:
    """실시간 세션의 Redis 계수(R-19). 위험구역 접근 재판정(SPEC-027 R-31)과 같은 계수다."""

    def __init__(self, budget: JudgeBudget, job_id: UUID, limit: int) -> None:
        self._budget = budget
        self._job_id = job_id
        self._limit = limit

    async def try_acquire(self) -> bool:
        return await self._budget.try_acquire(self._job_id, limit=self._limit)


@dataclass(frozen=True, slots=True)
class Judged:
    """판정 결과.

    Attributes:
        changes: 변화 후보 전체와 판정(후보 순서 그대로).
        events: 판정한 후보와 장비 방치의 재고 이벤트((startMs, eventId) 순).
        calls: 이번에 부른 ai-gateway 호출 수.
    """

    changes: tuple[InventoryChange, ...]
    events: tuple[InventoryEvent, ...]
    calls: int


class _ChangeNote(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="forbid", frozen=True)

    kind: str
    class_name: str
    description: str = Field(max_length=300)


class _StateCompareOutput(BaseModel):
    """`state_compare` 출력(계약 `_state-compare`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analysis: VLMAnalysis
    changes: list[_ChangeNote] = Field(max_length=20)


@dataclass(frozen=True, slots=True)
class _Outcome:
    analysis: VLMAnalysis
    call_id: UUID | None
    called: bool


@dataclass(frozen=True, slots=True)
class _ChangeResult:
    """변화 후보 하나의 판정. 상한에 걸리면 `record`·`event`가 None이다."""

    record: InventoryChange | None
    event: InventoryEvent | None
    calls: int


class InventoryJudge:
    """판정기.

    Args:
        gateway: ai-gateway(`state_compare`, `event_judge`).
        storage: 직전 영상 프레임을 이 job 폴더로 복사할 저장소(R-7, D-17 A).
        profile: 장비 방치 템플릿·기준값.
    """

    def __init__(
        self, gateway: AnalyzeClient, storage: ObjectStorage, profile: AggregateProfile
    ) -> None:
        self._gateway = gateway
        self._storage = storage
        self._profile = profile

    async def judge(
        self,
        comparison: Comparison,
        abandoned: Sequence[Abandoned],
        *,
        source_id: UUID,
        job_id: UUID,
        zones: Sequence[ZoneInfo],
        budget: CallBudget,
        traceparent: str | None,
        known_changes: Mapping[UUID, InventoryChange] | None = None,
        known_events: Mapping[UUID, InventoryEvent] | None = None,
    ) -> Judged:
        """후보를 직전 시각 순, 그다음 통로·위험구역 장비 방치를 (시작, objectId) 순으로 판정한다.

        Args:
            known_changes: 이미 판정한 후보(실시간 주기 계산, R-19). 판정 결과가 있으면 다시 부르지
                않고 그대로 쓴다.
            known_events: 이미 만든 재고 이벤트(eventId → 이벤트).
        """
        done_changes = known_changes or {}
        done_events = known_events or {}
        by_zone = {z.zone_id: z for z in zones}
        context = _Context(source_id, job_id, by_zone, budget, traceparent)
        judged: dict[UUID, InventoryChange] = {}
        events: dict[UUID, InventoryEvent] = {}
        calls = 0
        for change in sorted(comparison.changes, key=judge_order):
            if change.kind == "appeared":
                continue
            prior = done_changes.get(change.change_id)
            if prior is not None and prior.event_id is not None and prior.event_id in done_events:
                judged[change.change_id] = prior
                events[prior.event_id] = done_events[prior.event_id]
                continue
            result = await self._judge_change(change, context)
            calls += result.calls
            if result.record is not None and result.event is not None:
                judged[change.change_id] = result.record
                events[result.event.event_id] = result.event
        for item in abandoned:
            if item.event_id in done_events:
                events[item.event_id] = done_events[item.event_id]
                continue
            event, called = await self._judge_equipment(item, context)
            calls += called
            events[event.event_id] = event
        changes = tuple(
            judged.get(c.change_id) or c.to_contract()
            for c in sorted(comparison.changes, key=change_order)
        )
        ordered = tuple(sorted(events.values(), key=lambda e: (e.start_ms, e.event_id)))
        return Judged(changes, ordered, calls)

    async def _judge_change(self, change: Change, context: "_Context") -> _ChangeResult:
        if not await context.budget.try_acquire():
            return _ChangeResult(None, None, 0)
        event_type = _EVENT_OF[change.kind]
        outcome = await self._state_compare(change, event_type, context)
        event_id = ids.inventory_event_id(context.job_id, change.change_id)
        before, after = change.before_frame, change.after_frame
        event = InventoryEvent(
            event_id=event_id,
            event_type=event_type,
            start_ms=change.before_ms if change.before_ms is not None else change.after_ms or 0,
            end_ms=change.after_ms or 0,
            zone_id=change.zone_id,
            judgment_method="inventory+state_compare",
            evidence_frame_keys=[k for k in (before, after) if k is not None],
            analysis=outcome.analysis,
            call_id=outcome.call_id,
        )
        record = change.to_contract(
            event_id=event_id,
            call_id=outcome.call_id,
            status=outcome.analysis.verification_status,
        )
        return _ChangeResult(record, event, int(outcome.called))

    async def _state_compare(
        self, change: Change, event_type: InventoryEventType, context: "_Context"
    ) -> _Outcome:
        unknown = _Outcome(VLMAnalysis.unknown(event_type), None, False)
        if change.before_frame is None or change.after_frame is None:
            return unknown
        try:
            before = await asyncio.to_thread(
                self._frame_in_job, change.before_frame, change.change_id, context
            )
        except ObjectNotFoundError:
            # 직전 영상이 지워져 프레임이 없으면 판정할 수 없다(R-10과 같이 unknown).
            logger.warning("previous frame missing", extra={"changeId": str(change.change_id)})
            return unknown
        zone = context.zones.get(change.zone_id) if change.zone_id else None
        request = AnalyzeRequest(
            task_type=STATE_COMPARE_TASK,
            source_id=context.source_id,
            images=[ImageRef(storage_key=before), ImageRef(storage_key=change.after_frame)],
            variables={
                "eventType": event_type,
                "changeKind": change.kind,
                "expectedClasses": [change.class_name],
                "zoneName": zone.name if zone else None,
                "beforeSec": _sec(change.before_ms),
                "afterSec": _sec(change.after_ms),
            },
        )
        return await self._call(request, event_type, context, _state_analysis)

    async def _judge_equipment(
        self, item: Abandoned, context: "_Context"
    ) -> tuple[InventoryEvent, int]:
        zone = context.zones.get(item.zone_id)
        name = zone.name if zone else str(item.zone_id)
        if zone is None or zone.kind not in JUDGED_ZONE_KINDS:
            return self._equipment_event(item, "rule", self._template(item, name), None), 0
        if not await context.budget.try_acquire():
            # 상한을 넘으면 판정하지 않은 rule+vlm으로 두고 상태 계산에 넣지 않는다(plan 4절).
            unverified = VLMAnalysis.unknown(EQUIPMENT_EVENT).model_copy(
                update={"verification_status": "unverified"}
            )
            return self._equipment_event(item, "rule+vlm", unverified, None), 0
        stationary = round((item.end_ms - item.start_ms) / 1000, 3)
        request = AnalyzeRequest(
            task_type=EVENT_JUDGE_TASK,
            source_id=context.source_id,
            images=[ImageRef(storage_key=item.frame_key)],
            variables={
                "eventType": EQUIPMENT_EVENT,
                "startSec": item.start_ms / 1000,
                "endSec": item.end_ms / 1000,
                "zoneName": name,
                "evidence": {
                    "className": item.class_name,
                    "stationarySeconds": stationary,
                    "rule": EQUIPMENT_EVENT,
                },
            },
        )
        outcome = await self._call(request, EQUIPMENT_EVENT, context, _judge_analysis)
        event = self._equipment_event(item, "rule+vlm", outcome.analysis, outcome.call_id)
        return event, int(outcome.called)

    async def _call(
        self,
        request: AnalyzeRequest,
        event_type: InventoryEventType,
        context: "_Context",
        parse: "_Parser",
    ) -> _Outcome:
        try:
            response = await self._gateway.analyze(request, traceparent=context.traceparent)
        except AiGatewayError as e:
            # SPEC-021 R-9 재시도 뒤에도 실패했다. 판정하지 못한 것으로 둔다(R-10).
            logger.warning(
                "inventory judge failed",
                extra={"jobId": str(context.job_id), "task": request.task_type, "code": e.code},
            )
            return _Outcome(VLMAnalysis.unknown(event_type), None, True)
        try:
            analysis = parse(response.output)
        except ValidationError:
            logger.warning(
                "inventory judge output invalid",
                extra={"jobId": str(context.job_id), "task": request.task_type},
            )
            return _Outcome(VLMAnalysis.unknown(event_type), response.call_id, True)
        # 유형은 후보의 것이다. 모델이 다른 유형을 써도 이벤트 유형과 분석 유형을 맞춘다.
        return _Outcome(
            analysis.model_copy(update={"event_type": event_type}), response.call_id, True
        )

    def _frame_in_job(self, key: str, change_id: UUID, context: "_Context") -> str:
        """다른 영상의 프레임이면 이 job 폴더로 복사한 경로를 돌려준다(R-7, D-17 A).

        ai-gateway는 요청 sourceId 아래 블러 사본만 받는다. 사본은 이 영상과 함께 지워진다.
        """
        if key.startswith(f"sources/{context.source_id}/"):
            return key
        suffix = PurePosixPath(key).suffix.lower()
        target = (
            f"sources/{context.source_id}/jobs/{context.job_id}/aggregate/frames/blurred/"
            f"{change_id}-before{suffix}"
        )
        data = self._storage.get_bytes(key)
        self._storage.put_bytes(target, data, _CONTENT_TYPES.get(suffix, "image/webp"))
        return target

    def _template(self, item: Abandoned, zone_name: str) -> VLMAnalysis:
        """일반 구역 장비 방치의 템플릿 판정(R-12). 위험 여부는 판단하지 않는다."""
        text = self._profile.abandon_description.format(
            zone=zone_name, minutes=self._profile.abandon_ms // 60_000
        )
        return VLMAnalysis(
            event_type=EQUIPMENT_EVENT,
            event_detected=True,
            verification_status="confirmed",
            description=text,
            before_state=None,
            after_state=None,
            risk_detected=None,
            risk_type=None,
            risk_reason=None,
            solution=None,
            severity=None,
            evidence_start=item.start_ms / 1000,
            evidence_end=item.end_ms / 1000,
        )

    @staticmethod
    def _equipment_event(
        item: Abandoned, method: str, analysis: VLMAnalysis, call_id: UUID | None
    ) -> InventoryEvent:
        return InventoryEvent.model_validate(
            {
                "eventId": item.event_id,
                "eventType": EQUIPMENT_EVENT,
                "startMs": item.start_ms,
                "endMs": item.end_ms,
                "zoneId": item.zone_id,
                "judgmentMethod": method,
                "evidenceFrameKeys": [item.frame_key],
                "analysis": analysis,
                "callId": call_id,
            }
        )


@dataclass(frozen=True, slots=True)
class _Context:
    source_id: UUID
    job_id: UUID
    zones: Mapping[UUID, ZoneInfo]
    budget: CallBudget
    traceparent: str | None


class _Parser(Protocol):
    def __call__(self, output: object) -> VLMAnalysis: ...


def _state_analysis(output: object) -> VLMAnalysis:
    return _StateCompareOutput.model_validate(output).analysis


def _judge_analysis(output: object) -> VLMAnalysis:
    return VLMAnalysis.model_validate(output)


def _sec(ms: int | None) -> float | None:
    return None if ms is None else ms / 1000
