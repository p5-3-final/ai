"""요청을 거절하는 예외. 각 `code`는 계약 `ai-gateway.yaml`의 오류 코드와 같다."""

from vlm_common.errors import VlmError


class GatewayRequestError(VlmError):
    """호출자에게 그대로 알리는 오류의 기본 클래스. `status`는 HTTP 상태 코드다."""

    status = 400
    title = "Bad Request"


class InvalidRequestError(GatewayRequestError):
    code = "AI_INVALID_REQUEST"

    def __init__(self, detail: str, errors: list[tuple[str, str]] | None = None) -> None:
        super().__init__(detail)
        # (필드 경로, 이유). ProblemDetail의 errors로 내보낸다.
        self.errors = errors or []


class UnknownTaskTypeError(GatewayRequestError):
    code = "AI_UNKNOWN_TASK_TYPE"


class TooManyImagesError(GatewayRequestError):
    code = "AI_TOO_MANY_IMAGES"


class ImageNotFoundError(GatewayRequestError):
    code = "AI_IMAGE_NOT_FOUND"


class TooManyInputsError(GatewayRequestError):
    code = "AI_TOO_MANY_INPUTS"


class ImageUnreadableError(GatewayRequestError):
    code = "AI_IMAGE_UNREADABLE"


class ModelNotConfiguredError(GatewayRequestError):
    """프로필에 그 taskType의 모델이 없다. 설정을 바꾸기 전에는 다시 보내도 같다(SPEC-011 R-14)."""

    code = "AI_MODEL_NOT_CONFIGURED"
    status = 503
    title = "Service Unavailable"


class CallerForbiddenError(GatewayRequestError):
    code = "AI_CALLER_FORBIDDEN"
    status = 403
    title = "Forbidden"


class ImagePathForbiddenError(GatewayRequestError):
    code = "AI_IMAGE_PATH_FORBIDDEN"
    status = 403
    title = "Forbidden"
