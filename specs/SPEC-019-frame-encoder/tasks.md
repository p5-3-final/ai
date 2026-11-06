---
spec: SPEC-019
repo: ai
updated: 2026-11-06
---

# Tasks: SPEC-019 frame-encoder — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- 완료한 태스크는 `[x]`로 바꾼다.

경로의 `fe/`는 `services/frame-encoder/src/frame_encoder/`, `t/`는 `services/frame-encoder/tests/`다.

## Phase 0: 준비

- [x] T001 계약 잠금 2.10.0, 단계 ID 6개 — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `vlm_contracts/stages.py` (잠금을 올리며 드러난 media 계약 선택 필드 sessionId·role·retention도 모델에 맞춤)
- [x] T002 [R-16] `SegmentReady`·`SegmentEmbedded`·`EventCandidateVector` 모델, 경로가 실행 폴더 `blurred/` 밖이면 거절 — 테스트 `libs/contracts/tests/test_event_contracts.py`, `test_segment_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T003 [R-3] `VectorFile` 모델과 분류 체계 유형 목록이 계약과 일치 — 테스트 `libs/contracts/tests/test_vector_file_contract.py`, 구현 `vlm_contracts/{vectors,taxonomy}.py` (유형 목록은 T002에서 정의해 처음부터 통과)
- [x] T004 인코더 프로필·로드를 `libs/encoders/loader.py`로 옮김(리팩터링, ai-gateway 기존 테스트 통과)
- [x] T005 frame-encoder 패키지 뼈대(pyproject, 워크스페이스 등록, mypy 대상)

## Phase 1: US-1·US-2 구간·크롭 벡터 (P1)

- [ ] T006 [R-1] 구간 벡터 = 4장 정규화 평균을 다시 정규화 — 테스트 `t/test_vectors.py`, 구현 `fe/vectors.py`
- [ ] T007 [R-1] [R-2] [R-3] [R-4] [R-7] [R-8] 핸들러: 벡터 파일 저장과 `segment.embedded` — 테스트 `t/test_service.py`, 구현 `fe/{service,ids}.py`
- [ ] T008 [R-5] 같은 메시지를 다시 처리하면 같은 eventId·키 — 테스트 `t/test_service.py`, 구현 `fe/ids.py`
- [ ] T009 [R-6] 구간·크롭이 없으면 빈 파일과 이벤트 — 테스트 `t/test_service.py`
- [ ] T010 [R-17] 이미지가 없거나 디코딩할 수 없으면 `NonRetryableError`, 출력 없음 — 테스트 `t/test_service.py`
- [ ] T011 [R-18] 저장소 일시 오류는 그대로 던짐 — 테스트 `t/test_service.py`
- [ ] T012 [R-19] 단계 블록 `encoder.segments`·`crops`·`store` — 테스트 `t/test_service.py`

## Phase 2: US-3 프로토타입 후보 (P2)

- [ ] T013 [R-15] 프로필: 유형이 분류 체계 v2 밖·문장 없음·임계값 범위 밖이면 거절 — 테스트 `t/test_profile.py`, 구현 `fe/profile.py`
- [ ] T014 [R-10] [R-11] 프로토타입 벡터와 후보(점수 ≥ 임계값) — 테스트 `t/test_prototypes.py`, 구현 `fe/prototypes.py`
- [ ] T015 [R-12] [R-14] 후보가 있으면 `segment.embedded` 뒤에 `event.candidate.vector` 1건(계약 스키마 통과) — 테스트 `t/test_service.py`
- [ ] T016 [R-13] 프로토타입이 없거나 후보 0개면 발행 안 함 — 테스트 `t/test_service.py`
- [ ] T017 [R-19] 단계 블록 `encoder.prototype` — 테스트 `t/test_service.py`

## Phase 3: 워커와 배포

- [ ] T018 [R-9] 기동 조립: 프로필 읽기, 인코더 로드(manifest 불일치면 실패) — 테스트 `t/test_main.py`, 구현 `fe/{__main__,settings}.py`
- [ ] T019 [R-16] [NFR-1] [NFR-3] 워커 e2e: 정상 처리, 잘못된 메시지 DLQ — 테스트 `t/test_worker_e2e.py`
- [ ] T020 Dockerfile, 로컬 프로필, README, `scripts/test-in-docker.sh` 대상
- [ ] T021 [NFR-2] 실제 모델 테스트(`-m model`)와 로컬 compose 측정

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, pytest, test-in-docker 통과
