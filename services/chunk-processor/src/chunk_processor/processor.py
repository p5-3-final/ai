"""실제 processingRange 추출과 test 전용 추론 대역. 모델 의미를 만들지 않는다."""

import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue
from vlm_contracts.events import ChunkRequested, ProcessingProfile
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from chunk_processor.service import OutcomeUnknownError, ProcessingFailedError


class Frame(BaseModel):
    """ffprobe가 보고한 원본 영상의 실제 프레임 PTS."""

    best_effort_timestamp_time: Decimal


class Frames(BaseModel):
    """원본 시간과 준비 청크의 시각을 연결하는 근거."""

    frames: list[Frame]


class Artifact(BaseModel):
    """결과의 의미 output은 별도 AI 스키마로 두고 추출 근거와 참조만 저장한다."""

    execution_id: str
    input_key: str
    actual_start_seconds: str
    requested_start_ms: int
    processing_profile: ProcessingProfile
    verification: Literal[True] = True
    output: JsonValue


class VerificationProcessor:
    """test 환경에서만 사용한다. 영상 추출은 실제이며 추론 결과는 검증 표시뿐이다."""

    def __init__(
        self,
        storage: ObjectStorage,
        work_dir: Path,
        timeout_s: float = 60,
        outcome: Literal["success", "failure", "unknown"] = "success",
    ) -> None:
        self.storage = storage
        self.work_dir = work_dir
        self.timeout_s = timeout_s
        self.outcome = outcome

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(  # noqa: S603  # 코드가 만든 argv이며 셸을 쓰지 않는다 (#33)
                args, capture_output=True, text=True, timeout=self.timeout_s, check=False
            )
        except subprocess.TimeoutExpired as e:
            # subprocess.run은 자식을 kill·wait한 뒤 예외를 던진다. AI timeout과 구별한다.
            raise ProcessingFailedError("EXTRACTION_STOPPED_TIMEOUT") from e
        if result.returncode != 0:
            raise ProcessingFailedError("EXTRACTION_FAILED")
        return result

    def execute(self, request: ChunkRequested) -> str:
        """정규화 객체를 내려받아 배정 구간만 추출하고 실행별 결과를 저장한다."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.work_dir) as temp:
            source = Path(temp) / "normalized.mp4"
            try:
                self.storage.get_file(request.normalized_key, source)
            except ObjectNotFoundError as e:
                raise ProcessingFailedError("INPUT_NOT_FOUND") from e
            start = Decimal(request.processing_range.start_ms) / 1000
            end = Decimal(request.processing_range.end_ms) / 1000
            report = self._run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    f"{start}%{end}",
                    "-show_frames",
                    "-show_entries",
                    "frame=best_effort_timestamp_time",
                    "-of",
                    "json",
                    str(source),
                ]
            )
            frames = Frames.model_validate_json(report.stdout).frames
            times = [
                f.best_effort_timestamp_time
                for f in frames
                if start <= f.best_effort_timestamp_time < end
            ]
            if not times:
                raise ProcessingFailedError("EMPTY_PROCESSING_RANGE")
            actual = min(times)
            chunk = Path(temp) / "input.mp4"
            self._run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-t",
                    str(end),
                    "-i",
                    str(source),
                    "-vf",
                    f"select='gte(t,{actual})*lt(t,{end})',setpts=PTS-STARTPTS",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-fps_mode",
                    "vfr",
                    str(chunk),
                ]
            )
            prefix = (
                f"sources/{request.source_id}/jobs/{request.job_id}/chunks/{request.chunk_id}"
                f"/executions/{request.execution_id}/"
            )
            input_key = prefix + "input.mp4"
            self.storage.put_file(input_key, chunk, "video/mp4")
            if self.outcome == "failure":
                raise ProcessingFailedError("VERIFICATION_FAILED")
            if self.outcome == "unknown":
                raise OutcomeUnknownError("verification of uncertain inference termination")
            artifact = Artifact(
                execution_id=str(request.execution_id),
                input_key=input_key,
                actual_start_seconds=str(actual),
                requested_start_ms=request.processing_range.start_ms,
                processing_profile=request.processing_profile,
                output={"verification": True},
            )
            result_key = prefix + "result.json"
            data = artifact.model_dump_json().encode()
            self.storage.put_bytes_if_absent(result_key, data, "application/json")
            if self.storage.get_bytes(result_key) != data:
                raise ProcessingFailedError("RESULT_OBJECT_CONFLICT")
            return result_key
