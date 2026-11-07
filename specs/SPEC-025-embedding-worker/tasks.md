---
spec: SPEC-025
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-025 embedding-worker 이벤트·트랙·구간 주석 텍스트 벡터 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- `[확인]`은 plan 7절의 사용자 확인 결과에 따라 내용이 바뀌는 태스크다.
- 완료한 태스크는 `[x]`로 바꾼다.

경로의 `ew/`는 `services/embedding-worker/src/embedding_worker/`, `t/`는 `services/embedding-worker/tests/`다.

선행 조건: SPEC-021(ai-client `embed`, 생성 모델, `X-Request-Deadline`) 구현이 머지돼 있다. SPEC-024(`TrackAnalyzed`)·SPEC-022(`TrackMapping`) 계약 모델이 없으면 T003·T004에서 더한다.

## Phase 0: 준비

- [x] T001 계약 잠금을 3.14.0으로 올림(`scripts/fetch-service-contracts.sh --update 3.14.0`, 이미 3.14.0 이상이면 건너뜀), 단계 ID 4개 — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `service-contracts.lock`, `vlm_contracts/stages.py`
- [x] T002 [P] [R-3] [R-8] `SegmentAnnotated`·`EmbeddingReady`·`ItemKind` 모델(docs 원본과 필드·필수·enum·패턴·예시 왕복) — 테스트 `libs/contracts/tests/test_embedding_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T003 [P] [R-2] `TrackAnalyzed`·`TrackAttribute` 모델(SPEC-024가 이미 더했으면 건너뜀) — 테스트 `libs/contracts/tests/test_embedding_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T004 [P] [R-6] [R-7] `TextVectorFile`·`TextVectorMetadata`(벡터 길이 = dimension), `TrackMapping`(SPEC-022가 이미 더해 그것을 씀) — 테스트 `libs/contracts/tests/test_text_vector_file_contract.py`, 구현 `vlm_contracts/vectors.py`, `vlm_contracts/tracks.py`
- [x] T005 `services/embedding-worker` 패키지 뼈대(pyproject, 워크스페이스 `members`·mypy `files`·`mypy_path`·pytest `pythonpath` 등록, `ew_support.py`, 인터페이스만 둔 모듈)

## Phase 1: US-1 행동·속성으로 장면 찾기 (P1)

- [x] T006 [R-1] event 문장: 설명문·위험 사유·조치 제안 중 null 아닌 값을 순서대로 줄바꿈 연결, textFormat `event-v1` — 테스트 `t/test_embedding_texts.py`, 구현 `ew/texts.py`
- [x] T007 [R-2] track 문장: reason event·click, READY, confirmed, 설명 있음일 때 설명, `track-v1` — 테스트 `t/test_embedding_texts.py`, 구현 `ew/texts.py`
- [x] T008 [R-3] segment_annotation 문장: `text`, `segment_annotation-v1` — 테스트 `t/test_embedding_texts.py`, 구현 `ew/texts.py`
- [x] T009 [R-5] 최대 글자 수(기본 2000)를 넘으면 앞에서 자름, 설정 상한 2000 — 테스트 `t/test_embedding_texts.py`, 구현 `ew/texts.py`, `ew/settings.py`
- [x] T010 [확인] [R-7] event·segment_annotation metadata(시각 ms, 유형·severity·verificationStatus, segmentId, null 필드 생략) — 테스트 `t/test_embedding_texts.py`, 구현 `ew/texts.py`
- [x] T011 [확인] [R-7] track metadata: 번역표에서 className·startMs·endMs, trackId·보호구·역할. 번역표 없음은 재시도, trackId 없음은 `NonRetryableError` — 테스트 `t/test_embedding_tracks.py`, 구현 `ew/tracks.py`
- [x] T012 [P] [R-8] eventId = uuid5(itemKind·itemId·modelId), 벡터 파일 경로 — 테스트 `t/test_embedding_ids.py`, 구현 `ew/ids.py`, `ew/files.py`
- [x] T013 [R-6] [R-8] 핸들러: `embed_text` 문장 1개 호출 → 파일 저장 → `embedding.ready` 발행(파일·이벤트 계약 스키마 통과, 모델 ID·차원·textFormat 기록) — 테스트 `t/test_embedding_service.py`, 구현 `ew/service.py`
- [x] T014 [R-13] 단계 블록 `embedding.call`(문장 수)·`embedding.store`(파일 수) — 테스트 `t/test_embedding_service.py`, 구현 `ew/service.py`

## Phase 2: US-2 쓸모없는 항목은 건너뜀 (P2)

- [x] T015 [R-4] 빈 문장·조건 밖 track이면 호출·저장·발행 없이 이유 코드를 로그 — 테스트 `t/test_embedding_service.py`, 구현 `ew/texts.py`, `ew/service.py`

## Phase 3: US-3 장애 (P2)

- [x] T016 [R-9] 재시도 끝의 503·연결 실패·시간 초과는 그대로 던짐, `AI_MODEL_NOT_CONFIGURED`·`AI_BUDGET_EXCEEDED`는 `NonRetryableError` — 테스트 `t/test_embedding_service.py`, 구현 `ew/service.py`
- [x] T017 [확인] [R-9] 502·504·그 밖의 5xx 처리(기본안: 그대로 던짐) — 테스트 `t/test_embedding_service.py`, 구현 `ew/service.py`
- [x] T018 [R-10] 4xx, `AI_INVALID_RESPONSE`, 벡터 수 1이 아님, 길이 ≠ 차원이면 발행 없이 `NonRetryableError` — 테스트 `t/test_embedding_service.py`, 구현 `ew/service.py`
- [x] T019 [R-12] 저장·발행 오류는 그대로 던지고 다시 처리하면 같은 경로·같은 eventId — 테스트 `t/test_embedding_service.py`
- [x] T020 [NFR-2] 로그에 문장 원문·벡터가 없음 — 테스트 `t/test_embedding_service.py`

## Phase 4: 워커와 배포

- [x] T021 기동 조립: 설정, ai-client, 저장소, 세 `route`, health, 측정 발행 — 테스트 `t/test_embedding_main.py`, 구현 `ew/{__main__,settings}.py`
- [x] T022 [R-11] [NFR-3] 워커 e2e(Kafka testcontainer, `MockTransport`): 세 토픽 정상 발행, 잘못된 메시지 DLQ, `AI_MODEL_NOT_CONFIGURED` DLQ, 503 재시도 토픽, 발행 뒤 커밋 — 테스트 `t/test_embedding_worker_e2e.py`(구현 뒤 작성이면 PR에 "Red 없음"으로 적음)
- [x] T023 Dockerfile, README, 레포 `.env.example`(권한 설정으로 막혀 README 표에만 적음), CI `service-images` 이미지 빌드
- [x] T024 (보류: docs 레포 `local/`은 이 작업에서 고치지 않는다) docs `local/`: compose 서비스(256M), 토픽 `segment.annotated`·`embedding.ready`·재시도 3개·`embedding-worker.dlq`, `local/.env.example`, `local/README.md`(docs 레포 PR) — 완료: docs #142(로컬 compose에 서비스 256M·토픽·DLQ, README 메모리 표)
- [ ] T025 (보류: T024 compose가 있어야 측정한다) [NFR-1] 로컬 compose 측정(mock 프로필, p95 500ms). 결과는 아래 "측정 기록"에만 짧게 적음

## 측정 기록

(T025에서 채운다)

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, pytest(`not ffmpeg and not model`), `scripts/test-in-docker.sh embedding-worker` 통과
- [ ] plan.md와 실제 구현이 일치함(`[확인]` 태스크는 결정 결과를 plan에 반영)
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
