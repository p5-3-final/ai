# embedding-worker

`event.analyzed`·`track.analyzed`·`segment.annotated`를 소비 그룹 하나(`embedding-worker`)로 받아
항목마다 문장 하나를 만들고, ai-gateway `/v1/embed`(`embed_text`, 문장 1개)로 벡터를 만든다. 벡터는
텍스트 벡터 파일로 저장하고 `embedding.ready`로 경로를 알린다. 문장이 없는 항목은 호출 없이 건너뛴다.

- spec: docs `docs/specs/SPEC-025-embedding-worker/spec.md`, plan: `specs/SPEC-025-embedding-worker/plan.md`
- 계약: docs `service-contracts/asyncapi/embedding-worker.yaml`, `events/embedding.ready.schema.json`,
  `events/segment.annotated.schema.json`, `vectors/v1/text-vector-file.schema.json`,
  `tracks/v1/track-mapping.schema.json`(트랙 항목의 시각·클래스)
- ai-gateway 호출은 `libs/ai-client`(REST)로 한다. ai-gateway 프로필의 `[callers] allowed`에
  `embedding-worker`가 있고 `embed_text` 모델이 설정돼 있어야 한다. 없으면 모든 메시지가
  `AI_MODEL_NOT_CONFIGURED`로 DLQ에 간다.

| itemKind | 문장(textFormat) | 파일 metadata |
| --- | --- | --- |
| `event` | 설명문·위험 사유·조치 제안 중 값이 있는 것을 줄바꿈으로 연결(`event-v1`) | 이벤트 `startMs`·`endMs`, 유형, severity, verificationStatus |
| `track` | reason event·click, READY, confirmed인 속성 설명(`track-v1`) | 번역표의 `startMs`·`endMs`·className, trackId, 보호구·역할 |
| `segment_annotation` | 주석 문장(`segment_annotation-v1`) | `startMs`·`endMs`, segmentId |

파일 경로는 `sources/<sourceId>/jobs/<jobId>/text-vectors/<itemKind>/<itemId>.json`이다. 경로에 모델이
없어 같은 항목을 다른 모델로 다시 임베딩하면 마지막 결과로 덮어쓰고 eventId만 달라진다.

## 실행

```bash
uv run --package embedding-worker python -m embedding_worker
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `EMBEDDING_WORKER_AI_GATEWAY_URL` | (필수) | ai-gateway 주소(예: `http://ai-gateway:8000`) |
| `EMBEDDING_WORKER_CALL_TIMEOUT_S` | `30` | 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값을 `X-Request-Deadline`으로 싣는다(SPEC-021 R-8) |
| `EMBEDDING_WORKER_MAX_RETRIES`, `EMBEDDING_WORKER_RETRY_BASE_DELAY_S` | `2`, `1.0` | 503·연결 실패의 클라이언트 재시도 횟수와 첫 대기(두 배씩 는다, SPEC-021 R-10) |
| `EMBEDDING_WORKER_MAX_CHARS` | `2000` | 문장 최대 글자 수(1~2000). 넘으면 앞에서부터 자른다(R-5) |
| `EMBEDDING_WORKER_TRACK_MAPPING_CACHE` | `8` | 메모리에 둘 Job별 트랙 번역표 수 |
| `EMBEDDING_WORKER_GROUP_ID` | `embedding-worker` | 소비 그룹. 재시도 `<토픽>.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `STORAGE_*` | | 저장소 설정(`vlm_storage.s3.StorageSettings`). 번역표 읽기와 벡터 파일 쓰기 |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013). 단계 `embedding.call`·`embedding.store` |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 재시도 토픽: 재시도 끝의 503·연결 실패·시간 초과, 429 `AI_RATE_LIMITED`, 502·504·그 밖의 5xx,
  저장소·Kafka 오류, 번역표가 아직 없음
- DLQ(재시도 없음): `AI_MODEL_NOT_CONFIGURED`, `AI_BUDGET_EXCEEDED`, 그 밖의 4xx, 계약과 맞지 않는
  응답·벡터 수·길이, 계약과 맞지 않는 메시지, 번역표에 없는 trackId
- 문장 원문과 벡터는 로그에 남기지 않는다.

## 테스트

```bash
uv run pytest services/embedding-worker -m "not integration"   # 단위·서비스
uv run pytest services/embedding-worker                        # Kafka e2e 포함(Docker 필요)
scripts/test-in-docker.sh embedding-worker                     # 서비스 이미지 안에서 전체
```
