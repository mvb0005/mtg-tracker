"""Assembling a deck from the owner's copies (see mtgtracker/assembly.py).
The caller has run the ownership guard on the deck."""

from __future__ import annotations

import sqlite3

from mtgtracker import assembly
from mtgtracker.services import CodedError, translate_errors


class StalePlan(CodedError):
    """Some chosen steps are no longer in the plan (the deck changed)."""

    code = "stale_plan"
    status = 409


def plan(conn: sqlite3.Connection, deck_id: int) -> assembly.AssemblyPlan:
    with translate_errors():
        return assembly.plan(conn, deck_id)


def assemble(
    conn: sqlite3.Connection, deck_id: int, line_ids: list[str], *, note: str | None = None,
    commit: bool = True,
) -> assembly.AssemblyResult:
    with translate_errors():
        try:
            return assembly.apply(conn, deck_id, line_ids, note=note, commit=commit)
        except assembly.AssemblyError as exc:
            if exc.stale:
                raise StalePlan(str(exc), stale=exc.stale) from exc
            raise
