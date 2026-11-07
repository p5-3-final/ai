# grounding-worker

`grounding.requested`의 구간(최대 3개)을 순서대로 처리한다. 구간마다 정규화 영상에서 샘플 프레임(기본
1초 간격, 최대 12장)을 뽑아 대상어를 텍스트 클래스로 탐지하고, 박스와 대상어 라벨을 그린 H.264
클립(원본 fps, 오디오 유지)과 결과 파일을 저장한 뒤 `grounding.completed`를 한 건 발행한다. 결과
파일이 이미 있으면 탐지·인코딩 없이 그 내용으로 다시 발행한다. DB는 없다.

- spec: docs `docs/specs/SPEC-026-grounding-worker/spec.md`, plan: `specs/SPEC-026-grounding-worker/plan.md`
- 계약: docs `service-contracts/asyncapi/grounding-worker.yaml`, `events/grounding.requested.schema.json`,
  `events/grounding.completed.schema.json`, `groundings/v1/grounding-result.schema.json`
- 저장 경로: `sources/<sourceId>/groundings/<groundingId>/<groundingModel>/clip.mp4`, 같은 폴더의
  `result.json`(클립을 먼저 쓴다)
- 영상 처리는 시스템 ffmpeg·ffprobe 프로세스다. 입력은 저장소 서명 주소라 영상 전체를 받지 않고 구간만
  읽는다. 박스는 ffmpeg `drawbox`·`drawtext`로 그리고, 라벨 글꼴은 이미지의 `fonts-dejavu-core`다.
- 탐지기는 `GroundingDetector` 인터페이스다. 지금은 결정적 mock만 있다. 실제 탐지기(onnxruntime 추천)는
  가중치가 준비된 뒤 더한다(tasks T026~T028).

## 실행

```bash
GROUNDING_WORKER_PROFILE_PATH=services/grounding-worker/profiles/local-mock.toml \
GROUNDING_WORKER_ALLOW_MOCK=true \
uv run --package grounding-worker python -m grounding_worker
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_PATH_STYLE` | (버킷 필수) | 영상 버킷. 정규화 사본을 서명 주소로 읽고 클립·결과 파일을 쓴다 |
| `GROUNDING_WORKER_PROFILE_PATH` | (필수, 이미지는 `/app/profiles/local-mock.toml`) | 탐지기·기준 신뢰도·렌더링 프로필(TOML) |
| `GROUNDING_WORKER_ALLOW_MOCK` | `false` | mock 탐지기 허용. 운영에서 실수로 고르지 않도록 따로 켠다 |
| `GROUNDING_WORKER_ITEM_TIMEOUT_S` | `120` | 구간 하나(probe~렌더링)의 제한 시간. 넘으면 `GROUNDING_TIMEOUT` |
| `GROUNDING_WORKER_SAMPLE_INTERVAL_MS` | `1000` | 샘플 간격(200 이상) |
| `GROUNDING_WORKER_MAX_SAMPLES` | `12` | 구간당 최대 샘플 수(1~12) |
| `GROUNDING_WORKER_WORK_DIR` | 시스템 임시 폴더(이미지는 `/var/tmp/grounding`) | 구간마다 임시 폴더를 만들고 끝나면 지운다. `[A-Za-z0-9_./-]`만 쓴다 |
| `GROUNDING_WORKER_GROUP_ID` | `grounding-worker` | 소비 그룹. 재시도 `grounding.requested.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013). 단계 `grounding.sample`·`detect`·`render`·`store` |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 구간이 영상 길이를 넘거나(프레임 간격 하나까지 허용) 끝이 시작 이하이면 `GROUNDING_SEGMENT_OUT_OF_RANGE`,
  원본이 없거나 읽지 못하면 `GROUNDING_SOURCE_UNREADABLE`, 탐지기 오류면 `GROUNDING_DETECTOR_FAILED`로
  `outcome: FAILED`를 발행한다.
- 저장소·Kafka 오류는 메시지 전체를 재시도 토픽으로 보낸다. 다시 처리하면 끝난 구간은 결과 파일로
  재사용한다. 계약과 맞지 않거나 `normalizedKey`가 메시지의 sourceId·normalizationId 폴더가 아니면
  재시도 없이 DLQ로 간다.
- eventId는 groundingId·탐지기 버전·requestId·outcome으로 정한다. 기준 신뢰도는 결과 파일에만 있다.
- 대상어·질의는 로그에 남기지 않는다.

## 테스트

```bash
uv run pytest services/grounding-worker -m "not ffmpeg"   # 단위·서비스(가짜 미디어·탐지기)
scripts/test-in-docker.sh grounding-worker                # 서비스 이미지 안에서 ffmpeg·Kafka e2e 포함 전체
```
