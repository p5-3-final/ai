---
spec: SPEC-026
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-026 grounding-worker 검색 상위 구간 bbox 주석 클립 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- `[확인]`은 plan 5·7절의 사용자 확인 결과에 따라 내용이 바뀌는 태스크다. 탐지기 라이브러리가 정해지기 전에는 Phase 5를 시작하지 않는다.
- 완료한 태스크는 `[x]`로 바꾼다.

경로의 `gw/`는 `services/grounding-worker/src/grounding_worker/`, `t/`는 `services/grounding-worker/tests/`다.

## Phase 0: 준비

- [x] T001 계약 잠금을 3.14.0으로 올림(`scripts/fetch-service-contracts.sh --update 3.14.0`, 이미 3.14.0 이상이면 건너뜀), 단계 ID 6개 — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `service-contracts.lock`, `vlm_contracts/stages.py`
- [x] T002 [P] [R-15] `GroundingRequested` 모델(`normalizedKey`가 sourceId·normalizationId 폴더가 아니면 검증 실패) — 테스트 `libs/contracts/tests/test_grounding_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T003 [P] [R-7] [R-9] `GroundingCompleted`(READY·FAILED 필드 조합), `GroundingErrorCode` — 테스트 `libs/contracts/tests/test_grounding_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T004 [P] [R-6] [R-17] `GroundingResultFile` — 테스트 `libs/contracts/tests/test_grounding_result_contract.py`, 구현 `vlm_contracts/groundings.py`
- [x] T005 [P] `ObjectStorage.presigned_get_url`(S3·메모리 구현) — 테스트 `libs/storage/tests/test_storage_presign.py`, 구현 `vlm_storage/{storage,s3,memory}.py`
- [x] T006 `services/grounding-worker` 패키지 뼈대(pyproject, 워크스페이스 `members`·mypy `files`·`mypy_path`·pytest `pythonpath` 등록, `gw_support.py`, 인터페이스만 둔 모듈), Dockerfile에 ffmpeg·글꼴(test·runtime)

## Phase 1: 탐지기 인터페이스와 프로필

- [x] T007 [R-17] 프로필 `[detector]`·`[render]` 검증, mock은 허용 설정 없으면 기동 실패 — 테스트 `t/test_grounding_profile.py`, 구현 `gw/profile.py`, `profiles/local-mock.toml`
- [x] T008 [R-3] `GroundingDetector`·`Detection`·`RgbFrame`·`DetectorError`, 결정적 `MockDetector` — 테스트 `t/test_grounding_detector.py`, 구현 `gw/detector.py`

## Phase 2: US-1 검색 결과에서 대상 위치 보기 (P1)

- [x] T009 [P] [R-2] 샘플 시각: startMs부터 간격마다, `< endMs`, 최대 12장 — 테스트 `t/test_grounding_sampling.py`, 구현 `gw/sampling.py`
- [x] T010 [P] [R-5] 대상어 색 = UTF-8 sha256 앞 3바이트 `#rrggbb` — 테스트 `t/test_grounding_render.py`, 구현 `gw/render.py`
- [x] T011 [R-4] 샘플별 표시 구간(이웃 샘플 중간점)과 `drawbox`·`drawtext` 필터 스크립트(이스케이프 포함) — 테스트 `t/test_grounding_render.py`, 구현 `gw/render.py`
- [x] T012 [P] [R-9] eventId = uuid5(groundingId·groundingModel), 클립·결과 경로 — 테스트 `t/test_grounding_ids.py`, 구현 `gw/ids.py`, `gw/keys.py`
- [x] T013 [R-2] `FfmpegTool` probe와 샘플 프레임 rgb24 추출 — 테스트 `t/test_grounding_media.py`(`ffmpeg` 마커), 구현 `gw/media.py`
- [x] T014 [R-4] `FfmpegTool` 클립 렌더링: H.264, 원본 fps, 오디오 유지, 박스 위치 픽셀 확인 — 테스트 `t/test_grounding_media.py`(`ffmpeg` 마커), 구현 `gw/media.py`
- [x] T015 [R-1] [R-6] 핸들러: 구간 순서대로 처리, 클립 → 결과 파일 순서로 저장, 구간마다 READY 발행(계약 스키마 통과) — 테스트 `t/test_grounding_service.py`, 구현 `gw/service.py`
- [x] T016 [R-3] 기준 신뢰도 미만 탐지는 결과에서 뺌 — 테스트 `t/test_grounding_service.py`
- [x] T017 [R-7] `frames`에 샘플마다 모든 대상어 수(없으면 0) — 테스트 `t/test_grounding_service.py`
- [x] T018 [R-17] 결과 파일 `groundingModel`·`scoreThreshold`·`sampleFps`, 이벤트 `groundingModel` — 테스트 `t/test_grounding_service.py`
- [x] T019 [R-18] 단계 블록 `grounding.sample`·`detect`·`render`·`store` — 테스트 `t/test_grounding_service.py`

## Phase 3: US-2 같은 주석을 다시 만들지 않음 (P2)

- [x] T020 [R-8] 결과 파일이 있으면 탐지·인코딩 없이 그 내용으로 같은 eventId 발행 — 테스트 `t/test_grounding_service.py`, 구현 `gw/service.py`
- [x] T021 [R-14] 저장·발행 오류는 그대로 던지고, 다시 처리하면 끝난 구간은 재사용 — 테스트 `t/test_grounding_service.py`

## Phase 4: US-3 실패를 알림 (P2)

- [x] T022 [R-10] `endMs <= startMs`이거나 영상 길이를 넘으면 `GROUNDING_SEGMENT_OUT_OF_RANGE` FAILED — 테스트 `t/test_grounding_service.py`
- [x] T023 [R-11] 원본 없음·probe·디코딩 실패면 `GROUNDING_SOURCE_UNREADABLE` FAILED — 테스트 `t/test_grounding_service.py`, `t/test_grounding_media.py`(`ffmpeg` 마커, 깨진 파일)
- [x] T024 [R-12] `DetectorError`면 `GROUNDING_DETECTOR_FAILED` FAILED — 테스트 `t/test_grounding_service.py`
- [x] T025 [R-13] 구간 제한 시간(기본 120초)을 넘으면 멈추고 `GROUNDING_TIMEOUT` FAILED, ffmpeg 프로세스 종료 — 테스트 `t/test_grounding_service.py`, `t/test_grounding_media.py`(`ffmpeg` 마커), 구현 `gw/service.py`, `gw/media.py`

## Phase 5: 실제 탐지기 (사용자 확인 뒤)

- [ ] T026 [확인] [R-3] 결정한 라이브러리 추가(`uv add`, 이유를 plan에 기록)와 탐지기 구현(대상어를 텍스트 클래스로, 라이브러리 오류 → `DetectorError`) — 테스트 `t/test_grounding_model.py`(`model` 마커), 구현 `gw/detector_<라이브러리>.py`
- [ ] T027 [확인] [R-17] 가중치 manifest로 받기(`fetch_model`), 버전 = manifest version, `local.toml` 자리값 — 테스트 `t/test_grounding_profile.py`, 구현 `gw/detector.py` `load_detector`, `profiles/local.toml`
- [ ] T028 [확인] runtime 이미지에 라이브러리 반영과 크기 확인 — `services/grounding-worker/Dockerfile`

## Phase 6: 워커와 배포

- [x] T029 기동 조립: 설정, 프로필, 탐지기 로드(기동 때 한 번), 저장소, `route`, health, 측정 발행 — 테스트 `t/test_grounding_main.py`, 구현 `gw/{__main__,settings}.py`
- [x] T030 [R-15] [R-16] [NFR-3] 워커 e2e(Kafka·SeaweedFS testcontainer, mock 탐지기, 서비스 이미지의 ffmpeg): 정상 발행, 재사용, 잘못된 메시지·`normalizedKey` 불일치 DLQ, 긴 처리 중 커밋 — 테스트 `t/test_grounding_worker_e2e.py`(`ffmpeg` 마커, 구현 뒤 작성이면 PR에 "Red 없음"으로 적음)
- [x] T031 README, 레포 `.env.example`, CI `service-images` 이미지 빌드·ffmpeg 테스트(`.env.example`은 에이전트 권한 설정으로 수정하지 못해 README 환경변수 표로 대신했다)
- [x] T032 docs `local/`: compose 서비스(1.5G), 토픽 `grounding.requested`·`grounding.completed`·재시도·`grounding-worker.dlq`, `local/.env.example`, `local/README.md`(docs 레포 PR) — 완료: docs #142(로컬 compose에 서비스 1.5G·토픽·DLQ, README 메모리 표)
- [ ] T033 [확인] [NFR-1] [NFR-2] 로컬 compose 측정(CPU, 1080p 30fps 10초 구간 p95 30초, 메모리 1.5GB). 실제 탐지기 결정 뒤. 결과는 아래 "측정 기록"에만 짧게 적음

## 측정 기록

(T033에서 채운다)

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, pytest(`not ffmpeg and not model`), `scripts/test-in-docker.sh grounding-worker` 통과. 실제 탐지기를 더했으면 `pytest -m model`
- [ ] plan.md와 실제 구현이 일치함(`[확인]` 태스크는 결정 결과를 plan에 반영)
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
