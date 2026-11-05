"""질의 사전: 검색 문장을 규칙으로 표준 질의로 해석한다(SPEC-012 R-2~R-9, R-23~R-24, R-29~R-30).

원본은 `services/ai-gateway/registry/query/`이고(docs ADR-0012) 이벤트 유형은 서비스 간 계약의
분류 체계를 참조한다. 사전 버전은 사람이 붙인 버전 이름에 내용 해시를 붙여, 이름을 올리지 않고
내용만 바꿔도 예전 검색 결과와 섞이지 않게 한다(R-29).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from vlm_common.errors import VlmError

from ai_gateway.prompts import (
    PromptRegistryError,
    canonical,
    load_schemas,
    referenced_files,
    schema_errors,
    schema_registry,
    taxonomy_version_of,
)

type JsonObject = dict[str, object]
type Route = Literal["video_retrieval", "temporal_video_rag"]

_QUERY_FOLDER = "query"
_DICTIONARY_FILE = "dictionary.json"
_SCHEMA_FILE = "dictionary.schema.json"
# 프롬프트 버전 ID와 같은 길이로 자른다.
_VERSION_DIGITS = 12
_SPACES = re.compile(r"\s+")
_LATIN = re.compile(r"[a-z]")
_NOT_PHRASE = re.compile(r"[^a-z0-9 -]")
# 대상어 수 상한(계약 ParseResponse.targets)
_MAX_TARGETS = 3
# 대상어 길이 상한(계약 ParseResponse.targets)
_MAX_TARGET_CHARS = 64
# 영어 위주로 보는 최소 영문자 수. "ab" 같은 짧은 입력은 영어 구문으로 쓰지 않는다(R-8).
_MIN_ENGLISH_LETTERS = 3


class QueryDictionaryError(VlmError):
    """질의 사전을 읽거나 검증하지 못했다. 서비스는 기동에 실패해야 한다(R-24)."""

    code = "AI_QUERY_DICTIONARY_INVALID"


@dataclass(frozen=True, slots=True)
class DictionaryEntry:
    """한국어 표현 묶음 하나와 그 영어 대상어·이벤트 유형."""

    phrases: tuple[str, ...]
    target: str
    event_type: str | None


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """사전 매칭 결과. 맞은 표현이 없으면 만들지 않는다."""

    targets: tuple[str, ...]
    event_types: tuple[str, ...]
    phrases: tuple[str, ...]


@dataclass(frozen=True, slots=True, eq=False)
class QueryDictionary:
    """검증한 질의 사전."""

    # `<버전 이름>@<내용 해시 12자리>`(R-29)
    version: str
    # 이벤트 유형이 참조하는 분류 체계 버전(R-30)
    taxonomy_version: str | None
    stop_phrases: tuple[str, ...]
    english_stopwords: frozenset[str]
    temporal_terms: tuple[str, ...]
    visual_template: str
    visual_separator: str
    entries: tuple[DictionaryEntry, ...]

    @classmethod
    def load(cls, registry_dir: Path) -> "QueryDictionary":
        """`<registry_dir>/query/dictionary.json`을 스키마와 표현 규칙으로 검증해 읽는다(R-23).

        Raises:
            QueryDictionaryError: 파일이 없거나, 스키마와 맞지 않거나, 표현이 겹치거나
                정규형이 아닐 때.
        """
        folder = registry_dir / _QUERY_FOLDER
        try:
            raw = json.loads((folder / _DICTIONARY_FILE).read_text(encoding="utf-8"))
            schemas = load_schemas(registry_dir, (_QUERY_FOLDER, "taxonomy"))
        except FileNotFoundError as e:
            raise QueryDictionaryError(f"missing query dictionary: {e.filename}") from e
        except json.JSONDecodeError as e:
            raise QueryDictionaryError(f"invalid JSON in query dictionary: {e}") from e
        except PromptRegistryError as e:
            raise QueryDictionaryError(str(e)) from e
        schema_path = (folder / _SCHEMA_FILE).resolve()
        if schema_path not in schemas:
            raise QueryDictionaryError(f"missing query dictionary schema: {schema_path}")
        try:
            referenced = sorted(referenced_files(schema_path, schemas))
            taxonomy = taxonomy_version_of(
                "query dictionary", registry_dir / "taxonomy", referenced
            )
        except PromptRegistryError as e:
            raise QueryDictionaryError(str(e)) from e
        validator = Draft202012Validator(schemas[schema_path], registry=schema_registry(schemas))
        if errors := schema_errors(validator, raw):
            raise QueryDictionaryError(f"query dictionary does not match its schema: {errors}")
        dictionary = cls._from_raw(raw, taxonomy)
        dictionary._check_phrases()
        return dictionary

    @classmethod
    def _from_raw(cls, raw: JsonObject, taxonomy: str | None) -> "QueryDictionary":
        # 스키마 검증을 통과했으므로 형식은 맞다. 타입만 좁힌다.
        visual = _object(raw["visualQuery"])
        digest = hashlib.sha256(canonical(raw).encode("utf-8")).hexdigest()
        return cls(
            version=f"{raw['version']}@{digest[:_VERSION_DIGITS]}",
            taxonomy_version=taxonomy,
            # 긴 표현부터 지워 짧은 표현이 긴 표현의 일부만 지우지 않게 한다.
            stop_phrases=tuple(sorted(_strings(raw["stopPhrases"]), key=len, reverse=True)),
            english_stopwords=frozenset(_strings(raw["englishStopwords"])),
            temporal_terms=tuple(_strings(raw["temporalTerms"])),
            visual_template=str(visual["template"]),
            visual_separator=str(visual["separator"]),
            entries=tuple(
                DictionaryEntry(
                    phrases=tuple(_strings(entry["phrases"])),
                    target=str(entry["target"]),
                    event_type=str(entry["eventType"]) if "eventType" in entry else None,
                )
                for entry in map(_object, _list(raw["entries"]))
            ),
        )

    def normalize(self, query: str) -> str:
        """앞뒤·연속 공백을 정리하고 소문자로 바꾼 뒤 불용 표현을 지운다(R-2)."""
        text = _collapse(query.lower())
        for stop in self.stop_phrases:
            text = text.replace(stop, " ")
        return _collapse(text)

    def route(self, normalized: str) -> Route:
        """시간·상태 표현이 하나라도 있으면 시간·상태 질문이다(R-3, R-4)."""
        if any(term in normalized for term in self.temporal_terms):
            return "temporal_video_rag"
        return "video_retrieval"

    def match(self, normalized: str) -> RuleMatch | None:
        """사전 표현을 찾아 대상어·이벤트 유형을 사전 순서로 모은다(R-5~R-7).

        겹치는 표현은 긴 것만 쓴다(R-6). 맞은 표현이 없으면 None이다.
        """
        found = [
            (start, start + len(phrase), index, phrase)
            for index, entry in enumerate(self.entries)
            for phrase in entry.phrases
            for start in _occurrences(normalized, phrase)
        ]
        # 긴 표현부터, 같은 길이면 앞에 나온 것부터 자리를 차지한다.
        found.sort(key=lambda f: (f[0] - f[1], f[0]))
        taken: list[tuple[int, int]] = []
        matched: dict[int, list[str]] = {}
        for start, end, index, phrase in found:
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            taken.append((start, end))
            matched.setdefault(index, []).append(phrase)
        if not matched:
            return None
        used = [self.entries[i] for i in sorted(matched)]
        targets = tuple(dict.fromkeys(e.target for e in used))[:_MAX_TARGETS]
        events = tuple(dict.fromkeys(e.event_type for e in used if e.event_type is not None))
        phrases = tuple(p for i in sorted(matched) for p in matched[i])
        return RuleMatch(targets=targets, event_types=events, phrases=phrases)

    def english_phrase(self, normalized: str) -> str | None:
        """영어 위주 문장이면 불용어와 영문·숫자·공백·하이픈 밖의 문자를 지운 구문(R-8).

        영어 위주가 아니면 None, 지우고 나서 비면 빈 문자열이다(R-9).
        """
        letters = len(_LATIN.findall(normalized))
        if letters < _MIN_ENGLISH_LETTERS or letters * 2 < len(normalized.replace(" ", "")):
            return None
        text = _NOT_PHRASE.sub(" ", normalized)
        words = [w for w in text.split() if w not in self.english_stopwords]
        return _cut(" ".join(words).strip(" -"))

    def visual_query(self, targets: tuple[str, ...]) -> str:
        """대상어로 영어 검색 문장을 만든다. 같은 대상어 목록이면 같은 문장이다(R-16)."""
        return self.visual_template.replace("{targets}", self.visual_separator.join(targets))

    def _check_phrases(self) -> None:
        """표현이 정규형이고 겹치지 않는지 본다. 그렇지 않으면 그 표현은 맞을 수 없다."""
        phrases = [p for entry in self.entries for p in entry.phrases]
        for phrase in [*phrases, *self.temporal_terms, *self.stop_phrases]:
            if phrase != _collapse(phrase.lower()):
                raise QueryDictionaryError(f"phrase is not normalized: {phrase!r}")
        duplicated = sorted({p for p in phrases if phrases.count(p) > 1})
        if duplicated:
            raise QueryDictionaryError(f"duplicated phrases: {duplicated}")
        # 불용 표현은 매칭 전에 지우므로 다른 표현 안에 있으면 그 표현이 깨진다(R-2).
        for stop in self.stop_phrases:
            broken = [p for p in [*phrases, *self.temporal_terms] if stop in p]
            if broken:
                raise QueryDictionaryError(f"stop phrase {stop!r} is inside {broken}")


def _occurrences(text: str, phrase: str) -> list[int]:
    starts: list[int] = []
    start = text.find(phrase)
    while start >= 0:
        starts.append(start)
        start = text.find(phrase, start + 1)
    return starts


def _cut(phrase: str) -> str:
    """계약 길이 상한 안의 마지막 단어 경계에서 자른다."""
    if len(phrase) <= _MAX_TARGET_CHARS:
        return phrase
    head = phrase[: _MAX_TARGET_CHARS + 1]
    return head[: head.rfind(" ")].strip(" -") if " " in head else head[:_MAX_TARGET_CHARS]


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise QueryDictionaryError(f"expected an object: {value!r}")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise QueryDictionaryError(f"expected a list: {value!r}")
    return value


def _strings(value: object) -> list[str]:
    return [str(item) for item in _list(value)]
