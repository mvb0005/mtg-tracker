"""The JSON API (/api/v1): the same services and guards as the web pages,
for scripts and the system user. One APIRouter per area, all included with
prefix /api/v1 by `install`. Errors are `{"error": {"code", "message"}}`
(common.install_errors); `?dry_run=1` on any write runs it and rolls back."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi

from mtgtracker.web.api.v1 import assembly, cards, decks, meta, people, profile, purchases, suggestions
from mtgtracker.web.api.v1.common import install_errors

PREFIX = "/api/v1"
router = APIRouter()
for _module in (meta, decks, cards, people, profile, purchases, assembly, suggestions):
    router.include_router(_module.router)


def openapi_schema() -> dict:
    """The OpenAPI document of the /api/v1 routes alone."""
    spec = FastAPI(title="mtgtracker JSON API", version="1", openapi_url=None, docs_url=None, redoc_url=None)
    spec.include_router(router, prefix=PREFIX)
    return get_openapi(title=spec.title, version=spec.version, routes=spec.routes)


def install(app: FastAPI) -> None:
    install_errors(app)
    app.include_router(router, prefix=PREFIX)

    @app.get(f"{PREFIX}/openapi.json", include_in_schema=False)
    def openapi():
        # Behind the identity middleware like everything else: not public.
        if not hasattr(app.state, "api_openapi"):
            app.state.api_openapi = openapi_schema()
        return app.state.api_openapi
