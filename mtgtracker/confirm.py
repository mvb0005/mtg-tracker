"""Confirmed dry runs: a system actor can't run a risky action without first
being shown its exact preview, and what runs is exactly what was previewed.

A risky operation is a function that runs its service call with
commit=False and returns (result, preview), the preview being a
JSON-serializable dict of what it will do. For a system actor the first
request is rolled back and answered with the preview plus a token; the
repeat request carries the token (`confirm`), is run again, and is committed
only if its fresh preview digest equals the one the token was issued for.

Token: base64url(JSON {"exp": unix seconds, "pv": sha256 hex of the
canonical preview, "sig": hex HMAC-SHA256}). The HMAC (key: a process
secret, MTG_CONFIRM_SECRET or random per start) covers the canonical JSON of
{actor_id, principal_id, method, path, params, preview_sha256 (= pv), exp}. So
a token works only for the same actor, principal, route and form fields
(`confirm` excluded), within 15 minutes, and a changed preview is detected
by comparing digests. Tokens don't survive a restart, by design.

A token is single-use: once its action commits, the same token is refused
(per process; a token can't outlive its 15 minutes or a restart anyway).
And because a confirmed request runs the operation a second time, a risky
operation must only touch the database -- no files, network calls or other
side effects that a rollback can't undo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

TTL_SECONDS = 15 * 60

# Tokens whose action has committed, with their expiry (pruned as they lapse).
_spent: dict[str, int] = {}


def _now() -> float:
    return time.time()  # a seam for tests


def risky(endpoint: Callable) -> Callable:
    """Mark a route function as needing a confirmed dry run from system
    actors. Its body must go through deps.run_risky; a test enforces both."""
    endpoint.__risky__ = True
    return endpoint


class ConfirmRequired(Exception):
    """Answer 409 with the preview and a token to confirm it."""

    def __init__(self, code: str, message: str, preview: dict, token: str):
        super().__init__(message)
        self.code, self.message, self.preview, self.token = code, message, preview, token


@dataclass(frozen=True)
class Context:
    actor_id: int
    principal_id: int
    method: str
    path: str
    params: tuple[tuple[str, str], ...]  # form/JSON fields minus `confirm`, sorted


def params_of(fields: Iterable[tuple[str, object]]) -> tuple[tuple[str, str], ...]:
    """Sorted (name, value) pairs, `confirm` dropped; an uploaded file counts by name."""
    out = []
    for key, value in fields:
        if key == "confirm":
            continue
        out.append((key, value if isinstance(value, str) else f"<file:{getattr(value, 'filename', '')}>"))
    return tuple(sorted(out))


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(preview: dict) -> str:
    return hashlib.sha256(_canonical(preview)).hexdigest()


def _sig(secret: bytes, ctx: Context, pv: str, exp: int) -> str:
    body = {
        "actor_id": ctx.actor_id, "principal_id": ctx.principal_id, "method": ctx.method,
        "path": ctx.path, "params": ctx.params, "preview_sha256": pv, "exp": exp,
    }
    return hmac.new(secret, _canonical(body), hashlib.sha256).hexdigest()


def issue(secret: bytes, ctx: Context, preview: dict) -> str:
    exp = int(_now()) + TTL_SECONDS
    pv = digest(preview)
    raw = _canonical({"exp": exp, "pv": pv, "sig": _sig(secret, ctx, pv, exp)})
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify(secret: bytes, token: str, ctx: Context) -> str | None:
    """The preview digest the token was issued for, or None if the token is
    malformed, forged, for another actor/principal/route/params, or expired."""
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        exp, pv, sig = data["exp"], data["pv"], data["sig"]
        if not (isinstance(exp, int) and isinstance(pv, str) and isinstance(sig, str)):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    if not hmac.compare_digest(sig, _sig(secret, ctx, pv, exp)):
        return None
    return pv if exp >= _now() else None


def run(
    secret: bytes, conn, ctx: Context, confirm: str | None, operation: Callable[[object], tuple[object, dict]]
):
    """Run `operation(conn)` (uncommitted) for a system actor under the
    dry-run protocol. Returns (result, preview) with the work committed, or
    rolls back and raises ConfirmRequired."""
    try:
        result, preview = operation(conn)
    except BaseException:
        conn.rollback()
        raise
    if not confirm:
        conn.rollback()
        raise ConfirmRequired(
            "confirm_required", "this action needs confirming: review the preview, then repeat the"
            " request with the confirm token", preview, issue(secret, ctx, preview),
        )
    pv = verify(secret, confirm, ctx)
    if pv is None:
        conn.rollback()
        raise ConfirmRequired(
            "confirm_invalid", "the confirm token is invalid, expired, or for a different request;"
            " here is a fresh preview", preview, issue(secret, ctx, preview),
        )
    if not hmac.compare_digest(pv, digest(preview)):
        conn.rollback()
        raise ConfirmRequired(
            "preview_changed", "things changed since the preview you confirmed; review the new one",
            preview, issue(secret, ctx, preview),
        )
    now = int(_now())
    for spent, exp in list(_spent.items()):
        if exp < now:
            del _spent[spent]
    if confirm in _spent:
        conn.rollback()
        raise ConfirmRequired(
            "confirm_used", "that confirm token was already used; here is a fresh preview",
            preview, issue(secret, ctx, preview),
        )
    conn.commit()
    _spent[confirm] = now + TTL_SECONDS
    return result, preview
