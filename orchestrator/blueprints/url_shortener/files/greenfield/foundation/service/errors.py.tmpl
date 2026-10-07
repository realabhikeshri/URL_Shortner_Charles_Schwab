# lineage: REQ-GF-001 / implement.foundation (see runs/ for the decision trail)
"""Domain errors and the consistent error envelope ``{"error": {code, message, request_id}}``."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from service.observability import get_logger, request_id_var

log = get_logger("errors")


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.headers = headers or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class GoneError(AppError):
    status_code = 410
    code = "gone"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidInputError(AppError):
    status_code = 422
    code = "invalid_input"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details is not None:
        body["details"] = details
    return {"error": body}


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status, headers=headers)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    429: "rate_limited",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return error_response(422, "validation_error", "request validation failed", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        return error_response(
            exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"error_type": type(exc).__name__})
        return error_response(500, "internal_error", "internal server error")
