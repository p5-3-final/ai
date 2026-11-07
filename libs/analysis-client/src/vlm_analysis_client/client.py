"""analysis-api 내부 읽기 클라이언트(계약 `openapi/analysis-service.yaml`, SPEC-027 R-3).

요청·응답 모델은 계약에서 생성한 `vlm_analysis_client.models`만 쓴다. 모든 호출이 읽기 전용이라
503과 연결 실패만 다시 시도한다(`READ_ONLY`, SPEC-021 R-10). 마감은 호출자가 정해 넘긴다.
aggregator는 job 하나의 모든 호출에 같은 마감을 싣는다(SPEC-027 R-3).
"""

import asyncio
import time
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError
from vlm_common.http_call import (
    READ_ONLY,
    CallFailedError,
    CallRetry,
    Clock,
    Deadline,
    Failure,
    Sleep,
    call_with_retry,
    failure_of,
)

from vlm_analysis_client.models import (
    FinalState,
    InternalV1SourcesSourceIdJobsJobIdEventsGetResponse,
    InternalV1SourcesSourceIdJobsJobIdTracksGetResponse,
    InternalV1SourcesSourceIdJobsJobIdZonePresencesGetResponse,
    InternalV1SourcesSourceIdZonesGetResponse,
    ProblemDetail,
)

__all__ = [
    "AnalysisApiClient",
    "AnalysisApiError",
    "EventPage",
    "PresencePage",
    "TrackPage",
    "ZoneList",
]

EventPage = InternalV1SourcesSourceIdJobsJobIdEventsGetResponse
TrackPage = InternalV1SourcesSourceIdJobsJobIdTracksGetResponse
PresencePage = InternalV1SourcesSourceIdJobsJobIdZonePresencesGetResponse
ZoneList = InternalV1SourcesSourceIdZonesGetResponse


class AnalysisApiError(CallFailedError):
    """analysis-api 호출이 결과 없이 끝났다.

    Attributes:
        code: ProblemDetail의 `code`. 응답에 없으면 `ANALYSIS_HTTP_<상태>`, 응답 대기 시간 초과는
            `ANALYSIS_TIMEOUT`, 연결 실패는 `ANALYSIS_CONNECTION_FAILED`, 계약과 맞지 않는 응답은
            `ANALYSIS_INVALID_RESPONSE`다.
        status: HTTP 상태. 응답을 받지 못했으면 None이다.
        retryable: 다시 시도할 수 있는 실패(503·연결 실패)였는지. True면 재시도를 다 쓰거나 마감에
            닿아 끝난 것이다.
    """

    def __init__(
        self, code: str, message: str, *, status: int | None, retryable: bool, failure: Failure
    ) -> None:
        super().__init__(failure, message)
        self.code = code
        self.status = status
        self.retryable = retryable


class AnalysisApiClient:
    """analysis-api 내부 읽기 클라이언트. `async with`로 연결을 닫는다.

    Args:
        base_url: analysis-api 내부 주소(예: `http://analysis-api:8085`).
        caller: `X-Caller` 헤더 값. analysis-api가 허용 호출자 목록과 비교한다.
        timeout_s: 시도 하나의 제한 시간 상한. 실제 값은 마감까지 남은 시간과 비교해 짧은 쪽이다.
        retry: 재시도 횟수와 지수 대기.
        transport: 테스트가 바꿔 넣는 전송 계층.
        sleep: 재시도 전 대기 함수. 테스트가 바꿔 넣는다.
        clock: epoch 초 시계. 테스트가 바꿔 넣는다.
    """

    def __init__(
        self,
        base_url: str,
        *,
        caller: str,
        timeout_s: float,
        retry: CallRetry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
    ) -> None:
        self._timeout_s = timeout_s
        self._retry = retry or CallRetry()
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Caller": caller},
            timeout=timeout_s,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_job_events(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> EventPage:
        """job의 판정 이벤트 한 쪽(`listJobEvents`). `nextCursor`가 null이면 마지막 쪽이다.

        Raises:
            AnalysisApiError: 오류 응답, 재시도 끝의 503·연결 실패, 시간 초과, 계약과 맞지 않는
                응답.
        """
        path = f"/internal/v1/sources/{source_id}/jobs/{job_id}/events"
        return await self._get(path, EventPage, deadline, _page(cursor, limit), traceparent)

    async def list_job_tracks(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> TrackPage:
        """job의 최종 트랙 한 쪽(`listJobTracks`). 오류는 `list_job_events`와 같다."""
        path = f"/internal/v1/sources/{source_id}/jobs/{job_id}/tracks"
        return await self._get(path, TrackPage, deadline, _page(cursor, limit), traceparent)

    async def list_job_zone_presences(
        self,
        source_id: UUID,
        job_id: UUID,
        *,
        deadline: Deadline,
        cursor: str | None = None,
        limit: int | None = None,
        traceparent: str | None = None,
    ) -> PresencePage:
        """트랙별 구역 머문 구간 한 쪽(`listJobZonePresences`). 오류는 `list_job_events`와 같다."""
        path = f"/internal/v1/sources/{source_id}/jobs/{job_id}/zone-presences"
        return await self._get(path, PresencePage, deadline, _page(cursor, limit), traceparent)

    async def list_source_zones(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> ZoneList:
        """영상의 구역 정의(`listSourceZones`). 오류는 `list_job_events`와 같다."""
        path = f"/internal/v1/sources/{source_id}/zones"
        return await self._get(path, ZoneList, deadline, {}, traceparent)

    async def get_source_final_state(
        self, source_id: UUID, *, deadline: Deadline, traceparent: str | None = None
    ) -> FinalState:
        """영상 활성 분석 버전의 마지막 상태(`getSourceFinalState`).

        Raises:
            AnalysisApiError: `list_job_events`와 같다. 활성 버전이 없으면 404
                `ANALYSIS_NO_ACTIVE_VERSION`이다.
        """
        path = f"/internal/v1/sources/{source_id}/final-state"
        return await self._get(path, FinalState, deadline, {}, traceparent)

    async def _get[M: BaseModel](
        self,
        path: str,
        model: type[M],
        deadline: Deadline,
        params: dict[str, str],
        traceparent: str | None,
    ) -> M:
        headers = deadline.header()
        if traceparent:
            headers["traceparent"] = traceparent

        async def attempt(timeout_s: float) -> M:
            return await self._attempt(path, params, headers, timeout_s, model)

        return await call_with_retry(
            attempt,
            deadline=deadline,
            timeout_s=self._timeout_s,
            rule=READ_ONLY,
            retry=self._retry,
            sleep=self._sleep,
            clock=self._clock,
        )

    async def _attempt[M: BaseModel](
        self,
        path: str,
        params: dict[str, str],
        headers: dict[str, str],
        timeout_s: float,
        model: type[M],
    ) -> M:
        try:
            # 마감이 이미 지났으면 0 이하라 httpx가 바로 시간 초과로 끝낸다.
            response = await self._http.get(
                path, params=params, headers=headers, timeout=max(timeout_s, 0.001)
            )
        except httpx.TransportError as e:
            failure = failure_of(e)
            code = "ANALYSIS_TIMEOUT" if failure.kind == "timeout" else "ANALYSIS_CONNECTION_FAILED"
            raise AnalysisApiError(
                code, str(e), status=None, retryable=READ_ONLY(failure), failure=failure
            ) from e
        status = response.status_code
        if not response.is_success:
            failure = Failure("status", status)
            raise AnalysisApiError(
                _problem_code(response) or f"ANALYSIS_HTTP_{status}",
                f"analysis-api returned {status}",
                status=status,
                retryable=READ_ONLY(failure),
                failure=failure,
            )
        try:
            return model.model_validate_json(response.content)
        except ValidationError as e:
            # 같은 응답이 또 올 것이라 재시도하지 않는다.
            raise AnalysisApiError(
                "ANALYSIS_INVALID_RESPONSE",
                "analysis-api response does not match the contract",
                status=status,
                retryable=False,
                failure=Failure("invalid", status),
            ) from e


def _page(cursor: str | None, limit: int | None) -> dict[str, str]:
    params: dict[str, str] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = str(limit)
    return params


def _problem_code(response: httpx.Response) -> str | None:
    try:
        problem = ProblemDetail.model_validate_json(response.content)
    except ValidationError:
        return None
    return problem.code or None
