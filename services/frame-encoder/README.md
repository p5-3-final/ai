# frame-encoder

`segment.ready`를 받아 구간 프레임 4장·트랙 대표 크롭(블러 사본)을 이미지 인코더로 벡터로 바꾸고,
실행 폴더의 `vectors.json`에 저장한 뒤 `segment.embedded`를 발행한다. 프로필에 텍스트 프로토타입이
있으면 임계값을 넘은 (구간, 유형)을 `event.candidate.vector` 한 건으로 이어 발행한다.

- spec: docs `docs/specs/SPEC-019-frame-encoder/spec.md`
- 계약: docs `service-contracts/asyncapi/frame-encoder.yaml`, `vectors/v1/vector-file.schema.json`
- 인코더는 ai-gateway와 같은 `libs/encoders`를 쓴다. 두 서비스 프로필의 `[encoder]`가 같아야 질의 벡터와
  구간 벡터를 비교할 수 있다.

## 실행

```bash
uv run --package frame-encoder python -m frame_encoder
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_PATH_STYLE` | (필수), 없음, `false` | 블러 이미지를 읽고 벡터 파일을 쓰는 영상 버킷 |
| `FRAME_ENCODER_PROFILE_PATH` | 이미지: `/app/profiles/local.toml` | 인코더·묶음 크기·프로토타입 프로필 |
| `FRAME_ENCODER_MODELS_BUCKET` | 없음 | 인코더 가중치를 받는 모델 버킷. open_clip 프로필에 필요하다 |
| `FRAME_ENCODER_MODEL_CACHE_DIR` | `/var/cache/vlm-models` | 가중치 로컬 캐시 |
| `FRAME_ENCODER_ALLOW_MOCK` | `false` | mock 인코더 프로필(`local-mock.toml`)을 허용한다. CI·로컬 전용 |
| `FRAME_ENCODER_GROUP_ID` | `frame-encoder` | 소비 그룹. 재시도 `segment.ready.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013) |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 프로필·manifest가 틀리거나 mock을 허용하지 않았는데 mock 프로필이면 기동에 실패한다.
- 이미지가 없거나 디코딩할 수 없는 메시지, 실행 폴더 `blurred/` 밖 경로를 가리키는 메시지는 재시도 없이
  DLQ로 간다. 저장소·Kafka 일시 오류는 재시도한다.
- `profiles/local.toml`의 프로토타입 문장·임계값은 로컬 흐름 확인용 예시다. 운영 값은 평가로 정한다.

## 테스트

```bash
uv run pytest services/frame-encoder -m "not integration"   # 단위·서비스
uv run pytest services/frame-encoder                        # Kafka·SeaweedFS e2e 포함(Docker 필요)
scripts/test-in-docker.sh frame-encoder                     # 서비스 이미지 안에서 전체
uv run pytest services/frame-encoder -m model               # 실제 open_clip 가중치(로컬 전용)
```
