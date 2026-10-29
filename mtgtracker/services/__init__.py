"""The write flows behind the web forms (and, later, the JSON API): plain
functions over a connection and typed arguments, with no HTTP in them.

Each raises ServiceError (message safe to show the user) when the request is
rejected, and NotFoundError when something it names doesn't exist. Guards on
who owns what stay with the caller (web/deps.py), which runs them first.
Every function that writes takes `commit` (default True); with commit=False
nothing is committed or rolled back, so the caller decides -- a dry run is
"call it, read the result, conn.rollback()".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from mtgtracker import deckmods, scryfall


class ServiceError(Exception):
    """A request refused for a reason the user can act on."""


class NotFoundError(ServiceError):
    """Something the request names doesn't exist."""


class CodedError(ServiceError):
    """A refusal with its own machine-readable code and HTTP status, for the
    JSON API (`extra` goes into the error object). The web shows the message."""

    code = "rejected"
    status = 422

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


class UpstreamError(ServiceError):
    """Scryfall (or another outside service) couldn't be reached."""


@contextmanager
def translate_errors() -> Iterator[None]:
    """Deck-change errors and Scryfall outages as ServiceErrors."""
    try:
        yield
    except deckmods.ChangeError as exc:
        raise ServiceError(str(exc)) from exc
    except scryfall.RateLimited as exc:
        raise UpstreamError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("Couldn't reach Scryfall") from exc
