"""같은 트랙·같은 유형 후보의 쿨다운 병합(SPEC-023 R-3). I/O 없는 규칙만 둔다.

같은 사람이 몇 초 동안 헬멧을 벗고 있으면 탐지기는 후보를 여러 개 낸다. 판정 수십 건이 되지 않게
메시지 안에서 하나로 합친다. 청크·창을 넘는 병합은 aggregator가 한다.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.events import DetectableEventType, DetectorCandidate


@dataclass(frozen=True, slots=True)
class Kept:
    """병합 뒤 남은 후보와 그 후보에 합쳐진 후보 ID(시작 시각순)."""

    candidate: DetectorCandidate
    merged_ids: tuple[UUID, ...] = ()


def _order(candidate: DetectorCandidate) -> tuple[int, str]:
    return candidate.start_ms, str(candidate.candidate_id)


def _rank(candidate: DetectorCandidate) -> tuple[float, int, str]:
    # 점수 최고 → 이른 시작 → 작은 candidateId(문자열). min으로 고른다.
    return -candidate.score, candidate.start_ms, str(candidate.candidate_id)


def _chains(group: list[DetectorCandidate], cooldown_ms: float) -> list[list[DetectorCandidate]]:
    """시작순으로 놓고 바로 앞 후보의 끝과 간격이 쿨다운 이하인 후보를 이어 붙인다."""
    chains: list[list[DetectorCandidate]] = []
    for candidate in sorted(group, key=_order):
        if chains and candidate.start_ms - chains[-1][-1].end_ms <= cooldown_ms:
            chains[-1].append(candidate)
        else:
            chains.append([candidate])
    return chains


def merge_candidates(candidates: Sequence[DetectorCandidate], cooldown_s: float) -> list[Kept]:
    """쿨다운 병합. 남은 후보를 입력 순서대로 돌려준다.

    Args:
        candidates: 메시지(또는 후보 파일)의 후보 목록.
        cooldown_s: 프로필 쿨다운(초).

    Returns:
        남은 후보마다 `Kept`. 트랙이 없는 후보는 합치지 않고 그대로 남는다.
    """
    groups: dict[tuple[str, DetectableEventType], list[DetectorCandidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.track_local_id is not None:
            groups[(candidate.track_local_id, candidate.event_type)].append(candidate)
    merged: dict[UUID, tuple[UUID, ...]] = {}
    absorbed: set[UUID] = set()
    for group in groups.values():
        for chain in _chains(group, cooldown_s * 1000):
            best = min(chain, key=_rank)
            others = tuple(c.candidate_id for c in chain if c is not best)
            merged[best.candidate_id] = others
            absorbed.update(others)
    return [
        Kept(c, merged.get(c.candidate_id, ()))
        for c in candidates
        if c.candidate_id not in absorbed
    ]
