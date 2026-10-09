from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import typer

from mtgtracker import db, users
from mtgtracker.decks import reassemble
from mtgtracker.importers.order_wallet import import_order_file
from mtgtracker.importers.precon_decklist import seed_precon_deck_for_product

app = typer.Typer(help="Personal MTG collection and spending tracker.")

DEFAULT_DB_PATH = Path("data/mtgtracker.db")
DEFAULT_RECEIPTS_DIR = Path("receipts")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    db.migrate(conn)
    return conn


def _user_id(conn: sqlite3.Connection, spec: str) -> int:
    """--user: an email or a numeric id; empty means user 1."""
    spec = spec.strip()
    if not spec:
        return users.OWNER_ID
    user = users.get(conn, int(spec)) if spec.isdigit() else users.by_email(conn, spec)
    if user is None:
        raise typer.BadParameter(f"no user {spec!r}")
    return user.id


@app.command("add-user")
def add_user(
    name: str = typer.Argument(..., help="Display name"),
    email: str = typer.Option("", "--email", help="Email they sign in with (Cloudflare Access)"),
    admin: bool = typer.Option(False, "--admin", help="Make them an admin"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Add a person (users only exist if added -- sign-in is an allowlist)."""
    conn = _connect(db_path)
    try:
        user = users.create(conn, name, email or None, is_admin=admin)
    except users.UserError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Added user {user.id}: {user.display_name}" + (f" <{user.email}>" if user.email else ""))


@app.command("rename-user")
def rename_user(
    user: str = typer.Argument(..., help="Email or id"),
    name: str = typer.Argument(..., help="New display name"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Change a person's display name (user 1 starts as "Owner")."""
    conn = _connect(db_path)
    renamed = users.rename(conn, _user_id(conn, user), name)
    typer.echo(f"User {renamed.id} is now {renamed.display_name}")


@app.command("import-collection")
def import_collection(
    path: Path = typer.Argument(..., help="Collection CSV (ManaBox, Moxfield, Archidekt, or name/quantity)"),
    user: str = typer.Option("", "--user", help="Email or id of whose collection it is (default: user 1)"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Add every card in a collection export to a user's collection (one
    card per copy, purchase price as cost basis). Each file imports once."""
    from mtgtracker.importers import collection_csv

    conn = _connect(db_path)
    owner_id = _user_id(conn, user)
    try:
        summary = collection_csv.import_collection(
            conn, path.read_bytes(), owner_id=owner_id, imported_by=users.OWNER_ID, filename=path.name
        )
    except collection_csv.CollectionImportError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(summary.message())


@app.command("import-order")
def import_order(
    path: Path = typer.Argument(..., help="Path to a .order file"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
    receipts_dir: Path = typer.Option(DEFAULT_RECEIPTS_DIR, "--receipts", help="Receipts storage directory"),
    user: str = typer.Option("", "--user", help="Email or id of the user it belongs to (default: user 1)"),
):
    """Import an Apple Wallet .order file, and seed any known precon decks
    found among its products."""
    conn = _connect(db_path)
    result = import_order_file(conn, path, receipts_dir, owner_id=_user_id(conn, user))

    if result.created:
        typer.echo(f"Created transaction {result.transaction_id}.")
    elif result.updated:
        typer.echo(f"Updated existing transaction {result.transaction_id}.")
    else:
        typer.echo(f"No-op: transaction {result.transaction_id} already up to date.")

    # Always attempt seeding, not just on a freshly created transaction:
    # seed_precon_deck_for_product() is itself a no-op for products that
    # already have lots, which makes a failed first attempt (e.g. a
    # Scryfall fetch error after the transaction/receipt were already
    # committed) recoverable just by re-running this command.
    products = conn.execute(
        "SELECT id, display_name FROM products WHERE transaction_id = ?",
        (result.transaction_id,),
    ).fetchall()

    for product in products:
        seeded = seed_precon_deck_for_product(conn, product["id"])
        if seeded is None:
            continue
        deck_id, report = seeded
        typer.echo(f"  Seeded deck '{product['display_name']}' -> deck {deck_id}")
        if report.has_issues():
            typer.echo(
                f"    Needs your input: {len(report.ambiguous)} ambiguous, "
                f"{len(report.not_found)} not found, "
                f"basics not printing-resolved: {report.basics_unresolved}"
            )
            for entry in report.ambiguous:
                reason = entry.get("reason", "multiple printings in set")
                candidates = ", ".join(
                    f"{c['set']}#{c['collector_number']}" for c in entry["candidates"]
                )
                typer.echo(f"      ambiguous: {entry['name']} ({reason}) -> {candidates}")
            for name in report.not_found:
                typer.echo(f"      not found: {name}")


@app.command("deck-notes")
def deck_notes(
    deck_id: int = typer.Argument(..., help="Deck id (see list-decks)"),
    set_file: Path = typer.Option(
        None, "--set", help="Replace the notes with this file's text ('-' for stdin)"
    ),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Print a deck's notes, or replace them with --set."""
    conn = _connect(db_path)
    if conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone() is None:
        raise typer.BadParameter(f"no deck {deck_id}")
    if set_file is not None:
        text = (sys.stdin.read() if str(set_file) == "-" else set_file.read_text()).strip()
        conn.execute("UPDATE decks SET notes = ? WHERE id = ?", (text or None, deck_id))
        conn.commit()
        typer.echo(f"Saved {len(text)} characters of notes for deck {deck_id}.")
        return
    typer.echo(conn.execute("SELECT notes FROM decks WHERE id = ?", (deck_id,)).fetchone()[0] or "")


@app.command("list-decks")
def list_decks(db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db")):
    """List all decks."""
    conn = _connect(db_path)
    decks = conn.execute("SELECT id, name, format FROM decks ORDER BY id").fetchall()
    for deck in decks:
        count = conn.execute(
            "SELECT COUNT(*) FROM deck_cards WHERE deck_id = ? AND equipped = 1", (deck["id"],)
        ).fetchone()[0]
        typer.echo(f"{deck['id']}: {deck['name']} ({deck['format']}) - {count} cards equipped")


@app.command()
def reassemble_deck(
    deck_id: int,
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
):
    """Show what cards need to be pulled back to reassemble a deck's list."""
    conn = _connect(db_path)
    missing = reassemble(conn, deck_id)
    if not missing:
        typer.echo("Deck is fully assembled.")
        return
    for row in missing:
        where = row["current_deck_id"] if row["current_deck_id"] is not None else "nowhere (unassigned)"
        typer.echo(f"{row['card_name']}: currently in deck {where}")


@app.command()
def serve(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
    host: str = typer.Option("127.0.0.1", help="Bind address (local-only by default)"),
    port: int = typer.Option(8000, help="Port"),
    reload: bool = typer.Option(False, help="Reload on code changes (development)"),
):
    """Serve the web UI."""
    import uvicorn

    if reload:
        os.environ["MTGTRACKER_DB"] = str(db_path)
        uvicorn.run("mtgtracker.web.app:app_from_env", factory=True, host=host, port=port, reload=True,
                    proxy_headers=False)
    else:
        from mtgtracker.web.app import create_app

        # proxy_headers=False: never trust X-Forwarded-For, so a request can't
        # pose as a loopback client (which cloudflare auth treats as the owner).
        uvicorn.run(create_app(db_path), host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    app()
