from uuid import UUID, uuid4

import pytest
from ai_gateway.errors import ImagePathForbiddenError
from ai_gateway.images import ImagePolicy

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OTHER = uuid4()
BLURRED = r"^sources/{source_id}/.+/blurred/[^/]+\.(jpg|png)$"
# 영상 ID 자리 없이 쓴 패턴. 영상 폴더 검사(R-10)가 따로 막아야 한다.
LOOSE = r"^sources/[^/]+/.+/blurred/[^/]+\.jpg$"


def test_spec009_r9_blurred_copy_is_allowed() -> None:
    ImagePolicy([BLURRED]).check(SOURCE, [f"sources/{SOURCE}/chunks/0/blurred/000.jpg"])


@pytest.mark.parametrize(
    "key",
    [
        f"sources/{SOURCE}/original",
        f"sources/{SOURCE}/normalizations/{uuid4()}/normalized.mp4",
        f"sources/{SOURCE}/normalizations/{uuid4()}/hls/seg_00000.ts",
        f"sources/{SOURCE}/chunks/0/frames/000.jpg",
        f"sources/{SOURCE}/chunks/0/blurred/000.gif",
    ],
)
def test_spec009_r9_paths_outside_allowed_patterns_are_forbidden(key: str) -> None:
    with pytest.raises(ImagePathForbiddenError):
        ImagePolicy([BLURRED]).check(SOURCE, [key])


@pytest.mark.parametrize(
    "key",
    [
        f"sources/{OTHER}/chunks/0/blurred/000.jpg",
        f"sources/{SOURCE}/../{OTHER}/chunks/0/blurred/000.jpg",
        f"sources/{SOURCE}/chunks/../../{OTHER}/blurred/000.jpg",
        f"/sources/{SOURCE}/chunks/0/blurred/000.jpg",
    ],
)
def test_spec009_r10_paths_outside_source_folder_are_forbidden(key: str) -> None:
    with pytest.raises(ImagePathForbiddenError):
        ImagePolicy([LOOSE]).check(SOURCE, [key])


def test_spec009_r9_one_bad_key_rejects_the_whole_request() -> None:
    good = f"sources/{SOURCE}/chunks/0/blurred/000.jpg"

    with pytest.raises(ImagePathForbiddenError):
        ImagePolicy([BLURRED]).check(SOURCE, [good, f"sources/{SOURCE}/original"])


def test_spec009_r9_pattern_must_match_whole_key() -> None:
    # 패턴 끝에 $를 빠뜨려도 뒤에 덧붙인 경로가 통과하지 않는다.
    unanchored = r"^sources/{source_id}/.+/blurred/[^/]+\.jpg"

    with pytest.raises(ImagePathForbiddenError):
        ImagePolicy([unanchored]).check(SOURCE, [f"sources/{SOURCE}/c/blurred/0.jpg/raw.mp4"])
