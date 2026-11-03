from pathlib import Path
from typing import Protocol

from vlm_common.errors import VlmError

from normalize.decision import Normalize
from normalize.probe import ProbeResult


class DecodeError(VlmError):
    code = "NORMALIZE_DECODE_FAILED"


class MediaTool(Protocol):
    def probe(self, path: Path) -> ProbeResult: ...
    def normalize(self, src: Path, dst: Path, plan: Normalize, audio_codec: str | None) -> None: ...
    def make_hls(self, src: Path, out_dir: Path, segment_s: float) -> list[Path]: ...
