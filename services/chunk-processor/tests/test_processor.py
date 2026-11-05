"""[SPEC-014 NFR-2] 운영 이미지의 실제 ffmpeg 추출과 시간 매핑."""

import json
import subprocess
from pathlib import Path

import pytest
from chunk_processor.processor import VerificationProcessor
from chunk_processor.settings import Settings
from chunk_support import request
from vlm_contracts.events import TimeRange
from vlm_storage.memory import InMemoryStorage


def test_spec014_adapter_cannot_run_in_production() -> None:
    with pytest.raises(ValueError, match="production inference"):
        Settings(
            enabled=True,
            environment="production",
            adapter="verification",
            database_url="postgresql+asyncpg://localhost/worker",
            kafka_bootstrap="localhost:9092",
        )


@pytest.mark.ffmpeg
def test_spec014_extracts_actual_frames_and_mapping(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(  # noqa: S603  # 테스트가 만든 파일 인자만 쓰며 셸을 쓰지 않는다 (#33)
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=1",
            "-c:v",
            "libx264",
            str(source),
        ],
        capture_output=True,
        check=True,
    )  # 테스트가 만든 argv이며 셸을 쓰지 않는다 (#33)
    r = request().model_copy(
        update={
            "processing_range": TimeRange(start_ms=55, end_ms=555),
            "owned_range": TimeRange(start_ms=100, end_ms=500),
        }
    )
    storage = InMemoryStorage()
    storage.put_file(r.normalized_key, source, "video/mp4")
    key = VerificationProcessor(storage, tmp_path / "work").execute(r)
    artifact = json.loads(storage.get_bytes(key))
    assert float(artifact["actual_start_seconds"]) == 0.1
    assert artifact["requested_start_ms"] == 55
    assert artifact["verification"] is True
    assert artifact["output"] == {"verification": True}
    chunk = tmp_path / "prepared.mp4"
    storage.get_file(artifact["input_key"], chunk)
    probe = subprocess.run(  # noqa: S603  # 테스트가 만든 파일 인자만 쓰며 셸을 쓰지 않는다 (#33)
        ["/usr/bin/ffprobe", "-v", "error", "-show_streams", "-of", "json", str(chunk)],
        capture_output=True,
        text=True,
        check=True,
    )  # 테스트의 파일 경로만 인자로 사용한다 (#33)
    stream = json.loads(probe.stdout)["streams"][0]
    assert float(stream["start_time"]) == 0
    assert int(stream["nb_frames"]) == 5
    assert list((tmp_path / "work").iterdir()) == []
