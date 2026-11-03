"""RFC 9457 ProblemDetail 오류 응답(계약 `_common.yaml`의 ProblemDetail).

HTTP 서비스가 ai-gateway 하나뿐이라 `libs/common`이 아니라 여기에 둔다. 두 번째 HTTP 서비스가
생기면 옮긴다(plan 원칙 점검 V).
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ai_gateway.errors import GatewayRequestError, InvalidRequestError

_MEDIA_TYPE = "application/problem+json"


def install_problem_handlers(app: FastAPI) -> None:
    """요청 검증 실패와 GatewayRequestError를 ProblemDetail로 바꾼다."""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            (
                "/".join(str(p) for p in error["loc"] if p != "body") or "body",
                str(error["msg"]),
            )
            for error in exc.errors()
        ]
        # FastAPI 기본값 422 대신 계약의 400 AI_INVALID_REQUEST로 맞춘다(SPEC-009 R-2).
        return _problem(request, InvalidRequestError("request does not match the contract", errors))

    @app.exception_handler(GatewayRequestError)
    async def _gateway(request: Request, exc: GatewayRequestError) -> JSONResponse:
        return _problem(request, exc)


def _problem(request: Request, exc: GatewayRequestError) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": exc.title,
        "status": exc.status,
        "detail": str(exc),
        "instance": request.url.path,
        "code": exc.code,
    }
    if isinstance(exc, InvalidRequestError) and exc.errors:
        body["errors"] = [{"field": f, "message": m} for f, m in exc.errors]
    return JSONResponse(body, status_code=exc.status, media_type=_MEDIA_TYPE)
