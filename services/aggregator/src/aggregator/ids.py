"""결과 안의 결정적 ID(R-20). 같은 job·같은 입력이면 다시 처리해도 같은 ID다.

이름 규칙은 plan 4절 표다. `uuid5(NAMESPACE_URL, 이름)`이라 다른 서비스도 같은 ID를 만들 수 있다.
"""

from uuid import NAMESPACE_URL, UUID, uuid5


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, name)


def source_completed_id(job_id: UUID, revision: int) -> UUID:
    """`source.completed` eventId. 늦은 벡터 이벤트로 다시 내면 revision이 달라 새 ID다(R-27)."""
    return _id(f"vlm:source.completed:{job_id}:{revision}")


def state_updated_id(job_id: UUID, sequence: int) -> UUID:
    """`state.updated` eventId(계약: jobId·sequence로 결정적)."""
    return _id(f"vlm:state.updated:{job_id}:{sequence}")


def merge_group_id(job_id: UUID, kept_event_id: UUID) -> UUID:
    return _id(f"vlm:aggregate:merge:{job_id}:{kept_event_id}")


def rule_event_id(job_id: UUID, rule: str, track_id: UUID, zone_id: UUID, start_ms: int) -> UUID:
    return _id(f"vlm:aggregate:rule:{job_id}:{rule}:{track_id}:{zone_id}:{start_ms}")


def snapshot_id(job_id: UUID, related_event_id: UUID, zone_id: str) -> UUID:
    return _id(f"vlm:aggregate:snapshot:{job_id}:{related_event_id}:{zone_id}")


def risk_id(job_id: UUID, zone_id: str, risk_type: str, first_event_id: UUID) -> UUID:
    return _id(f"vlm:aggregate:risk:{job_id}:{zone_id}:{risk_type}:{first_event_id}")


def object_id(chunk_id: UUID, object_local_id: str) -> str:
    """영상 안 정적 객체 ID(SPEC-028 R-17). 사슬을 처음 본 청크와 그 청크의 로컬 ID로 정한다.

    계약 설명은 "chunkId 앞 8자 + objectLocalId"지만 같은 job의 chunkId(UUIDv7)는 앞 8자가 시각이라
    겹칠 수 있다. 앞 8자 뒤에 (chunkId, 로컬 ID)의 해시 16자를 붙여 겹치지 않게 하고 64자 안에 둔다.
    """
    digest = _id(f"vlm:aggregate:object:{chunk_id}:{object_local_id}").hex[:16]
    return f"{chunk_id.hex[:8]}-{digest}"


def change_id(job_id: UUID, kind: str, before: str | None, after: str | None) -> UUID:
    return _id(f"vlm:aggregate:change:{job_id}:{kind}:{before or '-'}:{after or '-'}")


def inventory_event_id(job_id: UUID, change: UUID) -> UUID:
    return _id(f"vlm:aggregate:inventory-event:{job_id}:{change}")


def equipment_event_id(job_id: UUID, object_id_: str) -> UUID:
    return _id(f"vlm:aggregate:equipment:{job_id}:{object_id_}")
