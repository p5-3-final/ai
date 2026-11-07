# aggregator

job 하나의 판정 이벤트를 병합하고 위험 상태 전이·노출·체류·배회·타임라인·요약을 계산해 결과 파일
`source-result.json`을 저장하고 `source.completed`를 발행한다. 실시간(STREAM) 세션은 창마다
상태를 접어(fold) `state.updated`를 내고, 세션이 끝나면 창 기록으로 같은 결과 파일을 만든다.

- spec: docs `docs/specs/SPEC-027-aggregator/spec.md`, plan: `specs/SPEC-027-aggregator/plan.md`
- 계약: docs `service-contracts/asyncapi/aggregator.yaml`, `events/source.completed.schema.json`,
  `events/state.updated.schema.json`, `aggregates/v1/source-result.schema.json`
- 읽기: analysis-api 내부 API(`libs/analysis-client`), 실시간 구역은 stream-service 내부 API
  (`libs/stream-client`), 요약·재판정은 ai-gateway(`libs/ai-client`, taskType `source_summary`·
  `event_judge`). ai-gateway 프로필의 `[callers] allowed`에 `aggregator`가 있어야 한다.

## 실행

```bash
uv run --package aggregator python -m aggregator
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `AGGREGATOR_AI_GATEWAY_URL` | (필수) | ai-gateway 주소(예: `http://ai-gateway:8000`) |
| `AGGREGATOR_ANALYSIS_API_URL` | (필수) | analysis-api 내부 주소(예: `http://analysis-api:8085`) |
| `AGGREGATOR_STREAM_SERVICE_URL` | (없음) | stream-service 내부 주소. 비우면 STREAM 메시지는 재시도 뒤 DLQ로 간다 |
| `AGGREGATOR_REDIS_URL` | (없음) | 대기 집합·세션 상태·재판정 계수용 Redis. 운영 필수다. 비우면 업로드 기본 경로만 동작한다 |
| `AGGREGATOR_PROFILE_PATH` | 이미지 `/app/profiles/aggregate-v1.toml` | 집계 프로필(병합 간격, 체류·배회 기준, 재판정 상한) |
| `AGGREGATOR_READ_TIMEOUT_S` | `300` | 메시지 하나의 내부 읽기 전체 마감(R-3) |
| `AGGREGATOR_READ_CALL_TIMEOUT_S` | `30` | 읽기 시도 하나의 제한 시간 상한 |
| `AGGREGATOR_PAGE_LIMIT` | `1000` | 쪽 크기(계약 상한 1000) |
| `AGGREGATOR_MAX_RETRIES`, `AGGREGATOR_RETRY_BASE_DELAY_S` | `2`, `1.0` | 503·연결 실패 재시도 횟수와 첫 대기(SPEC-021 R-10) |
| `AGGREGATOR_GATEWAY_TIMEOUT_S` | `180` | ai-gateway 호출 하나의 제한 시간 |
| `AGGREGATOR_MAX_ATTEMPTS` | `5` | 메시지 처리 최대 시도 수. 마지막 시도에서도 개수가 맞지 않으면 `partial`로 발행한다(R-4) |
| `AGGREGATOR_GROUP_ID` | `aggregator` | 소비 그룹. 재시도 `<토픽>.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_PATH_STYLE` | (필수), -, `us-east-1`, `false` | 결과·스냅샷 저장소. 자격 증명은 boto3 기본 체인을 쓴다 |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013) |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

소비 토픽은 `analysis.completed`, `event.analyzed`, `track.detected`, `chunk.manifest` 4개이고 발행
토픽은 `source.completed`, `state.updated`다.

## 동작

- 업로드 job: `analysis.completed`(scope job)를 받으면 analysis-api에서 이벤트·트랙·머문 구간·구역과
  직전 영상의 끝 상태를 읽는다. 직전 영상 `final-state`가 404면 빈 초기 상태로 시작한다.
  읽은 개수가 메시지 `counts`와 다르면 재시도하고, 마지막 시도면 `partial`과 `missingCounts`를 싣는다.
- 재분석(`REEMBED`) job은 결과를 다시 계산하지 않고 건너뛴다.
- 대기 집합: 판정 방식이 `vector*`인 `event.analyzed`는 `pending-vector:<jobId>`에 쌓아 업로드
  집계 입력에 더한다. job 집계 뒤 늦게 도착한 확정 이벤트는 결과를 다시 계산하고 `revision`을
  1 올려 `source.completed`를 다시 발행한다. 같은 메시지를 다시 받으면 기존 revision을 유지한다.
- revision과 입력 이벤트 목록은 계약의 결과 파일에 자리가 없어 옆 파일
  `source-result.meta.json`에 둔다.
- STREAM: 창(chunk)마다 `event.analyzed`·`track.detected`·`chunk.manifest`를
  `pending:<sourceId>:<chunkId>`에 모으고, `analysis.completed`(scope chunk)를 받으면 개수를 맞춘 뒤
  세션 상태를 접어 `state.updated`를 발행한다. 위험구역 접근 이벤트는 job당 재판정 상한(프로필
  `judge_call_limit`) 안에서 `event_judge`로 다시 판정한다(R-31). 이미 처리한 창은 저장해 둔 결과를
  다시 발행한다. 세션 완료(scope job)는 창 기록으로 결과 파일을 만들고 `source.completed`(revision 1)를
  발행한다.
- STREAM 계약에는 머문 구간이 없어 실시간 결과의 체류·배회 이벤트는 없고 구역 노출은 0이다.

## 저장 키

| 저장소 | 키 | TTL | 내용 |
| --- | --- | --- | --- |
| Redis | `pending-vector:<jobId>` | 7일 | 업로드 job의 `vector*` 판정 이벤트(HSET, 이벤트 id 필드로 멱등) |
| Redis | `pending:<sourceId>:<chunkId>` | 1시간 | 실시간 창의 이벤트·트랙·manifest |
| Redis | `agg:state:<sourceId>` | 48시간 | 실시간 세션의 현재 상태 |
| Redis | `agg:window:<sourceId>:<chunkId>` | 48시간 | 처리한 창의 sequence |
| Redis | `agg:zones:<sourceId>` | 48시간 | stream-service에서 읽은 세션 구역 |
| Redis | `agg:judge-calls:<jobId>` | 48시간 | 재판정 호출 수 |
| S3 | `sources/<sourceId>/jobs/<jobId>/aggregate/source-result.json` | - | 결과 파일 |
| S3 | `sources/<sourceId>/jobs/<jobId>/aggregate/source-result.meta.json` | - | revision, 입력 이벤트 id, partial |
| S3 | `sources/<sourceId>/jobs/<jobId>/aggregate/windows/<sequence>.json` | - | 창 기록(세션 마무리 입력) |
| S3 | `sources/<sourceId>/state/<sequence>.json`, `state/latest.json` | - | 30창마다 남기는 세션 상태 스냅샷. Redis를 잃으면 여기서 복구한다 |

## 테스트

```bash
uv run pytest services/aggregator -m "not integration"   # 단위·서비스·성능
uv run pytest services/aggregator                        # Redis·Kafka e2e 포함(Docker 필요)
scripts/test-in-docker.sh aggregator                     # 서비스 이미지 안에서 전체
```
