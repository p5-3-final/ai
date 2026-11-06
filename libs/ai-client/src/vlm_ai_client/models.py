# 생성 코드. scripts/generate-clients.sh로 만든다. 손으로 고치지 않는다.

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyUrl, BaseModel, ConfigDict, Field
from vlm_contracts.events import DetectableEventType


class ParseRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    query: Annotated[
        str,
        Field(
            description="사용자의 검색 문장(한국어·영어). 길이 상한은 프로필 값이다. (SPEC-012 R-20, R-21)",
            min_length=1,
        ),
    ]


type QueryRoute = Annotated[
    Literal["video_retrieval", "temporal_video_rag"],
    Field(
        description="검색 경로. (SPEC-012 R-3, R-4)\n- `video_retrieval`: 장면 찾기\n- `temporal_video_rag`: 시간·상태 질문 답변(사전의 시간·상태 표현이 있을 때)\n"
    ),
]


type ParseMethod = Annotated[
    Literal["industrial-rule", "english-phrase", "llm", "safe-fallback"],
    Field(
        description="해석 방법. (SPEC-012 R-5, R-8, R-9, R-11~R-14)\n- `industrial-rule`: 질의 사전의 한국어 표현\n- `english-phrase`: 영어 위주 문장에서 불용어를 지운 구문\n- `llm`: `query_parse` 모델 출력\n- `safe-fallback`: 위 방법으로 해석하지 못해 대상어를 `person`으로 제한함\n"
    ),
]


type Target = Annotated[str, Field(max_length=64, pattern="^[a-z0-9][a-z0-9 -]*$")]


type EventType = Annotated[
    Literal[
        "fall",
        "fire_or_smoke",
        "ppe_missing",
        "object_removed",
        "barrier_not_restored",
        "hazard_zone_access",
        "equipment_abandoned",
        "object_moved",
        "other",
    ],
    Field(
        description="이벤트 유형 8종과 other. fall(쓰러짐), fire_or_smoke(화재·연기), ppe_missing(보호구 미착용), object_removed(물건 제거), barrier_not_restored(방호 미복구), hazard_zone_access(위험구역 접근), equipment_abandoned(장비 방치: 장비 트랙이 사람 없이 정해진 시간 이상 정지), object_moved(안전시설 이동: 같은 클래스의 제거와 등장이 시간 창 안에서 짝지어짐), other(목록 밖)"
    ),
]


type TaskType = Annotated[
    str,
    Field(
        description="분석 작업 유형. 다른 taskType은 각 기능 spec에서 추가한다.\n값은 늘어날 수 있는 열린 목록(`x-extensible-enum`)이다. 쓰는 쪽은 모르는 값을 받아도 실패하지 않아야 한다. 등록되지 않은 값을 요청하면 400 `AI_UNKNOWN_TASK_TYPE`이다. (ADR-0014)\n- `event_judge`: 이벤트 후보 판정 → `VLMAnalysis` (SPEC-009)\n- `event_judge_escalate`: 위험하거나 판단 불가인 후보를 더 강한 모델로 재판정. 입력·출력 형식은 `event_judge`와 같고 모델은 프로필에서 따로 정한다. 재판정할지는 호출자(event-analyzer)가 정한다. (SPEC-020 R-20)\n- `track_attribute`: 트랙 대표 크롭 1~3장 → 보호구·역할·장비·외형 설명(`events/_track-attribute.schema.json`) (SPEC-024)\n- `object_question`: 트랙 크롭과 장면 프레임, 질문 → 답(`events/_object-answer.schema.json`) (SPEC-024)\n- `source_summary`: 영상 하나의 타임라인·위험 상태 JSON(이미지 없음) → 근거만으로 쓴 요약과 중요 순간(`events/_source-summary.schema.json`). 호출자는 aggregator다. (SPEC-027)\n- `state_compare`: 같은 자리의 전후 프레임 2장, 기대 객체 목록 → VLMAnalysis와 변화 목록(`events/_state-compare.schema.json`). 호출자는 aggregator 재고 비교다. (SPEC-028)\n- `risk_forecast`: 활성 위험의 특징 값(이미지 없음) → 다음 구간 노출 확률(`events/_risk-forecast.schema.json`). VLM이 아니라 predictor Adapter가 처리하고 1단계는 규칙 점수다. (SPEC-028)\n"
    ),
]


type EmbedTaskType = Annotated[
    str,
    Field(
        description="벡터 종류. (SPEC-011)\n값은 늘어날 수 있는 열린 목록(`x-extensible-enum`)이다. 쓰는 쪽은 모르는 값을 받아도 실패하지 않아야 한다. 등록되지 않은 값을 요청하면 400 `AI_UNKNOWN_TASK_TYPE`이다. (ADR-0014)\n- `embed_visual_text`: 영어 검색 문장 → 이미지·질의 인코더의 텍스트 벡터. `texts`를 쓴다.\n- `embed_image`: 검색용 이미지 → 같은 인코더의 이미지 벡터. `images`를 쓴다.\n- `embed_text`: 문장(한국어 포함) → 텍스트 임베딩 벡터. `texts`를 쓴다.\n"
    ),
]


type Text = Annotated[str, Field(min_length=1)]


class EmbedImageRef(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    storage_key: Annotated[
        str,
        Field(
            alias="storageKey",
            description="저장소 객체 키. taskType별 허용 패턴(설정)에 맞아야 한다. 검색용 이미지는 질의 전용 폴더 경로다. (R-10)",
            max_length=1024,
            min_length=1,
        ),
    ]


class EmbedResponse(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    call_id: Annotated[
        UUID, Field(alias="callId", description="호출 ID. 호출 로그를 찾는 키다. (SPEC-011 R-23)")
    ]
    task_type: Annotated[EmbedTaskType, Field(alias="taskType")]
    model_id: Annotated[
        str,
        Field(
            alias="modelId",
            description="벡터를 만든 모델 ID(프로필 값). 저장하는 벡터의 `embedding_model`로 쓴다. 인코더 taskType이면 frame-encoder와 같은 문자열이다. (R-6)",
        ),
    ]
    dimension: Annotated[int, Field(description="벡터 차원(프로필 값)", ge=1)]
    vectors: Annotated[
        list[list[float]],
        Field(description="입력 순서대로 만든 벡터. 각 벡터는 길이(L2 노름) 1이다. (R-4)"),
    ]


class ImageRef(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    storage_key: Annotated[
        str,
        Field(
            alias="storageKey",
            description="저장소 객체 키. 허용 패턴(설정)에 맞는 블러 사본 경로만 받는다. (R-9)",
            max_length=1024,
            min_length=1,
        ),
    ]
    pts_sec: Annotated[
        float | None,
        Field(
            alias="ptsSec", description="이 프레임의 정규화 사본 기준 시각(초). 모르면 null", ge=0.0
        ),
    ] = None


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    call_id: Annotated[
        UUID,
        Field(
            alias="callId",
            description="호출 ID. 호출 로그와 모델 원문을 찾는 키다. (SPEC-009 R-1, R-17)",
        ),
    ]
    task_type: Annotated[TaskType, Field(alias="taskType")]
    model_id: Annotated[
        str | None,
        Field(
            alias="modelId", description="호출한 모델 이름(프로필 값). 모델 미설정이면 null (R-8)"
        ),
    ]
    prompt_version: Annotated[
        str,
        Field(
            alias="promptVersion",
            description="프롬프트 버전 ID. `<taskType>@<내용 해시 12자리>`이며 고정 접두부·프롬프트·입력·출력 스키마·분류 체계 내용으로 계산한다. (R-13)",
            pattern="^[a-z_]+@[0-9a-f]{12}$",
        ),
    ]
    taxonomy_version: Annotated[
        str | None,
        Field(
            alias="taxonomyVersion",
            description="이 taskType의 입력·출력 스키마가 참조하는 분류 체계 버전(`service-contracts/taxonomy/<버전>/`). 참조하지 않으면 null (R-29)",
            pattern="^v[0-9]+$",
        ),
    ]
    unverified_reason: Annotated[
        Literal["budget_exceeded", "model_unavailable"] | None,
        Field(
            alias="unverifiedReason",
            description="`output.verificationStatus`가 `unverified`인 이유(v5.1, SPEC-016 R-22). model_unavailable: 모델 미설정(R-8), budget_exceeded: 예산 초과(SPEC-016 R-11). 그 외는 null 또는 없음. 호출자는 event.analyzed.unverifiedReason에 그대로 싣는다",
        ),
    ] = None
    output: Annotated[
        dict[str, object],
        Field(
            description="검증된 결과. 형식은 ai 레포 `services/ai-gateway/registry/prompts/<taskType>/output.schema.json`이다. `event_judge`·`event_judge_escalate`는 VLMAnalysis이고 원본은 `events/_vlm-analysis.schema.json`이다(ADR-0012, SPEC-020). (R-5~R-8)"
        ),
    ]


class Error(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    field: Annotated[str, Field(examples=["email"])]
    message: Annotated[str, Field(examples=["올바른 형식의 이메일 주소여야 합니다"])]


class ProblemDetail(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: AnyUrl
    title: Annotated[str, Field(examples=["Conflict"])]
    status: Annotated[int, Field(examples=[409])]
    detail: Annotated[str | None, Field(examples=["이미 가입된 이메일입니다."])] = None
    instance: Annotated[str | None, Field(examples=["/api/v1/auth/signup"])] = None
    code: Annotated[
        str,
        Field(
            description="서비스별 오류 코드 (<DOMAIN>_<REASON>)", examples=["AUTH_EMAIL_DUPLICATED"]
        ),
    ]
    trace_id: Annotated[str | None, Field(alias="traceId", description="요청 추적 ID")] = None
    errors: Annotated[
        list[Error] | None,
        Field(description="요청 본문 검증 오류일 때 필드별 오류 목록. 그 밖의 오류에는 없다."),
    ] = None


type Role = Literal["USER", "OPERATOR", "ADMIN"]


class PageMeta(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    page: Annotated[int, Field(ge=0)]
    size: Annotated[int, Field(ge=1)]
    total_elements: Annotated[int, Field(alias="totalElements")]
    total_pages: Annotated[int, Field(alias="totalPages")]


class EmbedRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    task_type: Annotated[EmbedTaskType, Field(alias="taskType")]
    texts: Annotated[
        list[Text] | None,
        Field(
            description="벡터로 바꿀 문장. 최대 개수와 문장 길이 상한은 taskType별 설정이다. (R-8, R-26)",
            min_length=1,
        ),
    ] = None
    images: Annotated[
        list[EmbedImageRef] | None,
        Field(
            description="벡터로 바꿀 이미지. 최대 개수는 taskType별 설정이다. (R-8, R-10)",
            min_length=1,
        ),
    ] = None


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )
    task_type: Annotated[TaskType, Field(alias="taskType")]
    source_id: Annotated[
        UUID,
        Field(
            alias="sourceId",
            description="분석하는 영상 ID. 이미지 경로는 모두 `sources/<sourceId>/` 아래여야 하고, 호출 로그를 이 ID로 남겨 영상 삭제 때 원문을 지운다. (SPEC-009 R-10, R-20)",
        ),
    ]
    images: Annotated[
        list[ImageRef],
        Field(
            description="모델에 보낼 이미지. 순서대로 모델에 전달한다. 최대 개수는 taskType별 설정이다. (R-4)"
        ),
    ]
    variables: Annotated[
        dict[str, object],
        Field(
            description="taskType의 입력 변수. 형식은 ai 레포 `services/ai-gateway/registry/prompts/<taskType>/input.schema.json`이다. (R-2)"
        ),
    ]


class ParseResponse(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    call_id: Annotated[
        UUID,
        Field(alias="callId", description="호출 ID. 호출 로그를 찾는 키다. (SPEC-012 R-1, R-27)"),
    ]
    route: QueryRoute
    targets: Annotated[
        list[Target],
        Field(
            description="영어 시각 대상어. bbox 주석(grounding) 대상으로 쓴다. (R-5, R-8, R-11)",
            max_length=3,
            min_length=1,
        ),
    ]
    event_types: Annotated[
        list[DetectableEventType],
        Field(
            alias="eventTypes",
            description="분류 체계 이벤트 유형(`other` 제외). 텍스트 인덱스 필터로 쓴다. 맞은 유형이 없으면 빈 목록이다. (R-7, R-11)",
        ),
    ]
    visual_query: Annotated[
        str,
        Field(
            alias="visualQuery",
            description="대상어로 만든 영어 검색 문장. `embed_visual_text`의 입력으로 쓴다. 같은 대상어 목록이면 같은 문장이다. (R-16)",
            min_length=1,
        ),
    ]
    parse_method: Annotated[ParseMethod, Field(alias="parseMethod")]
    rationale: Annotated[
        str,
        Field(
            description="해석 근거(해석 방법과 맞은 사전 표현). LLM 출력의 설명문은 넣지 않는다. 사람이 읽는 값이라 형식에 의존하지 않는다. (R-17)"
        ),
    ]
    dictionary_version: Annotated[
        str,
        Field(
            alias="dictionaryVersion",
            description="질의 사전 버전. `<버전 이름>@<사전 내용 해시 12자리>`다. (R-29)",
            pattern="^v[0-9]+@[0-9a-f]{12}$",
        ),
    ]
    prompt_version: Annotated[
        str | None,
        Field(
            alias="promptVersion",
            description="`parseMethod`가 `llm`일 때만 `query_parse` 프롬프트 버전 ID, 아니면 null (R-11, R-19)",
            pattern="^[a-z_]+@[0-9a-f]{12}$",
        ),
    ]
    taxonomy_version: Annotated[
        str,
        Field(
            alias="taxonomyVersion",
            description="질의 사전과 `query_parse` 출력 스키마가 참조하는 분류 체계 버전(`service-contracts/taxonomy/<버전>/`) (R-30)",
            pattern="^v[0-9]+$",
        ),
    ]
    canonical_query: Annotated[
        str,
        Field(
            alias="canonicalQuery",
            description="표준 질의 문자열. route·대상어·이벤트 유형·해석 방법·사전 버전·분류 체계 버전(`llm`이면 프롬프트 버전 포함)이 같으면 같은 값이고 하나라도 다르면 다른 값이다. (R-18)\n호출자는 이 문자열을 해석하지 않고 그대로 재사용 키의 일부로 쓴다. 형식은 바뀔 수 있다.\n",
            min_length=1,
        ),
    ]
