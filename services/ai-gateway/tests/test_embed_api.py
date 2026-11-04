import asyncio
import io
import logging
import math
import time
from pathlib import Path

import pytest
from ai_gateway.adapters.base import ProviderFailedError, ProviderUnavailableError
from ai_gateway.call_log import CallLogEntry
from gateway_support import (
    ENCODER_ID,
    QUERY_IMAGE,
    SOURCE,
    FakeAdapter,
    FakeEmbedder,
    embed_settings,
    gateway,
    mock_encoder,
    post_embed,
    profile,
)
from PIL import Image
from vlm_encoders.base import ImageDecodeError
from vlm_storage.memory import InMemoryStorage


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 200, 30)).save(buf, "JPEG")
    return buf.getvalue()


def _put_query_image(storage: InMemoryStorage) -> None:
    storage.put_bytes(QUERY_IMAGE, _jpeg(), "image/jpeg")


async def test_spec011_r1_visual_text_returns_encoder_vectors(registry_dir: Path) -> None:
    """[SPEC-011 R-1] 문장마다 인코더 텍스트 벡터를 입력 순서대로 돌려준다."""
    encoder = mock_encoder()

    async with gateway(registry_dir, profile(embed_settings()), encoder=encoder) as gw:
        response = await post_embed(
            gw.client, {"taskType": "embed_visual_text", "texts": ["a forklift", "a person"]}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["taskType"] == "embed_visual_text"
    assert body["dimension"] == 8
    expected = encoder.encode_texts(["a forklift", "a person"])
    assert body["vectors"] == [pytest.approx(v) for v in expected]
    assert body["callId"]


async def test_spec011_r6_encoder_model_id_is_profile_model_id(registry_dir: Path) -> None:
    """[SPEC-011 R-6] 모델 ID는 frame-encoder와 같은 프로필의 인코더 모델 ID다."""
    async with gateway(registry_dir, profile(embed_settings()), encoder=mock_encoder()) as gw:
        response = await post_embed(gw.client, {"taskType": "embed_visual_text", "texts": ["x"]})

    assert response.json()["modelId"] == ENCODER_ID


async def test_spec011_r2_image_returns_encoder_image_vectors(registry_dir: Path) -> None:
    """[SPEC-011 R-2] 저장소에서 읽은 이미지마다 인코더 이미지 벡터를 돌려준다."""
    encoder = mock_encoder()

    async with gateway(
        registry_dir, profile(embed_settings()), encoder=encoder, prepare=_put_query_image
    ) as gw:
        response = await post_embed(
            gw.client, {"taskType": "embed_image", "images": [{"storageKey": QUERY_IMAGE}]}
        )

    assert response.status_code == 200
    body = response.json()
    assert (body["taskType"], body["modelId"], body["dimension"]) == ("embed_image", ENCODER_ID, 8)
    assert body["vectors"] == [pytest.approx(v) for v in encoder.encode_images([_jpeg()])]
    assert math.isclose(math.hypot(*body["vectors"][0]), 1.0, rel_tol=1e-9)


async def _embed(
    registry_dir: Path,
    body: dict[str, object],
    *,
    settings: dict[str, object] | None = None,
    caller: str | None = "event-analyzer",
) -> tuple[int, dict[str, object], list[CallLogEntry]]:
    async with gateway(
        registry_dir,
        profile(settings if settings is not None else embed_settings()),
        encoder=mock_encoder(),
        embedders={"fake": FakeEmbedder()},
        prepare=_prepare,
    ) as gw:
        response = await post_embed(gw.client, body, caller=caller)
    return response.status_code, response.json(), gw.call_log.entries


def _prepare(storage: InMemoryStorage) -> None:
    _put_query_image(storage)
    storage.put_bytes("queries/q1/broken.jpg", b"not a jpeg", "image/jpeg")
    storage.put_bytes(
        f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/q.jpg", _jpeg(), "image/jpeg"
    )


@pytest.mark.parametrize(
    "path",
    [
        # 질의 전용 폴더 밖(블러 사본도 embed_image 패턴이 아니면 막는다)
        f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/q.jpg",
        f"sources/{SOURCE}/original",
        "queries/q1/../../sources/x/original.jpg",
        "queries/q1/nested/query.jpg",
    ],
)
async def test_spec011_r10_rejects_image_outside_query_pattern(
    registry_dir: Path, path: str
) -> None:
    """[SPEC-011 R-10] embed_image 경로가 taskType 허용 패턴 밖이면 403이다."""
    status, body, _ = await _embed(
        registry_dir, {"taskType": "embed_image", "images": [{"storageKey": path}]}
    )

    assert (status, body["code"]) == (403, "AI_IMAGE_PATH_FORBIDDEN")


async def test_spec011_r11_rejects_missing_image(registry_dir: Path) -> None:
    """[SPEC-011 R-11] 저장소에 없는 이미지면 400이다."""
    status, body, _ = await _embed(
        registry_dir,
        {"taskType": "embed_image", "images": [{"storageKey": "queries/q1/none.jpg"}]},
    )

    assert (status, body["code"]) == (400, "AI_IMAGE_NOT_FOUND")


async def test_spec011_r12_rejects_undecodable_image(registry_dir: Path) -> None:
    """[SPEC-011 R-12] 디코딩할 수 없는 이미지면 400이다."""
    async with gateway(
        registry_dir,
        profile(embed_settings()),
        encoder=_DecodingEncoder(),
        prepare=_prepare,
    ) as gw:
        response = await post_embed(
            gw.client,
            {"taskType": "embed_image", "images": [{"storageKey": "queries/q1/broken.jpg"}]},
        )

    assert (response.status_code, response.json()["code"]) == (400, "AI_IMAGE_UNREADABLE")


class _DecodingEncoder:
    """mock 인코더는 이미지를 디코딩하지 않아, 실제 인코더처럼 JPEG가 아니면 거절하는 대역."""

    model_id = ENCODER_ID
    dimension = 8

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        return mock_encoder().encode_texts(texts)

    def encode_images(self, images: list[bytes]) -> list[list[float]]:
        if any(not i.startswith(b"\xff\xd8") for i in images):
            raise ImageDecodeError("cannot decode image")
        return mock_encoder().encode_images(images)


@pytest.mark.parametrize(
    "body",
    [
        {"taskType": "embed_visual_text", "images": [{"storageKey": QUERY_IMAGE}]},
        {"taskType": "embed_image", "texts": ["a forklift"]},
        {"taskType": "embed_text", "texts": ["a"], "images": [{"storageKey": QUERY_IMAGE}]},
        {"taskType": "embed_visual_text"},
        {"taskType": "embed_visual_text", "texts": []},
        {"taskType": "embed_text", "texts": ["ok", ""]},
        {"taskType": "embed_image", "images": []},
    ],
)
async def test_spec011_r7_rejects_malformed_request(
    registry_dir: Path, body: dict[str, object]
) -> None:
    """[SPEC-011 R-7] 입력 종류가 taskType과 다르거나 빈 목록·빈 문장이면 400이다."""
    status, problem, entries = await _embed(registry_dir, body)

    assert (status, problem["code"]) == (400, "AI_INVALID_REQUEST")
    assert entries == []


@pytest.mark.parametrize(
    "body",
    [
        {"taskType": "embed_visual_text", "texts": ["a", "b", "c", "d"]},
        {"taskType": "embed_text", "texts": ["a", "b", "c", "d"]},
        {"taskType": "embed_image", "images": [{"storageKey": QUERY_IMAGE}] * 3},
    ],
)
async def test_spec011_r8_rejects_too_many_inputs(
    registry_dir: Path, body: dict[str, object]
) -> None:
    """[SPEC-011 R-8] 입력 개수가 taskType 상한을 넘으면 400이다."""
    status, problem, _ = await _embed(registry_dir, body)

    assert (status, problem["code"]) == (400, "AI_TOO_MANY_INPUTS")


@pytest.mark.parametrize("task_type", ["embed_visual_text", "embed_text"])
async def test_spec011_r26_rejects_too_long_text(registry_dir: Path, task_type: str) -> None:
    """[SPEC-011 R-26] 문장 길이가 taskType 상한을 넘으면 400이다. 상한 길이는 받는다."""
    too_long, _, _ = await _embed(registry_dir, {"taskType": task_type, "texts": ["x" * 41]})
    at_limit, _, _ = await _embed(registry_dir, {"taskType": task_type, "texts": ["x" * 40]})

    assert (too_long, at_limit) == (400, 200)


async def test_spec011_r9_rejects_unknown_task_type(registry_dir: Path) -> None:
    """[SPEC-011 R-9] 모르는 taskType이면 400이다."""
    status, problem, _ = await _embed(registry_dir, {"taskType": "embed_audio", "texts": ["a"]})

    assert (status, problem["code"]) == (400, "AI_UNKNOWN_TASK_TYPE")


@pytest.mark.parametrize("caller", [None, "someone"])
async def test_spec011_r13_rejects_unknown_caller(registry_dir: Path, caller: str | None) -> None:
    """[SPEC-011 R-13] 호출자 헤더가 없거나 허용 목록에 없으면 403이다."""
    status, problem, entries = await _embed(
        registry_dir, {"taskType": "embed_visual_text", "texts": ["a"]}, caller=caller
    )

    assert (status, problem["code"]) == (403, "AI_CALLER_FORBIDDEN")
    assert entries == []


@pytest.mark.parametrize(
    ("task_type", "remove"),
    [
        ("embed_visual_text", "encoder"),
        ("embed_image", "encoder"),
        ("embed_visual_text", "embed_visual_text"),
        ("embed_text", "embed_text"),
        ("embed_text", "embed_text_model"),
    ],
)
async def test_spec011_r14_model_not_configured(
    registry_dir: Path, task_type: str, remove: str
) -> None:
    """[SPEC-011 R-14] taskType 모델이 프로필에 없으면 503이고 호출 로그는 unavailable이다."""
    settings = embed_settings()
    embed = settings["embed"]
    assert isinstance(embed, dict)
    if remove == "encoder":
        del settings["encoder"]
    elif remove == "embed_text_model":
        for key in ("adapter", "model", "dimension"):
            del embed["embed_text"][key]
    else:
        del embed[remove]
    body: dict[str, object] = (
        {"taskType": task_type, "images": [{"storageKey": QUERY_IMAGE}]}
        if task_type == "embed_image"
        else {"taskType": task_type, "texts": ["a"]}
    )

    status, problem, entries = await _embed(registry_dir, body, settings=settings)

    assert (status, problem["code"]) == (503, "AI_MODEL_NOT_CONFIGURED")
    assert [(e.task_type, e.status) for e in entries] == [(task_type, "unavailable")]


async def _embed_text(
    registry_dir: Path, embedder: FakeEmbedder, texts: list[str]
) -> tuple[int, dict[str, object], list[CallLogEntry]]:
    async with gateway(registry_dir, profile(embed_settings()), embedders={"fake": embedder}) as gw:
        response = await post_embed(gw.client, {"taskType": "embed_text", "texts": texts})
    return response.status_code, response.json(), gw.call_log.entries


async def test_spec011_r3_r4_text_returns_normalized_adapter_vectors(registry_dir: Path) -> None:
    """[SPEC-011 R-3, R-4] 프로필의 Adapter·모델로 만든 벡터를 길이 1로 정규화해 돌려준다."""
    embedder = FakeEmbedder(reply=[[3.0, 4.0, 0.0, 0.0], [0.0, 0.0, 0.0, 2.0]])

    status, body, _ = await _embed_text(registry_dir, embedder, ["첫째 문장", "둘째"])

    assert status == 200
    assert (body["taskType"], body["modelId"], body["dimension"]) == (
        "embed_text",
        "fake-embedding",
        4,
    )
    assert body["vectors"] == [[0.6, 0.8, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    [call] = embedder.calls
    assert (call.model, call.texts) == ("fake-embedding", ["첫째 문장", "둘째"])


@pytest.mark.parametrize(
    "reply",
    [
        [[1.0, 0.0, 0.0]],  # 차원이 프로필(4)과 다름
        [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],  # 입력 1개인데 벡터 2개
        [[0.0, 0.0, 0.0, 0.0]],  # 정규화할 수 없음
    ],
)
async def test_spec011_r19_rejects_provider_vectors_that_do_not_match(
    registry_dir: Path, reply: list[list[float]]
) -> None:
    """[SPEC-011 R-19] 벡터 차원·개수가 맞지 않으면 502이고 호출 로그는 invalid_output이다."""
    status, problem, entries = await _embed_text(registry_dir, FakeEmbedder(reply=reply), ["a"])

    assert (status, problem["code"]) == (502, "AI_PROVIDER_FAILED")
    assert [e.status for e in entries] == ["invalid_output"]


@pytest.mark.parametrize(
    ("error", "status", "code", "log_status"),
    [
        (ProviderUnavailableError("down"), 503, "AI_PROVIDER_UNAVAILABLE", "unavailable"),
        (ProviderFailedError("rejected"), 502, "AI_PROVIDER_FAILED", "failed"),
    ],
)
async def test_spec011_r20_r21_provider_errors_are_logged(
    registry_dir: Path, error: Exception, status: int, code: str, log_status: str
) -> None:
    """[SPEC-011 R-20, R-21] 공급자 오류를 그대로 알리고 상태를 호출 로그에 남긴다."""
    result, problem, entries = await _embed_text(registry_dir, FakeEmbedder(reply=error), ["a"])

    assert (result, problem["code"]) == (status, code)
    assert [(e.status, e.error_code) for e in entries] == [(log_status, code)]


async def test_spec011_r23_logs_embed_call_without_vectors_or_texts(registry_dir: Path) -> None:
    """[SPEC-011 R-23] 호출자·모델·입력 수·토큰·상태를 남기고 벡터와 문장 원문은 남기지 않는다."""
    embedder = FakeEmbedder()
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"

    async with gateway(registry_dir, profile(embed_settings()), embedders={"fake": embedder}) as gw:
        response = await gw.client.post(
            "/v1/embed",
            json={"taskType": "embed_text", "texts": ["비밀 검색어", "둘째"]},
            headers={"X-Caller": "event-analyzer", "traceparent": traceparent},
        )

    [entry] = gw.call_log.entries
    assert str(entry.call_id) == response.json()["callId"]
    assert (entry.caller, entry.task_type, entry.adapter, entry.model_id) == (
        "event-analyzer",
        "embed_text",
        "fake",
        "fake-embedding",
    )
    assert (entry.status, entry.input_count, entry.input_tokens) == ("success", 2, 7)
    assert (entry.source_id, entry.prompt_version, entry.raw_output) == (None, None, None)
    assert entry.trace_id == "0123456789abcdef0123456789abcdef"
    assert "비밀 검색어" not in repr(entry)
    assert entry.output == {"dimension": 4}


async def test_spec011_r23_logs_query_image_paths(registry_dir: Path) -> None:
    """[SPEC-011 R-23] embed_image는 보낸 이미지 경로를 남긴다."""
    async with gateway(
        registry_dir, profile(embed_settings()), encoder=mock_encoder(), prepare=_put_query_image
    ) as gw:
        await post_embed(
            gw.client, {"taskType": "embed_image", "images": [{"storageKey": QUERY_IMAGE}]}
        )

    [entry] = gw.call_log.entries
    assert (entry.image_keys, entry.model_id, entry.input_count) == ([QUERY_IMAGE], ENCODER_ID, 1)


async def test_spec011_r22_visual_text_works_while_providers_are_down(registry_dir: Path) -> None:
    """[SPEC-011 R-22] VLM·텍스트 임베딩 공급자가 내려가도 시각 질의 벡터는 만든다."""
    down = ProviderUnavailableError("down")

    async with gateway(
        registry_dir,
        profile(embed_settings()),
        adapter=FakeAdapter(reply=down),
        encoder=mock_encoder(),
        embedders={"fake": FakeEmbedder(reply=down)},
    ) as gw:
        response = await post_embed(gw.client, {"taskType": "embed_visual_text", "texts": ["a"]})

    assert response.status_code == 200


class _SlowEncoder:
    """인코딩에 1.5초 걸리는 대역. 이벤트 루프에서 돌리면 그동안 다른 요청을 받지 못한다."""

    model_id = ENCODER_ID
    dimension = 8

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        time.sleep(1.5)
        return mock_encoder().encode_texts(texts)

    def encode_images(self, images: list[bytes]) -> list[list[float]]:
        return mock_encoder().encode_images(images)


async def test_spec011_nfr3_health_responds_while_encoding(registry_dir: Path) -> None:
    """[SPEC-011 NFR-3] 인코딩 중에도 /health가 1초 안에 응답한다."""
    async with gateway(registry_dir, profile(embed_settings()), encoder=_SlowEncoder()) as gw:
        started = time.perf_counter()
        encoding = asyncio.create_task(
            post_embed(gw.client, {"taskType": "embed_visual_text", "texts": ["a"]})
        )
        # 인코딩이 시작되도록 한 번 양보한다. 루프가 막히면 이 대기부터 1.5초 늦어진다.
        await asyncio.sleep(0.1)
        health = await gw.client.get("/health")
        elapsed = time.perf_counter() - started
        await encoding

    assert health.status_code == 200
    assert elapsed < 1.0


async def test_spec011_nfr5_logs_have_no_texts_or_vectors(
    registry_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[SPEC-011 NFR-5] 완료 로그는 남기되 문장 원문과 벡터는 남기지 않는다."""
    embedder = FakeEmbedder(reply=[[0.123456789, 0.0, 0.0, 0.0]])

    with caplog.at_level(logging.DEBUG):
        async with gateway(
            registry_dir, profile(embed_settings()), embedders={"fake": embedder}
        ) as gw:
            await post_embed(gw.client, {"taskType": "embed_text", "texts": ["비밀 검색어"]})

    completed = [r for r in caplog.records if r.getMessage() == "embed completed"]
    assert len(completed) == 1
    assert vars(completed[0])["call_id"] == str(gw.call_log.entries[0].call_id)
    assert vars(completed[0])["input_count"] == 1
    for record in caplog.records:
        rendered = f"{record.getMessage()} {vars(record)}"
        assert "비밀 검색어" not in rendered
        assert "0.123456789" not in rendered
