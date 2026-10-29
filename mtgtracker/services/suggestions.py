"""Deck suggestions: add a batch, accept one onto the Modify page, vote,
dismiss or reopen. The caller has already run require_own_deck."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from mtgtracker import suggestions
from mtgtracker.services import CodedError, NotFoundError, ServiceError, translate_errors


class CardsNotFound(CodedError):
    code = "cards_not_found"


@contextmanager
def _errors() -> Iterator[None]:
    with translate_errors():
        try:
            yield
        except suggestions.CardsNotFound as exc:
            raise CardsNotFound(str(exc), names=exc.names) from exc
        except suggestions.SuggestionError as exc:
            if str(exc).startswith("no such suggestion"):
                raise NotFoundError(str(exc)) from exc
            raise ServiceError(str(exc)) from exc


def create(
    conn: sqlite3.Connection, deck_id: int, items: list[dict], *, suggested_by: int | None,
    commit: bool = True,
) -> dict:
    with _errors():
        result = suggestions.create(conn, deck_id, items, suggested_by=suggested_by, commit=commit)
    n = len(result["created"])
    notice = f"Suggested {n} card{'s' if n != 1 else ''}."
    if result["skipped"]:
        notice += f" Skipped {len(result['skipped'])}."
    return {**result, "notice": notice}


def accept(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> str:
    with _errors():
        name = suggestions.get(conn, deck_id, suggestion_id)["card_name"]
        suggestions.accept(conn, deck_id, suggestion_id, commit=commit)
    return f"Queued {name} on the Modify page; apply it there to make the next version."


def dismiss(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> str:
    with _errors():
        name = suggestions.get(conn, deck_id, suggestion_id)["card_name"]
        suggestions.dismiss(conn, deck_id, suggestion_id, commit=commit)
    return f"Dismissed {name}."


def reopen(conn: sqlite3.Connection, deck_id: int, suggestion_id: int, *, commit: bool = True) -> str:
    with _errors():
        name = suggestions.get(conn, deck_id, suggestion_id)["card_name"]
        suggestions.reopen(conn, deck_id, suggestion_id, commit=commit)
    return f"{name} is open again, with no vote."


def vote(
    conn: sqlite3.Connection, deck_id: int, suggestion_id: int, verdict: str, note: str | None = None,
    *, commit: bool = True,
) -> str:
    with _errors():
        name = suggestions.vote(conn, deck_id, suggestion_id, verdict, note, commit=commit)["card_name"]
    return f"Liked {name}." if verdict == "up" else f"Passed on {name}."
