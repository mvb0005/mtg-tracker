"""The HTTP middlewares. Registration order matters (later ones wrap
earlier ones), so create_app calls install() once at the point they used to
be declared."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from mtgtracker import audit, db
from mtgtracker.web.auth import ActForError, NotSignedIn
from mtgtracker.web.auth import resolve as resolve_user

log = logging.getLogger(__name__)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _audit(request: Request, response) -> None:
    """One audit row for a write that succeeded: status < 400, and a
    redirect that isn't carrying an error. It has its own connection and
    must never break or change the response -- the write already happened --
    so any failure is logged and swallowed."""
    try:
        if request.method in SAFE_METHODS or response.status_code >= 400:
            return
        query = parse_qs(urlsplit(response.headers.get("location", "")).query)
        if query.get("error"):
            return
        principal, actor = getattr(request.state, "user", None), getattr(request.state, "actor", None)
        if principal is None or actor is None:
            return
        endpoint = request.scope.get("endpoint")
        summary = {}
        if query.get("notice"):
            summary["notice"] = query["notice"][0]
        preview = getattr(request.state, "audit_preview", None)
        if preview:
            summary["preview"] = preview
        conn = db.connect(request.app.state.db_path, check_same_thread=False)
        try:
            audit.record(
                conn, actor_id=actor.id, principal_id=principal.id, method=request.method,
                path=request.url.path, action=getattr(endpoint, "__name__", "unknown"),
                target=request.scope.get("path_params") or None, summary=summary or None,
            )
        finally:
            conn.close()
    except Exception:
        log.exception("audit log write failed")


def _refuse(status: int, code: str, message: str, as_json: bool):
    if as_json:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
    return PlainTextResponse(message, status_code=status)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def identify(request: Request, call_next):
        """Who's asking (web/auth.py): request.state.user is the principal,
        request.state.actor who really asks. Static files and the kubelet's
        /healthz probe need no identity. After the
        response, a successful write is audited."""
        if not (request.url.path.startswith("/static/") or request.url.path == "/healthz"):
            state = request.app.state
            conn = db.connect(state.db_path, check_same_thread=False)
            try:
                request.state.user, request.state.actor = resolve_user(
                    conn, state.auth, state.verifier,
                    client_host=request.client.host if request.client else None,
                    headers=request.headers, cookies=request.cookies,
                )
            except NotSignedIn as exc:
                return HTMLResponse(
                    state.templates.get_template("signed_out.html").render(reason=str(exc)),
                    status_code=401,
                )
            except ActForError as exc:
                return _refuse(exc.status, exc.code, exc.message, exc.as_json)
            finally:
                conn.close()
        response = await call_next(request)
        _audit(request, response)
        return response

    @app.middleware("http")
    async def static_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Versioned URLs (?v=<content hash>) never change; anything else
            # must be revalidated so a stale stylesheet can't stick around.
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if "v" in request.query_params else "no-cache"
            )
        return response

    @app.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if cross_site or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                return PlainTextResponse("Cross-origin request rejected", status_code=403)
        return await call_next(request)
