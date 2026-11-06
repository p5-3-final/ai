---
spec: SPEC-027
repo: ai
updated: 2026-11-06
---

# Tasks: SPEC-027 aggregator 영상 단위 병합·시간 규칙·위험 상태·노출 지표·요약 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝낸다.
- 완료한 태스크는 `[x]`로 바꾼다.
- 선행: SPEC-021(ai-client 생성 모델·재시도 규칙·생성 코드 비교 CI)이 머지된 뒤 시작한다.

경로의 `agg/`는 `services/aggregator/src/aggregator/`, `t/`는 `services/aggregator/tests/`, `ac/`는 `libs/analysis-client/`다.

## Phase 0: 준비

- [ ] T001 계약 잠금 3.14.0(`scripts/fetch-service-contracts.sh --update 3.14.0`). 이미 3.14.0이면 건너뛴다 — `service-contracts.lock`
- [ ] T002 [R-24] 단계 ID 6개(`aggregator.queue_wait`·`aggregator.handle`·`aggregate.read`·`compute`·`summary`·`store`) — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `vlm_contracts/stages.py`
- [ ] T003 [P] [R-1] `AnalysisCompleted`·`SourceCompleted` 모델(세션 null·`previousSourceId` null, 개수, 예시 왕복) — 테스트 `libs/contracts/tests/test_event_contracts.py`, 구현 `vlm_contracts/events.py`
- [ ] T004 [P] [R-20] 결과 파일 `SourceResult` 모델(필드·필수·enum을 `aggregates/v1/source-result`와 비교, 예시 왕복) — 테스트 `libs/contracts/tests/test_aggregate_contracts.py`, 구현 `vlm_contracts/aggregates.py`
- [ ] T005 재시도·마감 도우미를 ai-client에서 `libs/common`으로 옮김(동작 변경 없는 리팩터링, 기존 ai-client 테스트 통과로 확인) — 구현 `libs/common/src/vlm_common/http_call.py`, `libs/ai-client/src/vlm_ai_client/client.py`
- [ ] T006 `libs/analysis-client`·`services/aggregator` 패키지 뼈대(pyproject, 워크스페이스 등록, mypy·pytest 대상, `NotImplementedError` 인터페이스)
- [ ] T007 [R-3] analysis-client 생성 모델과 생성 코드 비교 CI에 `analysis-service.yaml` 추가 — 테스트 CI 비교 단계, 구현 `ac/src/vlm_analysis_client/models.py`(생성), 생성 스크립트·`.github/workflows/checks.yml`

## Phase 1: analysis-api 읽기 클라이언트

- [ ] T008 [R-3] 내부 operation 5개 호출: 경로, `X-Caller`·`X-Request-Deadline`·`traceparent`, `cursor`·`limit`, 응답 모델 — 테스트 `ac/tests/test_analysis_client.py`, 구현 `ac/src/vlm_analysis_client/client.py`
- [ ] T009 [R-22] 503·연결 실패만 2회 지수 대기 재시도, 다음 대기가 마감을 넘기면 마지막 오류 — 같은 테스트
- [ ] T010 [R-23] ProblemDetail → `AnalysisApiError(code, status)`, 시간 초과·연결 실패·계약과 맞지 않는 응답 코드 — 같은 테스트

## Phase 2: US-2 병합 (P1)

- [ ] T011 [R-21] 프로필 로딩과 버전 `<name>@<sha12>`, 기본값 — 테스트 `t/test_aggregator_profile.py`, 구현 `agg/profile.py`, `services/aggregator/profiles/aggregate-v1.toml`
- [ ] T012 [R-6] 같은 유형·같은 트랙(없으면 구역)·다른 청크·간격 이하면 한 묶음, 연쇄 병합, 같은 청크·chunkId null은 잇지 않음 — 테스트 `t/test_aggregator_merge.py`, 구현 `agg/merge.py`
- [ ] T013 [R-7] 대표 = 점수(null 최저) → startMs → eventId, 묶음 크기 2 이상만 출력 — 테스트 `t/test_aggregator_merge.py`

## Phase 3: US-1 상태·위험·노출·타임라인 (P1)

- [ ] T014 [R-11] 확정 대표만, 이벤트 시각(evidenceEnd ms, 없으면 endMs)·eventId 순, 구역 없으면 `global` — 테스트 `t/test_aggregator_state.py`, 구현 `agg/state.py`
- [ ] T015 [R-12] `afterState`가 있으면 스냅샷(이전 상태)과 구역 상태 변경 — 테스트 `t/test_aggregator_state.py`
- [ ] T016 [R-13] 같은 구역·유형 활성 위험 갱신 또는 새 위험(`riskReason` 없으면 무시) — 테스트 `t/test_aggregator_state.py`
- [ ] T017 [R-14] `riskDetected=false`면 해소, 활성 위험 없으면 변화 없음, 해소 뒤 재활성은 새 riskId — 테스트 `t/test_aggregator_state.py`
- [ ] T018 [R-16] 활성 시간, 구역·`global` 사람 트랙 노출 수·시간(반올림) — 테스트 `t/test_aggregator_exposure.py`, 구현 `agg/exposure.py`
- [ ] T019 [R-17] 타임라인 항목 종류·시각·`(atMs, refId)` 순서, 이어 온 위험은 활성 항목 없음 — 테스트 `t/test_aggregator_timeline.py`, 구현 `agg/timeline.py`
- [ ] T020 [R-20] 결정적 ID(source.completed, 병합 묶음, 규칙 이벤트, 스냅샷, 위험) — 테스트 `t/test_aggregator_ids.py`, 구현 `agg/ids.py`

## Phase 4: US-3 체류·배회 (P1)

- [ ] T021 [R-8] `hazard` 구역 사람 트랙 머문 구간이 기준 이상이면 `dwell`(정확히 기준값 포함, 사람 아닌 클래스·`aisle` 제외) — 테스트 `t/test_aggregator_rules.py`, 구현 `agg/rules.py`
- [ ] T022 [R-9] 창 안 진입 횟수 이상이면 `loiter` 하나, 마지막 진입 뒤부터 다음 창 — 테스트 `t/test_aggregator_rules.py`
- [ ] T023 [R-10] 규칙 이벤트 필드(`confirmed`·`rule`·템플릿 설명), 활성 위험 구간과 겹치면 high, 상태에 영향 없음 — 테스트 `t/test_aggregator_rules.py`

## Phase 5: US-4 세션 이어받기 (P2)

- [ ] T024 [R-15] 초기 활성 위험을 원래 riskId·`firstSeenMs: 0`·`carriedOver: true`로 시작, 이 영상에서 해소 가능 — 테스트 `t/test_aggregator_state.py`
- [ ] T025 [R-5] `previousSourceId`면 `final-state`를 초기 상태로, 404 `ANALYSIS_NO_ACTIVE_VERSION`이면 빈 상태·`fromSourceId: null` — 테스트 `t/test_aggregator_reader.py`, 구현 `agg/reader.py`

## Phase 6: 핸들러 (P1)

- [ ] T026 [R-3] 세 목록을 마지막 쪽까지, 구역 목록, 모든 호출에 같은 마감(핸들러 시작 + 설정값) — 테스트 `t/test_aggregator_reader.py`
- [ ] T027 [R-4] 이벤트·트랙 수가 `counts`와 다르면 발행 없이 재시도 가능 오류 — 테스트 `t/test_aggregator_reader.py`, `t/test_aggregator_service.py`
- [ ] T028 [R-1] `FULL`·`REANALYZE`면 결과 파일 저장과 `source.completed` 한 건(둘 다 계약 스키마 통과, 개수) — 테스트 `t/test_aggregator_service.py`, 구현 `agg/{service,compute,result}.py`
- [ ] T029 [R-2] `REEMBED`면 저장·발행 없이 건너뛴 이유 로그 — 테스트 `t/test_aggregator_service.py`
- [ ] T030 [R-20] 결과 파일 경로, 저장이 끝난 뒤 발행(저장 실패면 발행 없음) — 테스트 `t/test_aggregator_service.py`
- [ ] T031 [R-22] 재시도 소진·504·404 `ANALYSIS_JOB_NOT_FOUND`·저장소·발행 오류는 그대로 던짐 — 테스트 `t/test_aggregator_service.py`
- [ ] T032 [R-23] 400·계약과 맞지 않는 응답(`analysis`가 `VLMAnalysis`가 아님 포함)은 `NonRetryableError` — 테스트 `t/test_aggregator_service.py`
- [ ] T033 [R-21] 같은 입력 두 번(입력 순서를 섞어도) 요약을 뺀 결과 파일이 바이트 단위로 같음 — 테스트 `t/test_aggregator_compute.py`, 구현 `agg/result.py` `encode_result`
- [ ] T034 [R-24] 단계 블록 `aggregate.read`·`compute`·`summary`·`store`와 count — 테스트 `t/test_aggregator_service.py`

## Phase 7: US-5 요약 (P2)

- [ ] T035 [R-18] ai-gateway `source_summary` 등록(입력·출력 스키마, 자리 표시 프롬프트, `fallback.json`, 두 프로필, 테스트 도우미, Registry에 `_source-summary` 계약 파일) — 테스트 `services/ai-gateway/tests/test_prompts.py`, `test_analyze_api.py`, 구현 `registry/prompts/source_summary/`, 프로필, `registry_bundle.py`, `tests/conftest.py`, Dockerfile
- [ ] T036 [R-18] 확정 이벤트나 위험이 있으면 이미지 없이 한 번 호출(변수: 타임라인·위험·구역·영상 길이), 없으면 부르지 않고 `empty` — 테스트 `t/test_aggregator_summary.py`, 구현 `agg/summary.py`
- [ ] T037 [R-19] `modelId: null`이면 `not_configured`, 오류·대체 결과면 `failed`, 나머지 결과는 그대로 발행 — 테스트 `t/test_aggregator_summary.py`, `t/test_aggregator_service.py`

## Phase 8: 워커와 배포

- [ ] T038 기동 조립: 설정, 두 클라이언트, 저장소, 워커 — 테스트 `t/test_aggregator_main.py`, 구현 `agg/{__main__,settings}.py`
- [ ] T039 [NFR-4] [R-23] 워커 e2e: 정상 발행 뒤 커밋, 잘못된 메시지·400 DLQ, 503 소진 재시도 토픽 — 테스트 `t/test_aggregator_worker_e2e.py`
- [ ] T040 [NFR-3] 로그에 요약 문장·이벤트 설명 원문이 없음 — 테스트 `t/test_aggregator_service.py`
- [ ] T041 [NFR-1] 생성 입력(이벤트 2,000·트랙 5,000·머문 구간 20,000)으로 `compute` p95 5초 — 테스트 `t/test_aggregator_performance.py`
- [ ] T042 Dockerfile, README(환경변수 표), CI `service-images`에 aggregator runtime 빌드
- [ ] T043 [NFR-2] 로컬 compose(docs PR: aggregator 256M, 토픽 4개)와 analysis-api 자리 표시 서버(plan 5절 사용자 확인 뒤)로 성공 지표·메모리 측정, 아래에 측정 기록

## 측정 기록

(T043에서 채운다)

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, pytest, test-in-docker 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
