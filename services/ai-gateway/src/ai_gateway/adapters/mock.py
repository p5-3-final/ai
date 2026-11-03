"""CI·로컬 전용 가짜 VLM. 모델 없이 요청부터 호출 로그까지 흐름을 확인할 때 쓴다.

운영 프로필에서 고르면 기동에 실패한다(SPEC-009 R-15).
"""

import json

from ai_gateway.adapters.base import Completion, ImageInput
from ai_gateway.prompts import PromptMessage

_INPUT_BLOCK = "```json\n"


class MockAdapter:
    """입력 JSON의 후보를 그대로 "확인됨"으로 판정한 event_judge 결과를 돌려준다."""

    async def complete(
        self,
        *,
        model: str,
        message: PromptMessage,
        images: list[ImageInput],
        max_output_tokens: int,
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Completion:
        variables = _input_json(message.variable_part)
        reply = {
            "eventType": variables.get("eventType", "other"),
            "eventDetected": True,
            "verificationStatus": "confirmed",
            "description": f"mock 판정: 이미지 {len(images)}장",
            "beforeState": None,
            "afterState": None,
            "riskDetected": False,
            "riskType": None,
            "riskReason": None,
            "solution": None,
            "severity": "low",
            "evidenceStart": variables.get("startSec"),
            "evidenceEnd": variables.get("endSec"),
        }
        return Completion(
            text=json.dumps(reply, ensure_ascii=False), input_tokens=None, output_tokens=None
        )


def _input_json(variable_part: str) -> dict[str, object]:
    # 가변부 끝의 입력 JSON 코드 블록(PromptTask.render)을 읽는다.
    start = variable_part.rfind(_INPUT_BLOCK)
    if start < 0:
        return {}
    body = variable_part[start + len(_INPUT_BLOCK) :].split("```", 1)[0]
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
