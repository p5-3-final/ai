# track-stitcher

청크마다 끊긴 로컬 트랙을 Job 단위로 잇는 워커다. `chunk.boundary`·`segment.embedded`·`video.deleted`를
받아 Job별 대기 상태를 Redis에 모은다.

- 경계 연결: 인접 청크 한 쌍이 모이면 겹침 구간 박스 IoU로 같은 클래스 트랙을 일대일로 잇고
  `track.detected`(origin `stitcher`)를 쌍마다 한 번 발행한다.
- 재등장 연결(Re-ID): 소유 범위가 영상 전체를 덮고 모든 실행의 크롭 벡터가 모이면, 같은 클래스 트랙을
  외형 유사도와 시간 간격으로 잇는다.
- 확정: 번역표(`sources/<sourceId>/jobs/<jobId>/tracks/mapping.json`)를 저장한 뒤 `track.manifest`를 Job마다
  한 번 발행한다. 마지막 입력 뒤 대기 제한 시간이 지나면 받은 청크만으로 끝낸다(`complete: false`).
- `jobType: STREAM`(실시간 창)은 상태 없이 건너뛴다.

- spec: docs `docs/specs/SPEC-022-track-stitcher/spec.md`, plan: `specs/SPEC-022-track-stitcher/plan.md`
- 계약: docs `service-contracts/asyncapi/track-stitcher.yaml`, `events/chunk.boundary`·`track.detected`·
  `track.manifest`, `tracks/v1/chunk-tracks`·`track-mapping`
- 기준값: `profiles/local.toml`. 값은 로컬 동작 확인용 자리값이다. 운영 값은 평가로 정하고 `version`을 올린다.

## 실행

```bash
uv run --package track-stitcher python -m track_stitcher
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `TRACK_STITCHER_REDIS_URL` | (필수) | Job 대기 상태 Redis(예: `redis://redis:6379/0`). 키 접두사는 `ts:` |
| `TRACK_STITCHER_PROFILE_PATH` | 이미지: `/app/profiles/local.toml` | 기준값 프로필. 없거나 범위 밖이면 기동에 실패한다 |
| `TRACK_STITCHER_SWEEP_INTERVAL_S` | `30` | 대기 제한 시간 확인 간격 |
| `TRACK_STITCHER_FINALIZE_LEASE_S` | `300` | 확정 임대 시간. Pod가 죽으면 이 시간 뒤 다른 Pod가 이어 받는다 |
| `TRACK_STITCHER_GROUP_ID` | `track-stitcher` | 소비 그룹. 재시도 `<토픽>.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_PATH_STYLE` | | 트랙·벡터 파일을 읽고 번역표를 쓰는 영상 버킷 |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013) |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 트랙 파일이 없거나 계약·메시지와 맞지 않으면 그 메시지는 기록하지 않고 재시도 없이 DLQ로 간다. 저장소·Redis·Kafka
  오류는 재시도 토픽으로 간다.
- 확정 임대(`claim`)를 얻은 Pod 하나만 확정한다. 주기 확인은 Pod마다 돌지만 임대로 한 번만 발행한다.
- 실시간 창으로 건너뛴 수는 로그 `stitcher.skipped_stream`의 `skippedTotal`로 남는다.

## 테스트

```bash
uv run pytest services/track-stitcher -m "not integration"   # 단위·서비스(메모리 상태 저장소)
uv run pytest services/track-stitcher                        # Redis·Kafka·SeaweedFS 포함(Docker 필요)
scripts/test-in-docker.sh track-stitcher                     # 서비스 이미지 안에서 전체
```
