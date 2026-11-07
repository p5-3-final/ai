"""처리 단계 성능 Gate CI 명령(SPEC-030 R-19).

    uv run --package vlm-evals python -m evals.stage_gate \\
      --baseline-sources baseline.txt --candidate-sources candidate.txt \\
      [--stage-id analyzer.judge ...] [--max-regression-ratio 0.2] [--min-samples 5]

기준·후보 벤치마크 실행이 처리한 영상 ID 목록(한 줄에 하나, 빈 줄과 `#` 주석은 건너뜀)을
quality-service `evaluateStageGate`에 넘긴다. 종료 코드는 Gate 통과 0, Gate 실패 1, 호출 실패·
계약과 맞지 않는 응답·입력 오류 2다. 2를 1과 나누는 것은 "느려졌다"와 "판정하지 못했다"를 CI가
구분하게 하려는 것이다.
"""

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import Field, ValidationError
from pydantic_settings import SettingsConfigDict
from vlm_common.http_call import Deadline, Sleep
from vlm_common.logging import configure_logging
from vlm_common.settings import ServiceSettings
from vlm_quality_client.client import QualityApiClient, QualityApiError
from vlm_quality_client.models import StageGateRequest, StageGateResult

# `python -m`으로 실행하면 __name__이 "__main__"이라 로그를 거를 수 있게 이름을 고정한다.
logger = logging.getLogger("evals.stage_gate")

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2


class EvalsSettings(ServiceSettings):
    """평가 명령 설정. 환경변수 `EVALS_*`로 읽는다.

    Attributes:
        quality_url: quality-service 내부 주소.
        caller: `X-Caller` 값. quality-service 허용 호출자 목록에 있어야 한다
            (계약 예시 `quality-eval`).
        timeout_s: 시도 하나의 제한 시간.
        deadline_s: 재시도를 포함한 호출 전체의 제한 시간.
    """

    model_config = SettingsConfigDict(env_prefix="EVALS_", frozen=True, extra="ignore")

    quality_url: str = Field(min_length=1)
    caller: str = "quality-eval"
    timeout_s: float = Field(default=30.0, gt=0)
    deadline_s: float = Field(default=120.0, gt=0)


class _InputError(Exception):
    """목록 파일이나 인자가 계약에 맞지 않는다."""


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: EvalsSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> int:
    """명령을 실행하고 종료 코드를 돌려준다.

    Args:
        argv: 명령 인자. None이면 프로세스 인자다.
        settings: 설정. None이면 환경변수에서 읽는다.
        transport: 테스트가 바꿔 넣는 전송 계층.
        sleep: 재시도 전 대기 함수.

    Returns:
        `EXIT_PASSED`(0), `EXIT_FAILED`(1), `EXIT_ERROR`(2).
    """
    args = _parser().parse_args(argv)
    try:
        settings = settings or EvalsSettings()
    except ValidationError as e:
        logger.error("stage gate settings invalid", extra={"errors": _error_fields(e)})
        return EXIT_ERROR
    try:
        request = _request(args)
    except _InputError as e:
        logger.error("stage gate input invalid", extra={"reason": str(e)})
        return EXIT_ERROR
    try:
        result = asyncio.run(_evaluate(request, settings, transport, sleep))
    except QualityApiError as e:
        logger.error(
            "stage gate call failed",
            extra={"code": e.code, "status": e.status, "retryable": e.retryable},
        )
        return EXIT_ERROR
    return _report(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.stage_gate",
        description="처리 단계 p95를 기준 실행과 비교해 느려졌으면 실패한다(SPEC-030 R-19).",
    )
    parser.add_argument(
        "--baseline-sources", type=Path, required=True, help="기준 영상 ID 목록 파일"
    )
    parser.add_argument(
        "--candidate-sources", type=Path, required=True, help="후보 영상 ID 목록 파일"
    )
    parser.add_argument(
        "--stage-id", action="append", dest="stage_ids", help="비교할 단계. 없으면 양쪽 모든 단계"
    )
    parser.add_argument("--max-regression-ratio", type=float, help="허용 비율(서버 기본 0.2)")
    parser.add_argument("--min-samples", type=int, help="단계별 최소 기록 수(서버 기본 5)")
    return parser


def _request(args: argparse.Namespace) -> StageGateRequest:
    body = {
        "baselineSourceIds": _read_sources(args.baseline_sources),
        "candidateSourceIds": _read_sources(args.candidate_sources),
        "stageIds": args.stage_ids,
        "maxRegressionRatio": args.max_regression_ratio,
        "minSamples": args.min_samples,
    }
    try:
        # 비운 선택 인자(None)는 클라이언트가 빼고 보내 서버 기본값이 쓰인다.
        return StageGateRequest.model_validate(body)
    except ValidationError as e:
        raise _InputError(f"request does not match the contract: {_error_fields(e)}") from e


def _read_sources(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise _InputError(f"cannot read {path}: {e.strerror}") from e
    ids: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ids.append(str(UUID(line)))
        except ValueError as e:
            raise _InputError(f"{path}:{number} is not a UUID") from e
    # 계약이 uniqueItems라 서버가 400을 돌려줄 요청을 보내지 않는다. 목록을 고치게 알린다.
    if len(set(ids)) != len(ids):
        raise _InputError(f"{path} has duplicate source ids")
    return ids


def _error_fields(error: ValidationError) -> list[str]:
    # 값 자체(영상 ID 등)는 로그에 남기지 않고 어느 필드가 틀렸는지만 남긴다.
    return [".".join(str(part) for part in item["loc"]) for item in error.errors()]


async def _evaluate(
    request: StageGateRequest,
    settings: EvalsSettings,
    transport: httpx.AsyncBaseTransport | None,
    sleep: Sleep,
) -> StageGateResult:
    async with QualityApiClient(
        settings.quality_url,
        caller=settings.caller,
        timeout_s=settings.timeout_s,
        transport=transport,
        sleep=sleep,
    ) as client:
        return await client.evaluate_stage_gate(
            request, deadline=Deadline.start(settings.deadline_s)
        )


def _report(result: StageGateResult) -> int:
    for stage in result.stages:
        logger.info(
            "stage gate stage",
            extra={
                "stage_id": stage.stage_id,
                "baseline_p95_ms": stage.baseline_p95_ms,
                "candidate_p95_ms": stage.candidate_p95_ms,
                "ratio": stage.ratio,
                "baseline_count": stage.baseline_count,
                "candidate_count": stage.candidate_count,
                "status": stage.status,
            },
        )
    failed = [stage.stage_id for stage in result.stages if stage.status == "failed"]
    logger.log(
        logging.INFO if result.passed else logging.ERROR,
        "stage gate result",
        extra={
            "passed": result.passed,
            "max_regression_ratio": result.max_regression_ratio,
            "failed_stages": failed,
        },
    )
    return EXIT_PASSED if result.passed else EXIT_FAILED


def _cli() -> int:
    configure_logging("evals", "INFO")
    return main()


if __name__ == "__main__":
    raise SystemExit(_cli())
