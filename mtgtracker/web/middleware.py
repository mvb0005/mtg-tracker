"""The three HTTP middlewares. Registration order matters (later ones wrap
earlier ones), so create_app calls install() once at the point they used to
be declared."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from mtgtracker import db
from mtgtracker.web.auth import NotSignedIn
from mtgtracker.web.auth import resolve as resolve_user


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def identify(request: Request, call_next):
        """Who's asking (web/auth.py), as request.state.user. Static files
        and the kubelet's /healthz probe need no identity."""
        if not (request.url.path.startswith("/static/") or request.url.path == "/healthz"):
            state = request.app.state
            conn = db.connect(state.db_path, check_same_thread=False)
            try:
                request.state.user = resolve_user(
                    conn, state.auth, state.verifier,
                    client_host=request.client.host if request.client else None,
                    headers=request.headers, cookies=request.cookies,
                )
            except NotSignedIn as exc:
                return HTMLResponse(
                    state.templates.get_template("signed_out.html").render(reason=str(exc)),
                    status_code=401,
                )
            finally:
                conn.close()
        return await call_next(request)

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
