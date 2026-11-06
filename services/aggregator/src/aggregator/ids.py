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
