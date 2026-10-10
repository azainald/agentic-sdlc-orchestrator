"""Application errors and the consistent error JSON format.

Every error the API returns has the shape: {"error": {"code": ..., "message": ...}}
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class: carries everything needed to build the HTTP response."""

    status_code = 500

    def __init__(self, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.headers = headers or {}


class ValidationFailed(AppError):
    status_code = 422


class NotFound(AppError):
    status_code = 404


class Gone(AppError):
    status_code = 410


class Conflict(AppError):
    status_code = 409


class RateLimited(AppError):
    status_code = 429


class ServiceUnavailable(AppError):
    status_code = 503


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Make every error (ours, FastAPI's, Starlette's) use the same JSON shape."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"] if part != "body")
        message = f"{location}: {first['msg']}" if location else first["msg"]
        return error_response(422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        code = codes.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))
