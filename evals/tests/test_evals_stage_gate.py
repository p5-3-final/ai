"""처리 단계 성능 Gate CI 명령(SPEC-030 R-19).

quality-service는 MockTransport로 대신한다.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest
from evals.stage_gate import EvalsSettings, main

BASELINE = ["0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e10"]
CANDIDATE = ["0192f0d2-0000-7000-8000-0000000000bb"]
JUDGE = {
    "stageId": "analyzer.judge",
    "baselineP95Ms": 3400.0,
    "candidateP95Ms": 4300.0,
    "ratio": 1.265,
    "baselineCount": 30,
    "candidateCount": 31,
    "status": "failed",
}
TRANSCODE = {
    "stageId": "normalize.transcode",
    "baselineP95Ms": 8000.0,
    "candidateP95Ms": None,
    "ratio": None,
    "baselineCount": 3,
    "candidateCount": 0,
    "status": "skipped",
}
SETTINGS = EvalsSettings(quality_url="http://quality-service:8088")
LOGGER = "evals.stage_gate"


def _result(passed: bool, *stages: dict[str, object]) -> dict[str, object]:
    return {"passed": passed, "maxRegressionRatio": 0.2, "stages": list(stages)}


async def _no_sleep(_s: float) -> None:
    return None


def _sources(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _args(tmp_path: Path, *extra: str, baseline: list[str] | None = None) -> list[str]:
    base = _sources(tmp_path, "baseline.txt", BASELINE if baseline is None else baseline)
    cand = _sources(tmp_path, "candidate.txt", CANDIDATE)
    return ["--baseline-sources", str(base), "--candidate-sources", str(cand), *extra]


def _run(
    argv: list[str], replies: list[httpx.Response | Exception]
) -> tuple[int, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    code = main(argv, settings=SETTINGS, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    return code, seen


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def test_spec030_r19_passed_gate_exits_0_and_sends_sources_from_files(tmp_path: Path) -> None:
    """[SPEC-030 R-19] passed: true면 0이다. 목록 파일의 빈 줄과 `#` 주석은 건너뛴다."""
    baseline = ["# 기준 실행", BASELINE[0], "", f"  {BASELINE[1]}  "]
    argv = _args(
        tmp_path,
        "--stage-id",
        "analyzer.judge",
        "--stage-id",
        "normalize.transcode",
        "--max-regression-ratio",
        "0.25",
        "--min-samples",
        "10",
        baseline=baseline,
    )
    code, seen = _run(argv, [httpx.Response(200, json=_result(True))])

    assert code == 0
    [sent] = seen
    assert sent.headers["X-Caller"] == "quality-eval"
    assert json.loads(sent.content) == {
        "baselineSourceIds": BASELINE,
        "candidateSourceIds": CANDIDATE,
        "stageIds": ["analyzer.judge", "normalize.transcode"],
        "maxRegressionRatio": 0.25,
        "minSamples": 10,
    }


def test_spec030_r19_optional_arguments_use_server_defaults(tmp_path: Path) -> None:
    """[SPEC-030 R-19] 단계·비율·최소 개수를 주지 않으면 보내지 않고 서버 기본값을 쓴다."""
    code, seen = _run(_args(tmp_path), [httpx.Response(200, json=_result(True))])

    assert code == 0
    assert json.loads(seen[0].content) == {
        "baselineSourceIds": BASELINE,
        "candidateSourceIds": CANDIDATE,
    }


def test_spec030_r19_failed_gate_exits_1_and_logs_each_stage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[SPEC-030 R-19] passed: false면 1이고, 단계별 결과를 구조화 로그로 남긴다."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    code, _ = _run(_args(tmp_path), [httpx.Response(200, json=_result(False, JUDGE, TRANSCODE))])

    assert code == 1
    stages = [r for r in _records(caplog) if r.getMessage() == "stage gate stage"]
    assert [
        (
            getattr(r, "stage_id", None),
            getattr(r, "baseline_p95_ms", None),
            getattr(r, "candidate_p95_ms", None),
            getattr(r, "ratio", None),
            getattr(r, "status", None),
        )
        for r in stages
    ] == [
        ("analyzer.judge", 3400.0, 4300.0, 1.265, "failed"),
        ("normalize.transcode", 8000.0, None, None, "skipped"),
    ]
    [summary] = [r for r in _records(caplog) if r.getMessage() == "stage gate result"]
    assert getattr(summary, "passed", None) is False
    assert getattr(summary, "failed_stages", None) == ["analyzer.judge"]
    assert summary.levelno == logging.ERROR


@pytest.mark.parametrize(
    ("replies", "code"),
    [
        ([httpx.Response(503)] * 3, "QUALITY_HTTP_503"),
        (
            [
                httpx.Response(
                    403,
                    json={
                        "type": "about:blank",
                        "title": "Forbidden",
                        "status": 403,
                        "code": "QUALITY_CALLER_NOT_ALLOWED",
                    },
                )
            ],
            "QUALITY_CALLER_NOT_ALLOWED",
        ),
        ([httpx.Response(200, json={"passed": False})], "QUALITY_INVALID_RESPONSE"),
        ([httpx.ConnectError("refused")] * 3, "QUALITY_CONNECTION_FAILED"),
    ],
    ids=["503-exhausted", "403", "invalid-response", "connect-exhausted"],
)
def test_spec030_r19_call_failure_exits_2(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    replies: list[httpx.Response | Exception],
    code: str,
) -> None:
    """[SPEC-030 R-19] 호출 실패·계약과 맞지 않는 응답은 Gate 판정이 아니라 2로 끝난다."""
    exit_code, _ = _run(_args(tmp_path), list(replies))

    assert exit_code == 2
    [error] = [r for r in _records(caplog) if r.getMessage() == "stage gate call failed"]
    assert getattr(error, "code", None) == code


@pytest.mark.parametrize(
    ("baseline", "extra"),
    [
        (["not-a-uuid"], []),
        ([BASELINE[0], BASELINE[0]], []),
        (["# 비어 있음"], []),
        (BASELINE, ["--stage-id", "no.such_stage"]),
        (BASELINE, ["--min-samples", "0"]),
        (BASELINE, ["--max-regression-ratio", "0"]),
    ],
    ids=["bad-uuid", "duplicate", "empty", "unknown-stage", "min-samples", "ratio"],
)
def test_spec030_r19_invalid_input_exits_2_without_calling(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, baseline: list[str], extra: list[str]
) -> None:
    """[SPEC-030 R-19] 목록이나 인자가 계약에 맞지 않으면 부르지 않고 2로 끝난다."""
    code, seen = _run(_args(tmp_path, *extra, baseline=baseline), [])

    assert code == 2
    assert seen == []
    assert [r for r in _records(caplog) if r.getMessage() == "stage gate input invalid"]


def test_spec030_r19_missing_source_file_exits_2(tmp_path: Path) -> None:
    """[SPEC-030 R-19] 목록 파일이 없으면 2다."""
    argv = _args(tmp_path)
    argv[1] = str(tmp_path / "missing.txt")

    code, seen = _run(argv, [])

    assert (code, seen) == (2, [])


def test_spec030_r19_missing_quality_url_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[SPEC-030 R-19] quality-service 주소(EVALS_QUALITY_URL)가 없으면 2다."""
    monkeypatch.delenv("EVALS_QUALITY_URL", raising=False)

    assert main(_args(tmp_path)) == 2
