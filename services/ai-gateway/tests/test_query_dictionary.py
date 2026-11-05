import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from ai_gateway.query_dictionary import QueryDictionary, QueryDictionaryError

type JsonObject = dict[str, object]
type Mutation = Callable[[JsonObject], None]


@pytest.fixture(scope="module")
def dictionary(registry_dir: Path) -> QueryDictionary:
    return QueryDictionary.load(registry_dir)


def _copy_with(registry_dir: Path, tmp_path: Path, change: Mutation) -> Path:
    target = tmp_path / "registry"
    shutil.copytree(registry_dir, target)
    path = target / "query" / "dictionary.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def _entries(raw: JsonObject) -> list[JsonObject]:
    entries = raw["entries"]
    assert isinstance(entries, list)
    return entries


def test_spec012_r23_loads_committed_dictionary(dictionary: QueryDictionary) -> None:
    assert re.fullmatch(r"v1@[0-9a-f]{12}", dictionary.version)


def test_spec012_r30_dictionary_refers_to_taxonomy_v2(dictionary: QueryDictionary) -> None:
    assert dictionary.taxonomy_version == "v2"


def test_spec012_r24_missing_dictionary_fails(registry_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "registry"
    shutil.copytree(registry_dir, target)
    (target / "query" / "dictionary.json").unlink()

    with pytest.raises(QueryDictionaryError):
        QueryDictionary.load(target)


def _set_event_type(value: str) -> Mutation:
    return lambda raw: _entries(raw)[0].__setitem__("eventType", value)


def _set_target(value: str) -> Mutation:
    return lambda raw: _entries(raw)[0].__setitem__("target", value)


def _add_phrase(index: int, phrase: str) -> Mutation:
    def change(raw: JsonObject) -> None:
        phrases = _entries(raw)[index]["phrases"]
        assert isinstance(phrases, list)
        phrases.append(phrase)

    return change


def _first_phrase(raw: JsonObject) -> str:
    phrases = _entries(raw)[0]["phrases"]
    assert isinstance(phrases, list)
    phrase = phrases[0]
    assert isinstance(phrase, str)
    return phrase


def _duplicate_first_phrase(raw: JsonObject) -> None:
    _add_phrase(1, _first_phrase(raw))(raw)


def _stop_phrase_inside_entry(raw: JsonObject) -> None:
    stop = raw["stopPhrases"]
    assert isinstance(stop, list)
    stop.append("지게")


def _drop(key: str) -> Mutation:
    def change(raw: JsonObject) -> None:
        del raw[key]

    return change


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_set_event_type("other"), id="event-type-other"),
        pytest.param(_set_event_type("fight"), id="event-type-not-in-taxonomy"),
        pytest.param(_set_target("지게차"), id="target-not-english"),
        pytest.param(_duplicate_first_phrase, id="duplicate-phrase"),
        pytest.param(_add_phrase(0, "안전모  미착용"), id="phrase-not-normalized-space"),
        pytest.param(_add_phrase(0, "No Helmet"), id="phrase-not-lowercase"),
        pytest.param(_stop_phrase_inside_entry, id="stop-phrase-inside-entry"),
        pytest.param(_drop("version"), id="no-version"),
    ],
)
def test_spec012_r24_invalid_dictionary_fails(
    registry_dir: Path, tmp_path: Path, change: Mutation
) -> None:
    target = _copy_with(registry_dir, tmp_path, change)

    with pytest.raises(QueryDictionaryError):
        QueryDictionary.load(target)


def test_spec012_r29_content_change_changes_version(
    registry_dir: Path, tmp_path: Path, dictionary: QueryDictionary
) -> None:
    target = _copy_with(registry_dir, tmp_path, _add_phrase(8, "리프트 트럭"))

    changed = QueryDictionary.load(target)

    assert changed.version.startswith("v1@")
    assert changed.version != dictionary.version


def test_spec012_r29_formatting_does_not_change_version(
    registry_dir: Path, tmp_path: Path, dictionary: QueryDictionary
) -> None:
    target = _copy_with(registry_dir, tmp_path, lambda raw: None)

    assert QueryDictionary.load(target).version == dictionary.version


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("  지게차   찾아줘 ", "지게차"),
        ("Forklift  NEAR a Worker", "forklift near a worker"),
        ("안전모 안 쓴 작업자 장면 보여줘", "안전모 안 쓴 작업자"),
        ("찾아줘", ""),
    ],
)
def test_spec012_r2_normalizes_query(
    dictionary: QueryDictionary, query: str, expected: str
) -> None:
    assert dictionary.normalize(query) == expected


def test_spec012_r2_same_meaning_normalizes_the_same(dictionary: QueryDictionary) -> None:
    assert dictionary.normalize("지게차 찾아줘") == dictionary.normalize("지게차 장면")


@pytest.mark.parametrize(
    "query", ["작업 종료 후 남은 위험 요소", "지게차가 지나간 이후", "쓰러진 사람이 언제 일어났어"]
)
def test_spec012_r3_temporal_term_routes_to_video_rag(
    dictionary: QueryDictionary, query: str
) -> None:
    assert dictionary.route(dictionary.normalize(query)) == "temporal_video_rag"


@pytest.mark.parametrize("query", ["지게차", "쓰러진 사람", "forklift near a worker"])
def test_spec012_r4_without_temporal_term_routes_to_retrieval(
    dictionary: QueryDictionary, query: str
) -> None:
    assert dictionary.route(dictionary.normalize(query)) == "video_retrieval"


def test_spec012_r5_matches_targets_in_dictionary_order(dictionary: QueryDictionary) -> None:
    found = dictionary.match(dictionary.normalize("지게차 옆 작업자"))

    assert found is not None
    assert found.targets == ("person", "forklift")


def test_spec012_r5_targets_are_unique_and_at_most_three(dictionary: QueryDictionary) -> None:
    found = dictionary.match("사람 근로자 지게차 트럭 자전거")

    assert found is not None
    assert found.targets == ("person", "forklift", "truck")


def test_spec012_r5_no_phrase_returns_none(dictionary: QueryDictionary) -> None:
    assert dictionary.match("forklift near a worker") is None


def test_spec012_r6_longer_overlapping_phrase_wins(dictionary: QueryDictionary) -> None:
    found = dictionary.match(dictionary.normalize("안전모 미착용 작업자"))

    assert found is not None
    assert found.targets == ("person without a hard hat", "person")
    assert "안전모 미착용" in found.phrases
    assert "안전모" not in found.phrases


def test_spec012_r7_event_types_from_matched_phrases(dictionary: QueryDictionary) -> None:
    found = dictionary.match(dictionary.normalize("쓰러진 사람 옆 연기"))

    assert found is not None
    assert found.event_types == ("fall", "fire_or_smoke")


def test_spec012_r7_no_event_type_gives_empty_list(dictionary: QueryDictionary) -> None:
    found = dictionary.match("지게차")

    assert found is not None
    assert found.event_types == ()


@pytest.mark.parametrize(
    ("normalized", "expected"),
    [
        ("forklift near a worker", "forklift near a worker"),
        ("show me the forklift scene", "the forklift"),
        ("forklift, please!", "forklift"),
        ("find", ""),
    ],
)
def test_spec012_r8_english_phrase_drops_stopwords(
    dictionary: QueryDictionary, normalized: str, expected: str
) -> None:
    assert dictionary.english_phrase(normalized) == expected


@pytest.mark.parametrize("normalized", ["사람이 들고 있는 박스 같은 것", "ab", "검은 고양이 cat"])
def test_spec012_r8_not_mostly_english_returns_none(
    dictionary: QueryDictionary, normalized: str
) -> None:
    assert dictionary.english_phrase(normalized) is None


def test_spec012_r8_long_english_phrase_is_cut_at_word_boundary(
    dictionary: QueryDictionary,
) -> None:
    phrase = dictionary.english_phrase(" ".join(["forklift"] * 12))

    assert phrase is not None
    assert len(phrase) <= 64
    assert phrase.endswith("forklift")


def test_spec012_r16_visual_query_from_targets(dictionary: QueryDictionary) -> None:
    assert dictionary.visual_query(("person", "forklift")) == "a photo of person and forklift"


# NFR-4 예시 세트: (검색 문장, route, 대상어, 이벤트 유형)
EXAMPLES: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "안전모 안 쓴 작업자 찾아줘",
        "video_retrieval",
        ("person without a hard hat", "person"),
        ("ppe_missing",),
    ),
    ("안전모를 쓴 근로자", "video_retrieval", ("person wearing a hard hat", "person"), ()),
    (
        "조끼 안 입은 인원",
        "video_retrieval",
        ("person without a safety vest", "person"),
        ("ppe_missing",),
    ),
    (
        "보호구 없이 일하는 장면",
        "video_retrieval",
        ("person without protective equipment",),
        ("ppe_missing",),
    ),
    ("쓰러진 사람", "video_retrieval", ("fallen person", "person"), ("fall",)),
    ("위험구역에 들어간 사람", "video_retrieval", ("person",), ("hazard_zone_access",)),
    ("몸싸움 장면", "video_retrieval", ("person",), ()),
    ("지게차 보여줘", "video_retrieval", ("forklift",), ()),
    ("포클레인 작업", "video_retrieval", ("excavator",), ()),
    ("화물차 진입", "video_retrieval", ("truck",), ()),
    ("주차된 승용차", "video_retrieval", ("car",), ()),
    ("이륜차 통행", "video_retrieval", ("motorcycle",), ()),
    ("자전거 탄 사람", "video_retrieval", ("person", "bicycle"), ()),
    ("연기가 나는 곳", "video_retrieval", ("smoke",), ("fire_or_smoke",)),
    ("화재 발생", "video_retrieval", ("flame",), ("fire_or_smoke",)),
    ("펜스 미복구", "video_retrieval", ("safety barrier",), ("barrier_not_restored",)),
    ("라바콘을 옮긴 장면", "video_retrieval", ("safety barrier",), ("object_moved",)),
    ("방치된 공구", "video_retrieval", ("unattended equipment",), ("equipment_abandoned",)),
    ("사라진 물건", "video_retrieval", ("object",), ("object_removed",)),
    ("헬멧 놓인 선반", "video_retrieval", ("hard hat",), ()),
    ("형광조끼 입은", "video_retrieval", ("safety vest",), ()),
    ("안전콘 위치", "video_retrieval", ("traffic cone",), ()),
    ("바리케이드 앞", "video_retrieval", ("safety barrier",), ()),
    ("파렛트 적재", "video_retrieval", ("pallet",), ()),
    ("박스 더미", "video_retrieval", ("box",), ()),
    ("강아지", "video_retrieval", ("dog",), ()),
    ("작업 종료 후 남은 상자", "temporal_video_rag", ("box",), ()),
    ("지게차가 지나간 이후 작업자", "temporal_video_rag", ("person", "forklift"), ()),
    ("연기가 계속 나는지", "temporal_video_rag", ("smoke",), ("fire_or_smoke",)),
    (
        "쓰러진 사람이 얼마나 누워 있었나",
        "temporal_video_rag",
        ("fallen person", "person"),
        ("fall",),
    ),
    ("트럭이 들어오기 전후", "temporal_video_rag", ("truck",), ()),
    ("펜스 위치 변화", "temporal_video_rag", ("safety barrier",), ()),
]


@pytest.mark.parametrize(("query", "route", "targets", "events"), EXAMPLES)
def test_spec012_nfr4_example_set_parses_as_expected(
    dictionary: QueryDictionary,
    query: str,
    route: str,
    targets: tuple[str, ...],
    events: tuple[str, ...],
) -> None:
    normalized = dictionary.normalize(query)
    found = dictionary.match(normalized)

    assert found is not None
    assert (dictionary.route(normalized), found.targets, found.event_types) == (
        route,
        targets,
        events,
    )


def test_spec012_nfr4_example_set_covers_dictionary(dictionary: QueryDictionary) -> None:
    covered: set[int] = set()
    for query, *_ in EXAMPLES:
        normalized = dictionary.normalize(query)
        for index, entry in enumerate(dictionary.entries):
            if any(p in normalized for p in entry.phrases):
                covered.add(index)
    events = {e for *_, found in EXAMPLES for e in found}
    temporal = [q for q, route, *_ in EXAMPLES if route == "temporal_video_rag"]

    assert covered == set(range(len(dictionary.entries)))
    assert events == {
        "fall",
        "fire_or_smoke",
        "ppe_missing",
        "object_removed",
        "barrier_not_restored",
        "hazard_zone_access",
        "equipment_abandoned",
        "object_moved",
    }
    assert len(temporal) >= 5
