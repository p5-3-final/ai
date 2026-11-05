---
spec: SPEC-014
repo: ai
updated: 2026-11-05
---

# Tasks: SPEC-014 — ai 경로의 backend 소유 worker

## Phase 1: 준비

- [x] T001 계약 2.5.1 고정·uv 멤버·worker DB versioned SQL

## Phase 2: 실행과 종료

- [x] T002 [R-2 R-3] 실제 DB 점유·중복/입력 충돌 — store.py / test_service.py
- [x] T003 [R-4 R-5 R-6] 기한·성공·확실한 실패·종료/Outbox — service.py / test_service.py
- [x] T004 [R-7 R-8] 불명 RUNNING·저장 실패·재발행 — store/service / test_service.py
- [x] T005 [NFR-2] 실제 FFmpeg 구간·시간 매핑·test-only adapter — processor.py / test_processor.py

## Phase 3: 마무리

- [x] T006 필수 lint/format/type/pytest·이미지 및 실제 backend 연동
- [x] T007 README·환경변수·검사 예외 근거 및 검증 결과 정리

## 완료 조건 (Definition of Done)

- [x] 담당 요구사항 검증·필수 검사 통과
- [x] 실제 consumer/Outbox/추출과 AI 대역·불명 실행 제한을 구분
- PR·CI·머지는 GitHub 실제 상태로 확인한다. 운영 배포 전 Done 금지.

## TDD 기록

초기 service.handle 미구현 상태에서 종료 Outbox expected1/actual0 실패를 확인했고 구현 후 실제 PostgreSQL 테스트가 통과했다. 실패·동시·재시작 경계는 이어서 검증한다. normalize R-1은 별도 ai #32로 선행 수정·머지됐다.

실제 Kafka 연결에서 producedAt 덮어쓰기 Red를 확인했다. docs #89 설명 보완 후 공통 prepare_event로 시각을 먼저 저장하고 Publisher는 기존 값을 보존한다. 같은 eventId 전체 입력 비교는 유지한다.

검증 결과: ruff/check·format·mypy(123 source files) 통과, 필수 pytest 478개 통과(기존 모델/별도 ffmpeg 대상 14개는 표준 marker 분리), normalize 이미지 전체 65개 및 chunk-processor 이미지 전체 13개 통과. backend 실제 연결 테스트와 전체 506개/Pipeline158개가 통과했다. 모델·프롬프트 변경은 없으며 model suite 대상이 아니다. 실제 운영 기동과 추론 연결 완료로 기록하지 않는다.

검사 예외: processor.py의 subprocess argv 호출과 test_processor.py의 두 ffmpeg/ffprobe 호출에 S603만 한 줄 단위로 허용했다(#33). 입력은 코드가 생성한 argv/UUID 경로·숫자 구간이고 shell=False다. 기존 검사 기준을 낮추거나 전체 규칙을 끄지 않았다.
