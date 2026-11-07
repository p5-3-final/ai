"""grounding-worker 테스트가 함께 쓰는 메시지·가짜 미디어·가짜 탐지기·계약 검증 도우미."""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID

from grounding_worker.detector import Box, Detection, DetectorError, RgbFrame
from grounding_worker.media import MediaInfo, MediaReadError
from grounding_worker.profile import RenderProfile
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from vlm_contracts.events import Event, GroundingRequested, GroundingSegment

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
REQUEST = UUID("0192f310-0000-7000-8000-000000000001")
JOB = UUID("0192f0d5-0000-7000-8000-000000000001")
NORMALIZATION = UUID("0192f0d4-0000-7000-8000-000000000001")
MESSAGE_ID = UUID("0192f311-0000-7000-8000-000000000001")
NORMALIZED_KEY = f"sources/{SOURCE}/normalizations/{NORMALIZATION}/normalized.mp4"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TARGETS = ["person without a hard hat", "forklift"]
MODEL = "test-grounding-v1"
SOURCE_INFO = MediaInfo(
    duration_ms=120_000, fps=30, width=1920, height=1080, video_codec="h264", has_audio=True
)
CLIP_INFO = MediaInfo(
    duration_ms=10_000, fps=30, width=1920, height=1080, video_codec="h264", has_audio=True
)


def segment(index: int = 0, start_ms: int = 40_000, end_ms: int = 50_000) -> GroundingSegment:
    return GroundingSegment(
        grounding_id=UUID(f"0192f300-0000-5000-8000-0000000000a{index}"),
        segment_id=UUID(f"0192f0e0-0000-5000-8000-00000000001{index}"),
        start_ms=start_ms,
        end_ms=end_ms,
    )


def grounding_request(
    segments: Sequence[GroundingSegment] | None = None,
    *,
    targets: Sequence[str] = TARGETS,
    request_id: UUID = REQUEST,
) -> GroundingRequested:
    return GroundingRequested(
        event_id=MESSAGE_ID,
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        request_id=request_id,
        job_id=JOB,
        normalization_id=NORMALIZATION,
        normalized_key=NORMALIZED_KEY,
        canonical_query="person without a hard hat near forklift",
        targets=list(targets),
        segments=list(segments or [segment()]),
    )


@dataclass
class FakeMedia:
    """서명 주소(`memory://`)는 원본, 로컬 경로는 만든 클립으로 보고 정해 둔 정보를 돌려준다."""

    source: MediaInfo = SOURCE_INFO
    clip: MediaInfo = CLIP_INFO
    probe_fails: bool = False
    frame_fails_at: int | None = None
    render_fails: bool = False
    # 호출마다 기다리는 시간. 제한 시간 테스트에 쓴다.
    delay_s: float = 0
    probes: list[str] = field(default_factory=list)
    frames: list[int] = field(default_factory=list)
    # 렌더링 호출의 (주소, 시작, 길이)
    renders: list[tuple[str, int, int]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)

    def called(self) -> bool:
        return bool(self.probes or self.frames or self.renders)

    async def probe(self, url: str) -> MediaInfo:
        self.probes.append(url)
        await asyncio.sleep(self.delay_s)
        if not url.startswith("memory://"):
            return self.clip
        if self.probe_fails:
            raise MediaReadError("broken")
        return self.source

    async def extract_frame(self, url: str, pts_ms: int, info: MediaInfo) -> RgbFrame:
        self.frames.append(pts_ms)
        await asyncio.sleep(self.delay_s)
        if pts_ms == self.frame_fails_at:
            raise MediaReadError("decode failed")
        return RgbFrame(data=bytes(12), width=2, height=2, pts_ms=pts_ms)

    async def render_clip(
        self,
        url: str,
        *,
        start_ms: int,
        duration_ms: int,
        script: Path,
        dst: Path,
        info: MediaInfo,
        profile: RenderProfile,
    ) -> None:
        self.renders.append((url, start_ms, duration_ms))
        await asyncio.sleep(self.delay_s)
        if self.render_fails:
            raise MediaReadError("render failed")
        self.scripts.append(await asyncio.to_thread(script.read_text, encoding="utf-8"))
        await asyncio.to_thread(dst.write_bytes, b"fake mp4")


type Reply = Callable[[RgbFrame, Sequence[str]], list[Detection]]


def one_each(frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
    """대상어마다 신뢰도 0.9 박스 하나."""
    return [Detection(t, Box(0.1, 0.2, 0.3, 0.4), 0.9) for t in targets]


@dataclass
class FakeDetector:
    reply: Reply = one_each
    version: str = MODEL
    fails: bool = False
    frames: list[int] = field(default_factory=list)

    def detect(self, frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        self.frames.append(frame.pts_ms)
        if self.fails:
            raise DetectorError("model crashed")
        return self.reply(frame, targets)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)
    fails: bool = False

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        if self.fails:
            raise ConnectionError("broker down")
        self.published.append((topic, event, key, traceparent))


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


@cache
def _registry() -> tuple[Registry[Schema], Path]:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "events").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    resources = []
    for schema in [
        *sorted((path / "events").glob("*.schema.json")),
        path / "groundings/v1/grounding-result.schema.json",
    ]:
        raw = json.loads(schema.read_text(encoding="utf-8"))
        resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    return Registry().with_resources(resources), path


def _errors(schema_file: str, body: object) -> list[str]:
    registry, path = _registry()
    raw = json.loads((path / schema_file).read_text(encoding="utf-8"))
    validator = Draft202012Validator({"$ref": raw["$id"]}, registry=registry)
    return [e.message for e in validator.iter_errors(body)]


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    body = json.loads(event.model_dump_json(by_alias=True))
    return _errors(f"events/{event.event_type}.schema.json", body)


def result_errors(data: bytes) -> list[str]:
    """저장한 결과 파일을 docs 원본 스키마로 검증한다."""
    return _errors("groundings/v1/grounding-result.schema.json", json.loads(data))


def model_json(model: BaseModel) -> bytes:
    return model.model_dump_json(by_alias=True).encode()
