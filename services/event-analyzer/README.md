# event-analyzer

이벤트 후보를 ai-gateway `/v1/analyze`(`event_judge`)로 판정한다. 1차 판정이 severity `high`이거나 판단
불가면(탐지기 후보는 점수가 프로필의 애매 구간 안이어도) `event_judge_escalate`로 다시 판정한다. DB는 없다.

| 받는 토픽 | 하는 일 | 내는 토픽 |
| --- | --- | --- |
| `event.candidate.vector` | 벡터 후보마다 판정(SPEC-020) | `event.analyzed`(`vector+vlm*`) |
| `event.candidate` | 쿨다운 병합 → 규칙 후보 템플릿 확정 → 상태형 구간 규칙(carry) → 실행당 상한 안에서 판정(SPEC-023) | `event.analyzed`(`rule*`) × 남은 후보, `event.manifest` × 1 |
| `alert.candidate` | pending 알림을 먼저 내고 `event_judge` 1회로 닫는다(SPEC-023 R-21~R-24) | `alert.raised`(pending → confirmed·dismissed) |

- spec: docs `docs/specs/SPEC-020-event-analyzer-vector/spec.md`, `docs/specs/SPEC-023-event-analyzer-detector/spec.md`
- 계약: docs `service-contracts/asyncapi/event-analyzer.yaml`, `events/event.analyzed.schema.json`,
  `events/event.candidate.schema.json`, `events/event.manifest.schema.json`, `events/alert.*.schema.json`,
  `candidates/v1/candidate-file.schema.json`, `events/_vlm-analysis.schema.json`
- ai-gateway 호출은 `libs/ai-client`(REST)로 한다. ai-gateway 프로필의 `[callers] allowed`에
  `event-analyzer`가 있어야 한다.

## 실행

```bash
uv run --package event-analyzer python -m event_analyzer
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `EVENT_ANALYZER_AI_GATEWAY_URL` | (필수) | ai-gateway 주소(예: `http://ai-gateway:8000`) |
| `EVENT_ANALYZER_CALL_TIMEOUT_S` | `180` | 판정 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값을 `X-Request-Deadline`으로 싣는다(SPEC-021 R-8). ai-gateway 모델 제한 시간보다 길게 둔다 |
| `EVENT_ANALYZER_MAX_RETRIES`, `EVENT_ANALYZER_RETRY_BASE_DELAY_S` | `2`, `1.0` | 요청이 닿지 않은 연결 실패의 재시도 횟수와 첫 대기(두 배씩 는다). 502·503·504·응답 시간 초과는 VLM 비용이 두 번 들 수 있어 다시 부르지 않는다(SPEC-021 R-9) |
| `EVENT_ANALYZER_CONCURRENCY` | `4` | 한 메시지 안의 동시 판정 수 |
| `EVENT_ANALYZER_GROUP_ID` | `event-analyzer` | 소비 그룹. 재시도 `<받는 토픽>.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `EVENT_ANALYZER_PROFILE_PATH` | `/app/profiles/local.toml` | 판정 규칙 프로필. 없거나 틀리면 기동에 실패한다(SPEC-023 R-16) |
| `EVENT_ANALYZER_REDIS_URL` | (없음) | 실시간 재판정 간격 상태(SPEC-023 R-18). 비우거나 끊기면 창마다 판정한다 |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_PATH_STYLE` | (`S3_BUCKET` 필수) | 후보 파일(`candidatesKey`)을 읽는 저장소. 자격 증명은 boto3 기본 체인 |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013) |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 판정 호출이 끝내 실패하거나(재시도 소진, 5xx, `AI_DEADLINE_EXCEEDED`(504), `AI_IMAGE_NOT_FOUND`, 계약과 맞지 않는 응답) 그 후보는
  `unknown`과 실패 코드로 발행한다. 다른 후보는 그대로 판정한다.
- ai-gateway가 그 밖의 4xx(호출자·경로·taskType 거절)를 돌려주거나 계약과 맞지 않는 메시지는 아무것도
  발행하지 않고 재시도 없이 DLQ로 간다. Kafka 발행 실패는 재시도 토픽으로 간다.

## 프로필 (`profiles/local.toml`)

문장·기준값은 동작 확인용 자리값이다. 실제 값은 AI 담당자가 정한다.

| 표 | 값 | 규칙 |
| --- | --- | --- |
| `[merge]` | `cooldown_s`(기본 10) | 같은 트랙·유형의 앞 후보 끝과 다음 시작 간격이 이 값 이하면 점수 최고 하나만 남긴다. 0 이하면 기동 실패 |
| `[rule_text]` | `missing_zone_name` | 구역 이름이 null인 규칙 후보의 템플릿에 넣는 글자 |
| `[rules.<유형>]` | `template`(`{zoneName}` 자리 표시, 900자 이하), `severity` | 탐지 가능 유형 8종 모두 필요하다. 어떤 유형을 `rule`로 보낼지는 생산자가 정한다. 긴급 알림 pending의 severity로도 쓴다 |
| `[escalation.<유형>]` | `ambiguous_score = [시작, 끝]` | 선택. 점수가 구간 안(양 끝 포함)이면 재판정한다. 0~1 밖이거나 시작 > 끝이면 기동 실패 |
| `[rejudge]` | `<유형> = <초>` | 상태형 유형의 재판정 간격(이벤트 정의 `rejudgeIntervalS`). 간격 안의 반복 후보는 직전 판정을 복사해 `rule+carry`로 발행한다 |
| `[cap]` | `floor`(3), `per_minute`(3) | 실행당 호출 상한 `max(floor, ceil(per_minute * 실행 길이(분)))`. 넘는 후보(시작순 뒤쪽)는 호출 없이 `unverified`(`call_cap_reached`) |

- 구간 규칙: 업로드는 메시지 안에서만 잇는다. `jobType: STREAM`이면 Redis 키 `rejudge:<sourceId>:<trackId>:<eventType>`의
  마지막 `confirmed` 판정을 창을 넘어 잇고, 발행 뒤 그 키를 갱신한다(TTL 간격 × 2).
- 긴급 후보: 판정 요청은 같은 candidateId의 창 후보와 같은 모양이라 ai-gateway 결과 캐시가 적중한다. `alert.candidate`에
  트리거 클래스·구역 이름이 없어 그 둘은 근거에서 빠진다. 판정이 실패하면 DLQ로 보내지 않고 dismissed(`unknown`)로 닫는다.

## 테스트

```bash
uv run pytest services/event-analyzer -m "not integration"   # 단위·서비스
uv run pytest services/event-analyzer                        # Kafka e2e 포함(Docker 필요)
scripts/test-in-docker.sh event-analyzer                     # 서비스 이미지 안에서 전체
```
