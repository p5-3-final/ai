"""`JobStateStore`의 Redis 구현(SPEC-022 R-2, R-16, R-17, R-19, NFR-2, NFR-3).

키는 영상마다 `ts:{<sourceId>}:` 접두사다. 해시 태그 `{sourceId}`로 한 영상의 키가 같은 슬롯에
있어 Lua 스크립트 하나로 원자적으로 고친다(Redis Cluster여도 된다). 영상에 묶이지 않는 유일한 키
`ts:due`(주기 확인용 sorted set)는 다른 슬롯일 수 있어 스크립트 밖에서 따로 고친다. 이 키는 확인할
Job을 고르는 색인일 뿐이고 확정 여부는 스크립트가 정하므로, 둘 사이가 잠깐 어긋나도 결과는 같다.

| 키 | 형식 | 내용 |
| --- | --- | --- |
| `job:<jobId>:meta` | hash | status, ownerId, normalizationId, sourceDurationMs, lastInputMs, |
| | | traceparent, leaseUntilMs |
| `job:<jobId>:chunks` | hash | chunkId → ChunkRecord JSON |
| `job:<jobId>:vectors` | hash | executionId → VectorRecord JSON |
| `jobs` | set | 그 영상의 jobId(R-19 삭제용) |
| `deleted` | string | `video.deleted` 표시 |
"""

from collections.abc import Sequence
from typing import cast
from uuid import UUID

from redis import Redis

from track_stitcher.state import (
    ChunkRecord,
    JobKey,
    JobMeta,
    JobSnapshot,
    JobStatus,
    RecordOutcome,
    RecordResult,
    VectorRecord,
)

# NFR-3: 마지막 입력 뒤 7일이 지나면 대기 상태를 지운다. 삭제 표시도 같은 기간 남긴다.
KEY_TTL_S = 7 * 24 * 3600
DUE_KEY = "ts:due"
# 상태 저장소가 멈추면 재시도 토픽으로 넘기도록 오래 기다리지 않는다(R-20).
_SOCKET_TIMEOUT_S = 5.0

# KEYS: meta, 기록할 hash, chunks, vectors, jobs, deleted
# ARGV: field, json, executionId, ownerId, normalizationId, sourceDurationMs|"", traceparent|"",
#       nowMs, ttl, jobId, kind(chunk|vectors)
_RECORD = """
if redis.call('EXISTS', KEYS[6]) == 1 then return {'deleted'} end
local status = redis.call('HGET', KEYS[1], 'status')
if status and status ~= 'open' then return {'late', status} end
redis.call('HSETNX', KEYS[1], 'status', 'open')
redis.call('HSETNX', KEYS[1], 'ownerId', ARGV[4])
redis.call('HSETNX', KEYS[1], 'normalizationId', ARGV[5])
redis.call('HSETNX', KEYS[1], 'lastInputMs', ARGV[8])
if ARGV[6] ~= '' then redis.call('HSETNX', KEYS[1], 'sourceDurationMs', ARGV[6]) end
local outcome = 'recorded'
local existing = redis.call('HGET', KEYS[2], ARGV[1])
if existing then
  outcome = 'duplicate'
  if ARGV[11] == 'chunk' and cjson.decode(existing)['executionId'] ~= ARGV[3] then
    outcome = 'conflict'
  end
else
  redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
  redis.call('HSET', KEYS[1], 'lastInputMs', ARGV[8])
  if ARGV[7] ~= '' then
    redis.call('HSET', KEYS[1], 'traceparent', ARGV[7])
  else
    redis.call('HDEL', KEYS[1], 'traceparent')
  end
end
redis.call('SADD', KEYS[5], ARGV[10])
for i = 1, 5 do redis.call('EXPIRE', KEYS[i], ARGV[9]) end
return {outcome, redis.call('HGETALL', KEYS[1]), redis.call('HGETALL', KEYS[3]),
        redis.call('HGETALL', KEYS[4])}
"""

# KEYS: meta, chunks, vectors / ARGV: nowMs, leaseUntilMs
_CLAIM = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status or status == 'finalized' then return {'no'} end
if status == 'finalizing' then
  local lease = tonumber(redis.call('HGET', KEYS[1], 'leaseUntilMs') or '0')
  if lease > tonumber(ARGV[1]) then return {'no'} end
end
redis.call('HSET', KEYS[1], 'status', 'finalizing', 'leaseUntilMs', ARGV[2])
return {'claimed', redis.call('HGETALL', KEYS[1]), redis.call('HGETALL', KEYS[2]),
        redis.call('HGETALL', KEYS[3])}
"""

# KEYS: meta / ARGV: 새 status, 바꿀 수 있는 이전 status
_TRANSITION = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[1], 'status', ARGV[1])
return 1
"""

# KEYS: jobs, deleted / ARGV: ttl
_MARK_DELETED = """
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
return redis.call('SMEMBERS', KEYS[1])
"""

type _Reply = list[object]


class RedisJobStateStore:
    """Redis(운영은 ai-gateway와 같은 구성요소, 키 접두사로 나눔)에 Job 상태를 둔다.

    redis-py 동기 클라이언트는 연결 풀이 스레드에 안전해 워커 핸들러 스레드와 주기 확인 스레드가
    함께 쓴다. Redis 오류(`RedisError`)는 잡지 않는다(R-20).
    """

    def __init__(self, url: str, *, client: Redis | None = None) -> None:
        self._redis: Redis = client or Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_SOCKET_TIMEOUT_S,
        )
        self._record = self._redis.register_script(_RECORD)
        self._claim = self._redis.register_script(_CLAIM)
        self._transition = self._redis.register_script(_TRANSITION)
        self._mark_deleted = self._redis.register_script(_MARK_DELETED)

    def record_chunk(
        self, key: JobKey, meta: JobMeta, chunk: ChunkRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        args = (str(chunk.chunk_id), chunk.to_json(), str(chunk.execution_id))
        return self._record_item(key, meta, "chunks", "chunk", args, now_ms, wait_ms)

    def record_vectors(
        self, key: JobKey, meta: JobMeta, vectors: VectorRecord, *, now_ms: int, wait_ms: int
    ) -> RecordResult:
        execution = str(vectors.execution_id)
        args = (execution, vectors.to_json(), execution)
        return self._record_item(key, meta, "vectors", "vectors", args, now_ms, wait_ms)

    def snapshot(self, key: JobKey) -> JobSnapshot | None:
        pipe = self._redis.pipeline(transaction=True)
        pipe.hgetall(self._k(key, "meta"))
        pipe.hgetall(self._k(key, "chunks"))
        pipe.hgetall(self._k(key, "vectors"))
        meta, chunks, vectors = cast(list[dict[str, str]], pipe.execute())
        if not meta:
            return None
        return _snapshot(key, meta, chunks, vectors)

    def claim(self, key: JobKey, *, now_ms: int, lease_ms: int) -> JobSnapshot | None:
        lease_until = now_ms + lease_ms
        reply = cast(
            _Reply,
            self._claim(
                keys=[self._k(key, "meta"), self._k(key, "chunks"), self._k(key, "vectors")],
                args=[now_ms, lease_until],
            ),
        )
        if reply[0] != "claimed":
            return None
        # 임대 중 Pod가 죽어도 임대가 끝나면 주기 확인이 이어 받도록 대기 제한 시각을 옮긴다.
        self._redis.zadd(DUE_KEY, {_member(key): lease_until})
        return _snapshot(key, _pairs(reply[1]), _pairs(reply[2]), _pairs(reply[3]))

    def release(self, key: JobKey, *, retry_at_ms: int | None) -> None:
        released = self._transition(keys=[self._k(key, "meta")], args=["open", "finalizing"])
        if not released:
            return
        if retry_at_ms is None:
            self._redis.zrem(DUE_KEY, _member(key))
        else:
            self._redis.zadd(DUE_KEY, {_member(key): retry_at_ms})

    def finish(self, key: JobKey) -> None:
        self._transition(keys=[self._k(key, "meta")], args=["finalized", "finalizing"])
        self._redis.zrem(DUE_KEY, _member(key))

    def due(self, now_ms: int, limit: int) -> list[JobKey]:
        members = cast(
            list[str], self._redis.zrangebyscore(DUE_KEY, "-inf", now_ms, start=0, num=limit)
        )
        keys: list[JobKey] = []
        for member in members:
            source, job = member.split(":")
            keys.append(JobKey(UUID(source), UUID(job)))
        return keys

    def delete_video(self, source_id: UUID) -> int:
        prefix = _prefix(source_id)
        # 삭제 표시를 먼저 남기고 Job 목록을 읽는다. 그 뒤의 기록 스크립트는 표시를 보고 멈추므로
        # 목록 밖의 Job 키가 새로 생기지 않는다.
        jobs = cast(
            list[str],
            self._mark_deleted(keys=[f"{prefix}jobs", f"{prefix}deleted"], args=[KEY_TTL_S]),
        )
        if jobs:
            parts = ("meta", "chunks", "vectors")
            self._redis.delete(*(f"{prefix}job:{j}:{p}" for j in jobs for p in parts))
            self._redis.zrem(DUE_KEY, *(f"{source_id}:{j}" for j in jobs))
        self._redis.delete(f"{prefix}jobs")
        return len(jobs)

    def _record_item(
        self,
        key: JobKey,
        meta: JobMeta,
        part: str,
        kind: str,
        item: tuple[str, str, str],
        now_ms: int,
        wait_ms: int,
    ) -> RecordResult:
        prefix = _prefix(key.source_id)
        reply = cast(
            _Reply,
            self._record(
                keys=[
                    self._k(key, "meta"),
                    self._k(key, part),
                    self._k(key, "chunks"),
                    self._k(key, "vectors"),
                    f"{prefix}jobs",
                    f"{prefix}deleted",
                ],
                args=[
                    *item,
                    str(meta.owner_id),
                    str(meta.normalization_id),
                    "" if meta.source_duration_ms is None else meta.source_duration_ms,
                    meta.traceparent or "",
                    now_ms,
                    KEY_TTL_S,
                    str(key.job_id),
                    kind,
                ],
            ),
        )
        outcome = cast(RecordOutcome, reply[0])
        if outcome == "deleted":
            return RecordResult("deleted")
        if outcome == "late":
            return RecordResult("late", status=cast(JobStatus, reply[1]))
        fields = _pairs(reply[1])
        # 마지막 입력 + 대기 제한 시간. 중복 기록도 다시 넣어, 앞선 시도가 이 단계 전에 실패했어도
        # 주기 확인 대상에서 빠지지 않게 한다.
        self._redis.zadd(DUE_KEY, {_member(key): int(fields["lastInputMs"]) + wait_ms})
        return RecordResult(outcome, _snapshot(key, fields, _pairs(reply[2]), _pairs(reply[3])))

    def _k(self, key: JobKey, part: str) -> str:
        return f"{_prefix(key.source_id)}job:{key.job_id}:{part}"


def _prefix(source_id: UUID) -> str:
    return f"ts:{{{source_id}}}:"


def _member(key: JobKey) -> str:
    return f"{key.source_id}:{key.job_id}"


def _pairs(raw: object) -> dict[str, str]:
    """Lua `HGETALL` 응답(이름·값을 번갈아 둔 목록)을 dict로 바꾼다."""
    items = cast(Sequence[str], raw)
    return dict(zip(items[::2], items[1::2], strict=True))


def _snapshot(
    key: JobKey, meta: dict[str, str], chunks: dict[str, str], vectors: dict[str, str]
) -> JobSnapshot:
    duration = meta.get("sourceDurationMs")
    records = sorted(
        (ChunkRecord.from_json(v) for v in chunks.values()),
        key=lambda c: (c.owned_range.start_ms, str(c.chunk_id)),
    )
    return JobSnapshot(
        key=key,
        status=cast(JobStatus, meta["status"]),
        meta=JobMeta(
            owner_id=UUID(meta["ownerId"]),
            normalization_id=UUID(meta["normalizationId"]),
            source_duration_ms=int(duration) if duration is not None else None,
            traceparent=meta.get("traceparent"),
        ),
        chunks=tuple(records),
        vectors=tuple(
            sorted(
                (VectorRecord.from_json(v) for v in vectors.values()),
                key=lambda v: str(v.execution_id),
            )
        ),
    )
