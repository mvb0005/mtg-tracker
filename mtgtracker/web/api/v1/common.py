"""What every JSON API route shares: the error shape, the write wrapper that
makes `?dry_run=1` work, and the exception handlers that turn services' and
FastAPI's errors into `{"error": {"code", "message"}}` -- for /api/ paths
only; every other path keeps its HTML/plain behavior."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from mtgtracker import users
from mtgtracker.services import CodedError, NotFoundError, ServiceError, UpstreamError
from mtgtracker.web import deps
from mtgtracker.web.middleware import api_error

_STATUS_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 413: "too_large", 422: "invalid_request",
}


class ApiError(Exception):
    """An error with its own code (and extra fields) for the JSON API."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def user_json(user: users.User) -> dict:
    return {"id": user.id, "name": user.display_name, "kind": user.kind, "is_admin": user.is_admin}


def write(
    request: Request, conn: sqlite3.Connection, fn: Callable[[], dict], status: int = 200
) -> JSONResponse:
    """Run a write as one transaction. `fn` calls services with commit=False
    and returns the response body (built before anything is committed). A
    real run commits; with `?dry_run=1` it rolls back and the same body comes
    back plus "dry_run": true. Any exception rolls back. A body "notice" is
    what the audit row records."""
    dry = deps.is_dry_run(request)
    try:
        body = fn()
    except BaseException:
        conn.rollback()
        raise
    if dry:
        conn.rollback()
        body = {**body, "dry_run": True}
    else:
        conn.commit()
        if body.get("notice"):
            request.state.audit_notice = body["notice"]
    return JSONResponse(jsonable_encoder(body), status_code=status)


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{where}: {err.get('msg', 'invalid')}" if where else str(err.get("msg", "invalid")))
    return "; ".join(parts) or "invalid request"


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if not deps.is_api(request):
            return await http_exception_handler(request, exc)
        response = api_error(
            exc.status_code, _STATUS_CODES.get(exc.status_code, "error"), str(exc.detail)
        )
        for name, value in (exc.headers or {}).items():  # e.g. Allow on a 405
            response.headers[name] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        if not deps.is_api(request):
            return await request_validation_exception_handler(request, exc)
        return api_error(422, "validation_error", _validation_message(exc))

    @app.exception_handler(ApiError)
    async def api_failure(request: Request, exc: ApiError):
        return api_error(exc.status, exc.code, exc.message, **exc.extra)

    @app.exception_handler(ServiceError)
    async def service_failure(request: Request, exc: ServiceError):
        if not deps.is_api(request):
            raise exc  # the web routes catch their own; keep any miss a plain 500
        if isinstance(exc, CodedError):
            return api_error(exc.status, exc.code, str(exc), **exc.extra)
        if isinstance(exc, NotFoundError):
            return api_error(404, "not_found", str(exc))
        if isinstance(exc, UpstreamError):
            return api_error(502, "upstream_unavailable", str(exc))
        return api_error(422, "rejected", str(exc))

    @app.exception_handler(deps.DryRun)
    async def dry_run_preview(request: Request, exc: deps.DryRun):
        return JSONResponse(
            jsonable_encoder({"notice": exc.result, "preview": exc.preview, "dry_run": True})
        )


class Fields:
    """The bits of a request run_risky reads from a form: the confirm token
    and the fields a token is bound to (none, for a DELETE)."""

    def __init__(self, confirm: str | None = None, **fields: str):
        self._fields = {**fields, **({"confirm": confirm} if confirm else {})}

    def get(self, key: str, default=None):
        return self._fields.get(key, default)

    def multi_items(self):
        return list(self._fields.items())
