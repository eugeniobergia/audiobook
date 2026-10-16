import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str  # stable, machine-readable, e.g. "not_found"
    message: str  # human-readable
    details: Any | None = None


class ErrorResponse(BaseModel):
    """The body of every error response. Also used to document errors in OpenAPI."""

    error: ErrorDetail


class AppError(Exception):
    """An error our own code raises on purpose, e.g. a missing audiobook (404)."""

    def __init__(
        self, status_code: int, code: str, message: str, details: Any = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = jsonable_encoder(details)
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def _code_for_status(status_code: int) -> str:
    """401 -> "unauthorized", 404 -> "not_found", 405 -> "method_not_allowed"."""
    try:
        return HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    except ValueError:
        return "http_error"


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's HTTPException (FastAPI's subclasses it), so this also covers
    # unknown routes (404) and wrong methods (405) raised by the router.
    assert isinstance(exc, StarletteHTTPException)
    return error_response(
        exc.status_code,
        _code_for_status(exc.status_code),
        str(exc.detail),
        headers=exc.headers,  # keep e.g. the Allow header on a 405
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        422, "validation_error", "The request is invalid.", details=exc.errors()
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Log the traceback for us; never send internals to the client.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An unexpected error occurred.")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
