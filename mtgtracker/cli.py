from __future__ import annotations

from pathlib import Path

import typer

from mtgtracker import db
from mtgtracker.decks import reassemble
from mtgtracker.importers.order_wallet import import_order_file
from mtgtracker.importers.precon_decklist import seed_precon_deck_for_product

app = typer.Typer(help="Personal MTG collection and spending tracker.")

DEFAULT_DB_PATH = Path("data/mtgtracker.db")
DEFAULT_RECEIPTS_DIR = Path("receipts")


def _connect(db_path: Path) -> "sqlite3.Connection":
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    db.migrate(conn)
    return conn


@app.command("import-order")
def import_order(
    path: Path = typer.Argument(..., help="Path to a .order file"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
    receipts_dir: Path = typer.Option(DEFAULT_RECEIPTS_DIR, "--receipts", help="Receipts storage directory"),
):
    """Import an Apple Wallet .order file, and seed any known precon decks
    found among its products."""
    conn = _connect(db_path)
    result = import_order_file(conn, path, receipts_dir)

    if result.created:
        typer.echo(f"Created transaction {result.transaction_id}.")
    elif result.updated:
        typer.echo(f"Updated existing transaction {result.transaction_id}.")
    else:
        typer.echo(f"No-op: transaction {result.transaction_id} already up to date.")

    if not result.created:
        return

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


if __name__ == "__main__":
    app()
