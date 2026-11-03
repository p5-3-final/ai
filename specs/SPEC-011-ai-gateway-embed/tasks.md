---
spec: SPEC-011
repo: ai
updated: 2026-11-03
---

# Tasks: SPEC-011 ai-gateway 벡터 생성 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `gw/`는 `services/ai-gateway/src/ai_gateway/`, `t/`는 `services/ai-gateway/tests/`, `enc/`는 `libs/encoders/src/vlm_encoders/`, `et/`는 `libs/encoders/tests/`다.

## Phase 1: 준비

- [x] T001 `libs/encoders` 멤버, open_clip_torch·torch·torchvision(Linux는 PyTorch CPU 인덱스), 마커 `model`, `ImageTextEncoder` Protocol·오류 — 루트 `pyproject.toml`, `libs/encoders/pyproject.toml`, `enc/base.py` (7d56af8)
- [x] T002 호출 로그 마이그레이션 `0002`(source_id·prompt_version NULL 허용, input_count) — `gw/db/`, 테스트 `t/test_migrations.py`(PostgreSQL) (42f8e42)

## Phase 2: 인코더 라이브러리 (US-1, US-2, US-4)

- [x] T003 [P] [R-15] [R-16] 가중치 받기와 sha256 확인(불일치·없음이면 오류) — 테스트 `et/test_weights.py`, 구현 `enc/weights.py` (2def525)
- [x] T004 [R-18] 캐시에 해시가 같은 파일이 있으면 다시 받지 않음 — 테스트 `et/test_weights.py`, 구현 `enc/weights.py` (2def525)
- [x] T005 [P] [R-6] 모델 ID 해석(`open_clip:<아키텍처>:<가중치>:<집계>`, 잘못된 ID 거부) — 테스트 `et/test_model_id.py`, 구현 `enc/model_id.py` (0498803)
- [x] T006 [P] [R-4] [R-5] mock 인코더(결정적·길이 1) — 테스트 `et/test_mock_encoder.py`, 구현 `enc/mock.py` (0498803)
- [x] T007 [R-17] open_clip 인코더 로드와 차원 확인(무작위 초기화 가중치 파일), 전처리 값이 pretrained 설정과 같음 — 테스트 `et/test_open_clip.py`, 구현 `enc/open_clip.py` (4906d6b)
- [x] T008 [R-4] [R-5] open_clip 인코더의 문장·이미지 벡터가 길이 1이고 같은 입력이면 같음 — 테스트 `et/test_open_clip.py`, 구현 `enc/open_clip.py` (4906d6b)
- [x] T009 [R-12] 디코딩할 수 없는 이미지면 `ImageDecodeError` — 테스트 `et/test_open_clip.py`, 구현 `enc/open_clip.py` (4906d6b)

## Phase 3: 프로필 (US-4)

- [x] T010 [R-14] `[encoder]`·`[embed.*]` 프로필 스키마와 검증(인코더 없으면 인코더 taskType 미설정, Adapter 참조 확인) — 테스트 `t/test_profile.py`, 구현 `gw/profile.py` (21322f6)
- [x] T011 [R-24] mock 허용 없이 mock 인코더를 고르면 기동 실패 — 테스트 `t/test_profile.py`, 구현 `gw/profile.py` (21322f6)

## Phase 4: US-1·US-2 시각 벡터 (P1)

- [x] T012 [R-1] `embed_visual_text` → 인코더 텍스트 벡터, 호출 ID·모델 ID·차원 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py`, `gw/api.py` (013f049)
- [x] T013 [R-6] 인코더 taskType 응답 모델 ID가 프로필의 인코더 모델 ID 그대로 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T014 [R-2] `embed_image` → 저장소에서 읽어 이미지 벡터 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T015 [R-10] `embed_image` 경로가 taskType 패턴 밖이면 403 — 테스트 `t/test_images.py`, `t/test_embed_api.py`, 구현 `gw/images.py` (013f049)
- [x] T016 [R-11] 저장소에 없는 이미지면 400 `AI_IMAGE_NOT_FOUND` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T017 [R-12] 디코딩할 수 없는 이미지면 400 `AI_IMAGE_UNREADABLE` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py`, `gw/errors.py` (013f049)

## Phase 5: 입력 검사 (US-1, US-3)

- [x] T018 [R-7] 입력 종류가 taskType과 다르거나 빈 목록·빈 문장이면 400 `AI_INVALID_REQUEST` — 테스트 `t/test_embed_api.py`, 구현 `gw/api.py`, `gw/embed.py` (013f049)
- [x] T019 [R-8] 입력 개수 상한 초과면 400 `AI_TOO_MANY_INPUTS` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py`, `gw/errors.py` (013f049)
- [x] T020 [R-26] 문장 길이 상한 초과면 400 `AI_INVALID_REQUEST` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T021 [R-9] 모르는 taskType이면 400 `AI_UNKNOWN_TASK_TYPE` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T022 [R-13] 호출자 헤더가 없거나 허용 목록 밖이면 403 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T023 [R-14] taskType 모델이 프로필에 없으면 503 `AI_MODEL_NOT_CONFIGURED`, 로그 `unavailable` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py`, `gw/errors.py` (013f049)

## Phase 6: US-3 텍스트 벡터 (P1)

- [x] T024 [R-3] openai Adapter `embed`(입력 순서 유지, 토큰 수) — 테스트 `t/test_openai_adapter.py`, 구현 `gw/adapters/base.py`, `gw/adapters/openai.py`, `gw/adapters/mock.py` (013f049)
- [x] T025 [R-3] [R-4] `embed_text` → Adapter 벡터를 정규화해 반환 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T026 [R-19] 공급자 벡터 차원·개수가 다르면 502, 로그 `invalid_output` — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (013f049)
- [x] T027 [R-20] 시간 초과·연결 불가·429·5xx면 503 `AI_PROVIDER_UNAVAILABLE`, 로그 `unavailable` — 테스트 `t/test_openai_adapter.py`, `t/test_embed_api.py`, 구현 `gw/adapters/openai.py`, `gw/embed.py` (013f049)
- [x] T028 [R-21] 429 외 4xx면 502 `AI_PROVIDER_FAILED`, 로그 `failed` — 테스트 `t/test_openai_adapter.py`, `t/test_embed_api.py`, 구현 `gw/adapters/openai.py`, `gw/embed.py` (013f049)

## Phase 7: US-5·US-6 가용성과 호출 로그 (P2·P3)

- [x] T029 [R-22] VLM·텍스트 임베딩 Adapter가 실패해도 `embed_visual_text`가 동작 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (64f0b84)
- [x] T030 [R-23] 임베딩 호출 로그(입력 수·지연·상태·토큰, 벡터·문장 원문 없음) — 테스트 `t/test_call_log.py`(PostgreSQL), `t/test_embed_api.py`, 구현 `gw/call_log.py`, `gw/embed.py` (64f0b84)
- [x] T031 [NFR-3] 인코딩을 스레드에서 실행해 처리 중에도 `/health` 1초 안 응답 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (64f0b84)
- [x] T032 [NFR-5] 로그에 문장 원문·벡터가 없음 — 테스트 `t/test_embed_api.py`(caplog), 구현 `gw/embed.py` (64f0b84)

## Phase 8: 기동 조립 (US-4)

- [x] T033 [R-15] [R-16] [R-25] `build_production`: 모델 버킷에서 가중치 받기 → 로드 → 준비. 해시가 다르면 기동 실패 — 테스트 `t/test_startup.py`, `t/test_startup_e2e.py`(SeaweedFS·PostgreSQL·Kafka), 구현 `gw/main.py`, `gw/settings.py` (7a473a8)
- [x] T034 [NFR-4] [NFR-6] open_clip·torch import는 `libs/encoders`의 구현 안에서만, 이미지에 가중치 없음 — 테스트 `t/test_architecture.py`, 구현 `services/ai-gateway/Dockerfile`, `pyproject.toml` (5ee07b5)

## Phase 9: 마무리

- [x] T035 프로필(`local.toml`: MobileCLIP2-S0·Ollama `bge-m3`, `local-mock.toml`: mock), `AGENTS.md`, `.env.example` 갱신 — `services/ai-gateway/profiles/`, `AGENTS.md`, `.env.example` (9a5b824)
- [x] T036 [NFR-1] [NFR-2] 실제 가중치로 지연과 성공 지표 확인(마커 `model`) — 테스트 `t/test_embed_latency.py` (9a5b824)
- [ ] T037 docs 레포: 로컬 compose 모델 버킷·가중치 올리기 절차, Ollama `bge-m3` 안내(별도 PR) 후 로컬 스택에서 확인
