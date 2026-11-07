---
spec: SPEC-024
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-024 track-analyzer 트랙 속성 분석과 객체 자유 질문 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `ta/`는 `services/track-analyzer/src/track_analyzer/`, `t/`는 `services/track-analyzer/tests/`, `gw/`는 `services/ai-gateway/`, `lc/`는 `libs/contracts/`다.

선행 조건: SPEC-021(ai-client 생성 모델, analyze 연결 실패만 재시도, `X-Request-Deadline`, 오류 코드 규칙) 구현이 머지돼 있다. 아래 요청 모델 이름은 SPEC-021의 생성 모델을 따른다.

## Phase 0: 준비

- [x] T001 [R-10] (SPEC-021 PR #44가 잠금 4.1.0·`StageId`·`EventAnalyzed`를 반영해 확인만 함) 계약 잠금 4.0.0(`scripts/fetch-service-contracts.sh --update 4.0.0`)과 그 때문에 깨지는 계약 테스트 맞춤: `StageId` 전체 목록(`track-analyzer.*`·`attribute.call` 포함), `EventAnalyzed`의 판정 방식 5종·`DetectorEvidence`·`judgments` 개수 규칙. 잠금이 이미 3.14.0이고 이 테스트가 통과하면 건너뛴다(SPEC-023 T001과 같은 태스크) — 테스트 `lc/tests/test_stage_contracts.py`, `lc/tests/test_event_contracts.py`, `lc/tests/test_analysis_contracts.py`, 구현 `service-contracts.lock`, `vlm_contracts/{stages,events}.py`
- [x] T002 [R-3] `PpeState`·`WorkerRole`, `TrackAttribute`, `ObjectAnswer`, `TrackAnalyzed`(READY·FAILED와 reason별 결과 규칙), `EVENT_MODELS` 등록 — 테스트 `lc/tests/test_event_contracts.py`(자동 계약 검사), `lc/tests/test_track_attribute_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T003 [R-7] `AttributeRequested`(`question`이면 질문 필수, 아니면 질문 null·장면 프레임 없음), `EVENT_MODELS` 등록 — 테스트 `lc/tests/test_event_contracts.py`, `lc/tests/test_track_attribute_contracts.py`
- [x] T004 [R-6] 크롭·장면 프레임 경로가 `sources/<sourceId>/` 밖이거나 `.`·`..`·빈 조각이 있으면 검증 실패 — 테스트 `lc/tests/test_track_attribute_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T005 [P] (보류: SPEC-022 작업자가 `vlm_contracts/tracks.py`를 만드는 중이라 SPEC-022가 4.1.0 계약대로 넣는다) 트랙 요약 파일 모델에 `representativeCrops`. SPEC-022 모델이 아직 없으면 건너뛰고 SPEC-022가 4.0.0 계약대로 넣는다 — 테스트 SPEC-022의 트랙 파일 계약 테스트, 구현 `vlm_contracts/tracks.py` — 완료: SPEC-022가 `vlm_contracts/tracks.py`에 `representative_crops`를 넣었다(2026-11-07 확인)
- [x] T006 `services/track-analyzer` 패키지 뼈대(pyproject, `.gitkeep` 삭제, 루트 `members`·mypy `files`·`mypy_path`·pytest `pythonpath` 등록, `ta_support.py`)

## Phase 1: ai-gateway (US-1·US-2·US-3, P1)

- [x] T007 [P] [R-11] `track_attribute` 등록: 입력 `className`, 출력 `_track-attribute` 참조, 자리 표시 프롬프트, `fallback.json`, 두 로컬 프로필(`max_images` 3), 테스트 registry 배치·`gateway_support.profile()`·Dockerfile COPY — 테스트 `gw/tests/test_prompts.py`, `gw/tests/test_analyze_api.py`, 구현 `gw/registry/prompts/track_attribute/`, `gw/profiles/*.toml`, `gw/tests/{conftest,gateway_support}.py`, `gw/Dockerfile`
- [x] T008 [P] [R-12] `object_question` 등록: 입력 `className`·`question`(1~500자), 출력 `_object-answer` 참조, `max_images` 11 — 테스트 `gw/tests/test_prompts.py`, `gw/tests/test_analyze_api.py`, 구현 `gw/registry/prompts/object_question/`, 프로필, 같은 배치 파일
- [x] T009 [R-11] [R-12] (D-11 기본안으로 결정, 2026-11-07) 이미지 하한: `TaskProfile.min_images`(기본 0), 두 taskType 1, 모자라면 400 `AI_INVALID_REQUEST` — 테스트 `gw/tests/test_profile.py`, `gw/tests/test_analyze_api.py`, 구현 `gw/src/ai_gateway/{profile,analyze}.py`
- [x] T010 [R-13] (기존 대체 결과 경로로 통과, Red 없음) 두 taskType의 모델 미설정·예산 초과 → `unverified`, 출력 검증 실패 → `unknown`(판정 필드 null, `equipment` 빈 배열). 기존 대체 결과 경로로 통과하면 Red 없음으로 기록 — 테스트 `gw/tests/test_analyze_api.py`

## Phase 2: US-1·US-2 속성과 질문 (P1)

- [x] T011 [R-1] `event`·`click` → `track_attribute`: sourceId, 크롭 순서와 `ptsSec`, 변수 `className` — 테스트 `t/test_track_service.py`, 구현 `ta/service.py`
- [x] T012 [R-2] `question` → `object_question`: 크롭 다음 장면 프레임, 변수 `className`·`question` — 테스트 `t/test_track_service.py`
- [x] T013 [R-3] READY 발행: reason별 `attributes`·`answer`, 호출 ID·모델·프롬프트 버전·분류 체계 버전, `unverified`·`unknown`도 READY, 계약 스키마 통과 — 테스트 `t/test_track_service.py`
- [x] T014 [R-5] eventId = requestId 기반 결정적 ID, 같은 요청 → 같은 eventId — 테스트 `t/test_track_service.py`, 구현 `ta/ids.py`
- [x] T015 [R-10] 단계 블록 `attribute.call`(count = 보낸 이미지 수) — 테스트 `t/test_track_service.py`

## Phase 3: US-3·US-4 실패 (P1·P2)

- [x] T016 [R-4] 503(다시 부르지 않음)·504·시간 초과·연결 실패 소진·403·`AI_IMAGE_NOT_FOUND`·잘못된 출력(`AI_INVALID_RESPONSE`) → FAILED와 오류 코드, 나머지 필드 null — 테스트 `t/test_track_service.py`
- [x] T017 [R-8] (T011 구현이 이미 그대로 던져 Red 없음) 발행 오류는 그대로 던짐 — 테스트 `t/test_track_service.py`
- [x] T018 [NFR-3] (T016 로그가 식별자만 실어 Red 없음) 성공·실패 모두 로그에 질문·답·설명 문장이 없음 — 테스트 `t/test_track_service.py`(`caplog`)

## Phase 4: 워커와 배포

- [x] T019 [R-9] 기동 조립: 설정, 클라이언트(`X-Caller` `track-analyzer`, 설정 제한 시간으로 `X-Request-Deadline`), 워커 route `attribute.requested` — 테스트 `t/test_track_main.py`, 구현 `ta/{__main__,settings}.py`
- [x] T020 [R-6] [R-7] [NFR-2] 워커 e2e: 정상 발행, 잘못된 메시지 DLQ, sourceId 밖 경로 DLQ(ai-gateway 호출 0회), 긴 호출 중 커밋(구현 뒤 작성, Red 없음) — 테스트 `t/test_track_worker_e2e.py`
- [x] T021 Dockerfile, README, CI `service-images` runtime 빌드 (레포 `.env.example`은 에이전트 권한 설정이 읽기를 막아 고치지 못했다. event-analyzer 변수도 그 파일에 없고 README 표가 기준이다)
- [x] T022 [NFR-1] 로컬 compose 측정: click·question 요청 발행부터 `track.analyzed`까지 p95 2초 안, 성공 지표(click 1, question 1 → 2건 계약 통과)(docs compose PR 뒤) — 완료: 아래 측정 기록

## 측정 기록

2026-11-07, 로컬 compose mock 프로필(M1 Pro, Docker 10 CPU·25GB), docs `scripts/e2e/run.sh measure`

| 항목 | 기준 | 결과 |
| --- | --- | --- |
| 성공 지표(click 1, question 1) | `track.analyzed` 2건, 계약 통과 | 2건, 계약 통과 |
| NFR-1 click 20회, 발행부터 `track.analyzed`까지 | p95 2초 | p95 32ms |

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, `pytest -m "not ffmpeg and not model"`, `scripts/test-in-docker.sh track-analyzer`·`ai-gateway` 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
