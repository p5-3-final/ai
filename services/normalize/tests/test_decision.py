import pytest
from normalize.decision import Limits, Normalize, Reject, decide
from normalize.probe import ProbeResult

LIMITS = Limits(min_duration_s=1.0, max_duration_s=14_400.0, max_fps=30.0)


def _probe(**overrides: object) -> ProbeResult:
    base: dict[str, object] = {
        "has_video": True,
        "video_codec": "h264",
        "duration_sec": 60.0,
        "avg_fps": 30.0,
        "base_fps": 30.0,
        "width": 1280,
        "height": 720,
        "audio_codec": "aac",
    }
    return ProbeResult(**{**base, **overrides})  # type: ignore[arg-type]  # 테스트용 덮어쓰기 (#8)


def test_r3_h264_constant_rate_is_copied() -> None:
    assert decide(_probe(), LIMITS) == Normalize(transcode=False, fps=30.0)


def test_r3_small_rate_rounding_still_counts_as_constant() -> None:
    assert decide(_probe(avg_fps=29.97, base_fps=29.97003), LIMITS) == Normalize(
        transcode=False, fps=29.97
    )


def test_r4_variable_rate_is_transcoded_at_average_rate() -> None:
    assert decide(_probe(avg_fps=24.3, base_fps=30.0), LIMITS) == Normalize(
        transcode=True, fps=24.3
    )


def test_r4_other_codec_is_transcoded() -> None:
    assert decide(_probe(video_codec="hevc"), LIMITS) == Normalize(transcode=True, fps=30.0)


def test_r4_transcoded_rate_is_capped() -> None:
    assert decide(_probe(video_codec="vp9", avg_fps=60.0, base_fps=60.0), LIMITS) == Normalize(
        transcode=True, fps=30.0
    )


def test_r4_high_rate_h264_constant_is_still_copied() -> None:
    # 복사 경로는 프레임을 바꾸지 않으므로 상한은 재인코딩에만 적용한다(SPEC-007 용어).
    assert decide(_probe(avg_fps=60.0, base_fps=60.0), LIMITS) == Normalize(
        transcode=False, fps=60.0
    )


def test_r8_no_video_stream() -> None:
    assert decide(_probe(has_video=False, video_codec=None), LIMITS) == Reject("NO_VIDEO_STREAM")


@pytest.mark.parametrize(
    "overrides", [{"duration_sec": None}, {"avg_fps": None}, {"duration_sec": 0.0}]
)
def test_r9_missing_metadata(overrides: dict[str, object]) -> None:
    assert decide(_probe(**overrides), LIMITS) == Reject("MISSING_METADATA")


def test_r10_too_short() -> None:
    assert decide(_probe(duration_sec=0.5), LIMITS) == Reject("TOO_SHORT")


def test_r10_exactly_minimum_is_accepted() -> None:
    assert isinstance(decide(_probe(duration_sec=1.0), LIMITS), Normalize)


def test_r11_too_long() -> None:
    assert decide(_probe(duration_sec=14_400.5), LIMITS) == Reject("TOO_LONG")


def test_r11_exactly_maximum_is_accepted() -> None:
    assert isinstance(decide(_probe(duration_sec=14_400.0), LIMITS), Normalize)
