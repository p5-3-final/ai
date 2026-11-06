"""상태형 유형의 구간 규칙(SPEC-023 R-17, R-18). I/O 없는 규칙만 둔다.

작업자가 몇 분 동안 보호구 없이 보이면 쿨다운 병합 뒤에도 후보가 여러 개 남는다. 재판정 간격마다
첫 후보만 판정하고 그 사이 후보는 직전 판정을 복사해 호출 수를 간격당 1회로 묶는다. 입력이
같으면 같은 계획이 나와 공유 상태 없이 결정적이다.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from uuid import UUID

from vlm_contracts.events import DetectableEventType, DetectorCandidate

type TrackKey = tuple[str, DetectableEventType]


def plan_carry(
    candidates: Sequence[DetectorCandidate],
    intervals_s: Mapping[DetectableEventType, float],
    prior_starts: Mapping[TrackKey, int] | None = None,
) -> dict[UUID, UUID | None]:
    """복사할 후보와 복사 원본.

    Args:
        candidates: 병합 뒤 남은 후보.
        intervals_s: 상태형 유형의 재판정 간격(초). 없는 유형은 사건형이다.
        prior_starts: 창을 넘어 이어 받을 직전 판정의 시작 시각(ms). 실시간(STREAM)에서
            Redis에서 읽은 값이다(R-18).

    Returns:
        복사할 후보 ID → 같은 메시지의 판정 대상 후보 ID. 직전 판정(`prior_starts`)을 이어 받으면
        값이 None이다. 여기 없는 VLM 후보는 판정 대상이다.
    """
    groups: dict[TrackKey, list[DetectorCandidate]] = defaultdict(list)
    for candidate in candidates:
        # 규칙 후보는 호출 비용이 없고 후보마다 구역 문장이 달라 복사하지 않는다.
        stateful = candidate.event_type in intervals_s
        if stateful and candidate.judgment == "vlm" and candidate.track_local_id is not None:
            groups[(candidate.track_local_id, candidate.event_type)].append(candidate)
    priors = prior_starts or {}
    plan: dict[UUID, UUID | None] = {}
    for key, group in groups.items():
        interval_ms = intervals_s[key[1]] * 1000
        ordered = sorted(group, key=lambda c: (c.start_ms, str(c.candidate_id)))
        last = priors.get(key)
        # 다시 처리할 때 Redis에 이번 메시지의 판정이 이미 있어도 같은 계획이 나오도록, 첫 후보보다
        # 앞선 판정만 이어 받는다.
        if last is not None and last >= ordered[0].start_ms:
            last = None
        source: UUID | None = None
        for candidate in ordered:
            if last is not None and candidate.start_ms - last <= interval_ms:
                plan[candidate.candidate_id] = source
            else:
                last, source = candidate.start_ms, candidate.candidate_id
    return plan
