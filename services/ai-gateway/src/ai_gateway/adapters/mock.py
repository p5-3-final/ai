"""CI·로컬 전용 가짜 VLM. 모델 없이 요청부터 호출 로그까지 흐름을 확인할 때 쓴다.

운영 프로필에서 고르면 기동에 실패한다(SPEC-009 R-15).
"""

import json

from vlm_encoders.mock import MockEncoder

from ai_gateway.adapters.base import Completion, Embeddings, ImageInput
from ai_gateway.prompts import PromptMessage, PromptRegistry

_INPUT_BLOCK = "```json\n"


class MockAdapter:
    """taskType 출력 스키마의 첫 예시를 "확인됨" 판정처럼 돌려준다.

    taskType별 코드 없이 Registry 정의만 쓴다(SPEC-009 R-24). 입력 변수와 이름이 같은 출력 필드는
    입력값으로 바꿔, 후보의 유형 같은 값이 요청을 따라가게 한다.

    Args:
        prompts: 출력 예시를 찾을 Prompt Registry.
        embedding_dimension: `embed`가 돌려줄 벡터 차원(프로필의 embed_text 차원).
    """

    def __init__(self, prompts: PromptRegistry, embedding_dimension: int = 8) -> None:
        self._prompts = prompts
        self._embedding_dimension = embedding_dimension

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
        task = self._prompts.get(message.task_type)
        example = task.output_example if task else {}
        variables = _input_json(message.variable_part)
        reply = example | {k: v for k, v in variables.items() if k in example}
        return Completion(
            text=json.dumps(reply, ensure_ascii=False), input_tokens=None, output_tokens=None
        )

    async def embed(
        self,
        *,
        model: str,
        texts: list[str],
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Embeddings:
        # 입력 해시로 만든 결정적 벡터. 차원은 프로필의 embed_text 차원을 따른다.
        vectors = MockEncoder(model, self._embedding_dimension).encode_texts(texts)
        return Embeddings(vectors=vectors, input_tokens=None)


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
