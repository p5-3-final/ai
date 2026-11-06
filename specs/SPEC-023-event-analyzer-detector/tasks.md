---
spec: SPEC-023
repo: ai
updated: 2026-11-06
---

# Tasks: SPEC-023 event-analyzer 탐지기·규칙 후보 판정과 event.manifest — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `ea/`는 `services/event-analyzer/src/event_analyzer/`, `t/`는 `services/event-analyzer/tests/`, `lc/`는 `libs/contracts/`다.

선행 조건: SPEC-021(ai-client 생성 모델, analyze 연결 실패만 재시도, `X-Request-Deadline`, event-analyzer 504 기록) 구현이 머지돼 있다. 아래 요청 모델 이름은 SPEC-021의 생성 모델을 따른다.

## Phase 0: 준비

- [ ] T001 [R-9] [R-13] 계약 잠금 3.14.0(`scripts/fetch-service-contracts.sh --update 3.14.0`)과 그 때문에 깨지는 계약 테스트 맞춤: `StageId` 전체 목록(`analyzer.merge` 포함), `EventAnalyzed`의 판정 방식 5종·`DetectorEvidence`·`judgments` 개수 규칙(`rule`은 0개, 그 밖은 1개 이상)·`rule*`이면 `detectorCandidate` 필수. 잠금이 이미 3.14.0이고 이 테스트가 통과하면 건너뛴다(SPEC-024 T001과 같은 태스크) — 테스트 `lc/tests/test_stage_contracts.py`, `lc/tests/test_event_contracts.py`, `lc/tests/test_analysis_contracts.py`, 구현 `service-contracts.lock`, `vlm_contracts/{stages,events}.py`
- [ ] T002 [R-14] `DetectorCandidate`·`DetectorInfo`·`EventCandidate`(`candidates`·`candidatesKey` 중 하나, `candidatesKey`가 메시지 실행 폴더의 `candidates.json`), `EventManifest`, `EVENT_MODELS` 등록. 근거 프레임 실행 폴더 검사는 plan의 사용자 확인 3 결과에 따른다 — 테스트 `lc/tests/test_event_contracts.py`(자동 계약 검사), `lc/tests/test_analysis_contracts.py`, 구현 `vlm_contracts/events.py`
- [ ] T003 [P] [R-1] 후보 파일 `CandidateFile` 모델 — 테스트 `lc/tests/test_candidate_file_contract.py`, 구현 `vlm_contracts/candidates.py`
- [ ] T004 `services/event-analyzer/pyproject.toml`에 `vlm-storage` 워크스페이스 의존 추가, `uv.lock` 갱신(새 외부 패키지 없음)
- [ ] T005 [R-16] 프로필 `EventAnalyzerProfile`: 쿨다운 기본 10초·0 이하 거부, 탐지 가능 유형 8종의 템플릿·기본 severity 필수, 애매 구간 0~1·시작 ≤ 끝, 템플릿 900자 이하. 자리값 `profiles/local.toml` — 테스트 `t/test_analyzer_profile.py`, 구현 `ea/profile.py`, `services/event-analyzer/profiles/local.toml`

## Phase 1: 규칙 (US-2·US-3, P1)

- [ ] T006 [P] [R-3] 쿨다운 병합: 같은 트랙·같은 유형, 간격 = 쿨다운이면 병합·초과면 분리, 트랙 null은 병합 안 함, 동점은 이른 시작·candidateId 순, 나머지 ID는 시작 시각순 — 테스트 `t/test_analyzer_merge.py`, 구현 `ea/merge.py`
- [ ] T007 [P] [R-4] 규칙 후보의 템플릿 `VLMAnalysis`(구역 이름 치환, 구역 null이면 프로필 자리값, 시각은 초) — 테스트 `t/test_analyzer_rules.py`, 구현 `ea/rules.py`
- [ ] T008 [P] [R-6] 재판정 조건에 애매 구간(경계 포함). 1차 실패·`unverified`면 애매해도 부르지 않음 — 테스트 `t/test_judgment.py`, 구현 `ea/judgment.py`
- [ ] T009 [R-7] `decide`가 판정 경로를 받아 `rule+vlm`·`rule+vlm+escalate`를 고름. 벡터 경로 기존 테스트 유지 — 테스트 `t/test_judgment.py`, 구현 `ea/judgment.py`

## Phase 2: 핸들러 (US-1·US-2·US-3, P1)

- [ ] T010 [R-8] `handle_detector`: 메시지 후보로 병합 → 규칙 확정 → VLM 판정 → 남은 후보마다 `event.analyzed`(계약 스키마 통과, eventId는 candidateId). 판정·실패 처리 코드를 벡터 경로와 공유하도록 정리(Refactor, 기존 테스트 유지) — 테스트 `t/test_analyzer_detector_service.py`, 구현 `ea/service.py`
- [ ] T011 [R-9] `detectorCandidate`(메시지 chunkId·executionId·`detectorProfile`, 후보 근거, `mergedCandidateIds`)와 `vectorCandidate` 없음 — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T012 [R-5] `event_judge` 요청: 근거 프레임 순서, 변수 `eventType`·`startSec`·`endSec`·`zoneName`·`evidence`(path `detector`, triggerClass, score, observationCount, detectorProfile) — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T013 [R-6] 1차 severity high, 애매 구간 점수면 같은 입력으로 `event_judge_escalate` — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T014 [R-10] 분석 발행 뒤 `event.manifest` 한 건(받은 수·합쳐진 수·발행 수·eventId 목록·`unsupportedEvents`·`detectorProfile`, eventId는 executionId) — 테스트 `t/test_analyzer_detector_service.py`, 구현 `ea/{service,ids}.py`
- [ ] T015 [R-11] 후보 0개면 `event.analyzed` 없이 `candidateCount: 0`인 manifest만 — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T016 [R-12] 동시 판정 수 상한 — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T017 [R-13] 단계 블록 `analyzer.merge`(받은 후보 수), `analyzer.judge`(VLM 후보 수, 없으면 블록 없음), `analyzer.escalate` — 테스트 `t/test_analyzer_detector_service.py`

## Phase 3: 큰 묶음·실패 (US-4, P2)

- [ ] T018 [R-1] `candidatesKey` 파일의 후보를 메시지 후보와 같이 처리(메모리 저장소) — 테스트 `t/test_analyzer_candidates.py`, 구현 `ea/candidates.py`
- [ ] T019 [R-2] 파일 없음·잘못된 JSON·계약 불일치·후보 수 불일치·식별자 불일치면 아무것도 발행하지 않고 `NonRetryableError`. 그 밖의 저장소 오류는 그대로 던짐 — 테스트 `t/test_analyzer_candidates.py`, `t/test_analyzer_detector_service.py`
- [ ] T020 [R-7] VLM 후보 실패 처리: 1차 실패 `unknown`과 실패 코드, 재판정 실패면 1차 결과, `AI_IMAGE_NOT_FOUND`·504·시간 초과 기록, 그 밖의 4xx면 분석·manifest 모두 발행 없이 `NonRetryableError` — 테스트 `t/test_analyzer_detector_service.py`
- [ ] T021 [R-15] 발행 오류는 그대로 던지고, 다시 처리하면 같은 eventId로 분석·manifest 발행 — 테스트 `t/test_analyzer_detector_service.py`

## Phase 4: 워커와 배포

- [ ] T022 [R-16] 기동 조립: 프로필 경로·저장소 설정, 프로필 검증 실패면 기동 실패, `event.candidate` route 추가 — 테스트 `t/test_analyzer_main.py`, 구현 `ea/{__main__,settings}.py`
- [ ] T023 [R-14] [R-2] [NFR-2] 워커 e2e: `event.candidate` 정상 발행(분석 + manifest), 잘못된 메시지·없는 후보 파일 DLQ, 긴 판정 중 커밋(구현 뒤 작성, Red 없음) — 테스트 `t/test_analyzer_worker_e2e.py`
- [ ] T024 Dockerfile(`profiles` 복사, `EVENT_ANALYZER_PROFILE_PATH`), README(경로·환경변수·프로필 표), 레포 `.env.example`
- [ ] T025 [NFR-1] 로컬 compose 측정: VLM 후보 50개 메시지 10초 안, 성공 지표 묶음(VLM 3, 규칙 1, 병합될 후보 2) → 분석 4건·`mergedCount: 2`(docs compose PR 뒤)

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, `pytest -m "not ffmpeg and not model"`, `scripts/test-in-docker.sh event-analyzer` 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
