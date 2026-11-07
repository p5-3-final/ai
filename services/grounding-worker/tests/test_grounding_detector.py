"""탐지기 인터페이스와 결정적 mock 구현(SPEC-026 R-3)."""

import pytest
from grounding_worker.detector import MockDetector, RgbFrame

TARGETS = ["person without a hard hat", "forklift"]


def _frame(pts_ms: int = 40_000) -> RgbFrame:
    return RgbFrame(data=bytes(4 * 2 * 3), width=4, height=2, pts_ms=pts_ms)


def test_spec026_r3_mock_detects_each_target_deterministically() -> None:
    detector = MockDetector("mock-grounding-v1")

    first = detector.detect(_frame(), TARGETS)
    second = MockDetector("mock-grounding-v1").detect(_frame(), TARGETS)

    assert first == second
    assert [d.target for d in first] == TARGETS


@pytest.mark.parametrize("pts_ms", [0, 1_000, 40_000, 3_599_000])
def test_spec026_r3_mock_boxes_stay_inside_the_frame(pts_ms: int) -> None:
    for detection in MockDetector("m").detect(_frame(pts_ms), TARGETS):
        box = detection.box
        assert box.x >= 0
        assert box.x + box.w <= 1
        assert box.y >= 0
        assert box.y + box.h <= 1
        assert box.w > 0
        assert box.h > 0
        assert 0 <= detection.confidence <= 1


def test_spec026_r3_mock_boxes_move_with_time() -> None:
    detector = MockDetector("m")

    assert detector.detect(_frame(40_000), TARGETS) != detector.detect(_frame(41_000), TARGETS)
