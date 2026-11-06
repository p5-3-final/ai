# track-analyzer

`attribute.requested`를 받아 ai-gateway `/v1/analyze`를 한 번 부르고, 요청마다 `track.analyzed`를 한 건
발행한다. reason `event`·`click`은 `track_attribute`(트랙 대표 크롭 1~3장), `question`은
`object_question`(크롭 다음에 장면 프레임, 질문)이다. 저장소·DB는 없다.

- spec: docs `docs/specs/SPEC-024-track-analyzer/spec.md`, plan: `specs/SPEC-024-track-analyzer/plan.md`
- 계약: docs `service-contracts/asyncapi/track-analyzer.yaml`, `events/attribute.requested.schema.json`,
  `events/track.analyzed.schema.json`, `events/_track-attribute.schema.json`,
  `events/_object-answer.schema.json`
- ai-gateway 호출은 `libs/ai-client`(REST)로 한다. ai-gateway 프로필의 `[callers] allowed`에
  `track-analyzer`가 있어야 한다.

## 실행

```bash
uv run --package track-analyzer python -m track_analyzer
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `TRACK_ANALYZER_AI_GATEWAY_URL` | (필수) | ai-gateway 주소(예: `http://ai-gateway:8000`) |
| `TRACK_ANALYZER_CALL_TIMEOUT_S` | `60` | 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값을 `X-Request-Deadline`으로 싣는다(SPEC-021 R-8). 화면이 기다리는 요청이라 event-analyzer(180초)보다 짧다 |
| `TRACK_ANALYZER_MAX_RETRIES`, `TRACK_ANALYZER_RETRY_BASE_DELAY_S` | `2`, `1.0` | 요청이 닿지 않은 연결 실패의 재시도 횟수와 첫 대기(두 배씩 는다). 502·503·504·응답 시간 초과는 다시 부르지 않는다(SPEC-021 R-9) |
| `TRACK_ANALYZER_GROUP_ID` | `track-analyzer` | 소비 그룹. 재시도 `attribute.requested.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013). 호출은 단계 `attribute.call` |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 호출이 결과 없이 끝나면(4xx·5xx·504·시간 초과·연결 실패 소진·결과 형식과 맞지 않는 응답) 화면이
  기다리지 않도록 `outcome: FAILED`와 오류 코드로 발행한다. event-analyzer와 달리 4xx도 DLQ로 보내지 않는다.
- 계약과 맞지 않는 메시지나 크롭·장면 프레임 경로가 그 메시지의 `sources/<sourceId>/` 밖인 메시지는
  ai-gateway를 부르지 않고 재시도 없이 DLQ로 간다. Kafka 발행 실패는 재시도 토픽으로 간다.
- 질문·답·설명 문장은 로그에 남기지 않는다.

## 테스트

```bash
uv run pytest services/track-analyzer -m "not integration"   # 단위·서비스
uv run pytest services/track-analyzer                        # Kafka e2e 포함(Docker 필요)
scripts/test-in-docker.sh track-analyzer                     # 서비스 이미지 안에서 전체
```
