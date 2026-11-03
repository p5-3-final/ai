#!/usr/bin/env bash
# SPEC-007 NFR-1·NFR-2 측정: 1080p 영상을 만들어 정규화 이미지에서 복사·재인코딩 시간을 잰다.
#   scripts/bench_normalize.sh            # 10분 영상, CPU 2개
#   MINUTES=1 CPUS=2 scripts/bench_normalize.sh
# 저장소 입출력은 빼고 ffmpeg 처리(사본 + HLS + 사본 검사)만 잰다. 결과는 PR에 기록한다.
set -euo pipefail

minutes="${MINUTES:-10}"
cpus="${CPUS:-2}"
root="$(cd "$(dirname "$0")/.." && pwd)"
image="vlm/normalize-test"

docker build --quiet -f "${root}/services/normalize/Dockerfile" --target test -t "${image}" "${root}" >/dev/null

docker run --rm -i --cpus "${cpus}" -v "${root}:/app" -v /app/.venv -e MINUTES="${minutes}" "${image}" \
  uv run --frozen python - <<'PY'
import subprocess
import tempfile
import time
from pathlib import Path

from normalize.decision import Normalize
from normalize.ffmpeg import FfmpegTool

from pydantic_settings import BaseSettings


class Bench(BaseSettings):
    minutes: float = 10


seconds = int(Bench().minutes * 60)
tool = FfmpegTool()
with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    src = work / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    for name, plan in [("copy", Normalize(False, 30.0)), ("transcode", Normalize(True, 30.0))]:
        out = work / f"{name}.mp4"
        start = time.monotonic()
        tool.normalize(src, out, plan, None)
        tool.make_hls(out, work / f"hls-{name}", 6.0)
        tool.probe(out)
        elapsed = time.monotonic() - start
        print(f"{name}: {seconds}s 1080p video processed in {elapsed:.1f}s ({elapsed / seconds:.2f}x)")
PY
