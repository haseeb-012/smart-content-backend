"""Structured errors. Every error response has the shape:

    {"error": {"code": "credits_exhausted", "message": "...", "details": {...}}}

Frontends switch on `code` (see docs/API_CONTRACTS.md), never on the message.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class PlanLimitError(AppError):
    """A plan limit or plan-gated feature blocked the request (FR-BILL-04)."""

    status_code = 403
    code = "plan_limit"


class CreditsExhausted(AppError):
    status_code = 402
    code = "credits_exhausted"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


def _body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            _body("validation_error", "Invalid request.", {"errors": errors}), status_code=422
        )
