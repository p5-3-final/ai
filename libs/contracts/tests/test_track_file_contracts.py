"""트랙 파일 모델(트랙 요약, 번역표)이 docs 원본과 맞는지 검증한다(SPEC-022 R-1, R-12, R-13)."""

import importlib
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ValidationError

JsonObject = dict[str, object]

_FILES = {
    "ChunkTracks": "tracks/v1/chunk-tracks.schema.json",
    "TrackMapping": "tracks/v1/track-mapping.schema.json",
}


def _load(contracts_dir: Path, path: str) -> JsonObject:
    loaded: JsonObject = json.loads((contracts_dir / path).read_text(encoding="utf-8"))
    return loaded


def _model(name: str) -> type[BaseModel]:
    # 모듈을 실행 중에 읽어, 모델이 없으면 수집 오류가 아니라 이 테스트의 실패로 드러나게 한다.
    return cast(type[BaseModel], getattr(importlib.import_module("vlm_contracts.tracks"), name))


def _example(contracts_dir: Path, name: str) -> JsonObject:
    return cast(list[JsonObject], _load(contracts_dir, _FILES[name])["examples"])[0]


def _item_properties(schema: JsonObject, *path: str) -> set[str]:
    node = schema
    for key in path:
        node = cast(JsonObject, cast(JsonObject, node["properties"])[key])
        node = cast(JsonObject, node.get("items", node))
    return set(cast(JsonObject, node["properties"]))


@pytest.mark.parametrize("name", sorted(_FILES))
def test_spec022_track_file_examples_round_trip(contracts_dir: Path, name: str) -> None:
    schema = _load(contracts_dir, _FILES[name])
    model = _model(name)

    for example in cast(list[JsonObject], schema["examples"]):
        parsed = model.model_validate(example)
        assert parsed.model_dump(mode="json", by_alias=True) == example


@pytest.mark.parametrize("name", sorted(_FILES))
def test_spec022_track_file_fields_match(contracts_dir: Path, name: str) -> None:
    schema = _load(contracts_dir, _FILES[name])
    model_schema = cast(JsonObject, _model(name).model_json_schema(by_alias=True))

    assert set(cast(JsonObject, model_schema["properties"])) == set(
        cast(JsonObject, schema["properties"])
    )


def test_spec022_r1_chunk_track_fields_match(contracts_dir: Path) -> None:
    schema = _load(contracts_dir, _FILES["ChunkTracks"])
    tracks = importlib.import_module("vlm_contracts.tracks")

    def fields(model: type[BaseModel]) -> set[str]:
        return {f.alias or n for n, f in model.model_fields.items()}

    assert fields(tracks.ChunkTrack) == _item_properties(schema, "tracks")
    assert fields(tracks.BoundaryBox) == _item_properties(schema, "tracks", "boundaryBoxes")
    assert fields(tracks.RepresentativeCrop) == _item_properties(
        schema, "tracks", "representativeCrops"
    )


def test_spec022_r13_mapping_item_fields_match(contracts_dir: Path) -> None:
    schema = _load(contracts_dir, _FILES["TrackMapping"])
    tracks = importlib.import_module("vlm_contracts.tracks")

    def fields(model: type[BaseModel]) -> set[str]:
        return {f.alias or n for n, f in model.model_fields.items()}

    assert fields(tracks.MappedTrack) == _item_properties(schema, "tracks")
    assert fields(tracks.TrackLink) == _item_properties(schema, "links")


@pytest.mark.parametrize(
    "box", [[0.5, 0.2, 0.4, 0.7], [0.1, 0.8, 0.4, 0.7], [0.1, 0.2, 1.2, 0.7], [0.1, 0.2, 0.3]]
)
def test_spec022_r1_boundary_box_must_be_ordered_unit_box(
    contracts_dir: Path, box: list[float]
) -> None:
    example = _example(contracts_dir, "ChunkTracks")
    [track] = cast(list[JsonObject], example["tracks"])
    first = cast(list[JsonObject], track["boundaryBoxes"])[0]
    bad = {**track, "boundaryBoxes": [{**first, "box": box}]}

    with pytest.raises(ValidationError):
        _model("ChunkTracks").model_validate({**example, "tracks": [bad]})


def test_spec022_r13_chunk_track_ids_are_unique(contracts_dir: Path) -> None:
    example = _example(contracts_dir, "ChunkTracks")
    [track] = cast(list[JsonObject], example["tracks"])

    with pytest.raises(ValidationError, match="trackLocalId"):
        _model("ChunkTracks").model_validate({**example, "tracks": [track, track]})


def test_spec022_r13_gap_is_only_for_reid_links(contracts_dir: Path) -> None:
    example = _example(contracts_dir, "TrackMapping")
    [link] = cast(list[JsonObject], example["links"])
    model = _model("TrackMapping")

    with pytest.raises(ValidationError, match="gapMs"):
        model.model_validate({**example, "links": [{**link, "gapMs": 10}]})
    with pytest.raises(ValidationError, match="gapMs"):
        model.model_validate({**example, "links": [{**link, "kind": "reid"}]})
    reid = {**link, "kind": "reid", "gapMs": 10}
    parsed = model.model_validate({**example, "links": [reid]})
    assert parsed.model_dump(mode="json", by_alias=True)["links"] == [reid]
