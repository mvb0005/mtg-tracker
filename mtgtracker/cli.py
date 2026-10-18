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


@app.command("add-system-user")
def add_system_user(
    name: str = typer.Argument(..., help="Display name, e.g. Claude"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Add a system user (an automated actor with no email that acts for people)."""
    conn = _connect(db_path)
    try:
        user = users.create_system(conn, name)
    except users.UserError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Added system user {user.id}: {user.display_name}")


@app.command("add-credential")
def add_credential(
    user_id: int = typer.Argument(..., help="System user id"),
    cf_client_id: str = typer.Option(
        ..., "--cf-client-id", help="Cloudflare Access service token Client ID (not the secret)"
    ),
    label: str = typer.Option(..., "--label", help="What this credential is for"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Let a Cloudflare Access service token act as a system user."""
    conn = _connect(db_path)
    try:
        cred = users.add_credential(conn, user_id, cf_client_id, label)
    except users.UserError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Added credential {cred.id} ({cred.label}) for user {cred.user_id}")


@app.command("revoke-credential")
def revoke_credential(
    credential_id: int = typer.Argument(..., help="Credential id (see list-credentials)"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Revoke a service credential."""
    conn = _connect(db_path)
    try:
        users.revoke_credential(conn, credential_id)
    except users.UserError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Revoked credential {credential_id}")


@app.command("list-credentials")
def list_credentials(
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """List service credentials (Client IDs are identifiers, not secrets)."""
    conn = _connect(db_path)
    for c in users.list_credentials(conn):
        state = f"revoked {c.revoked_at[:10]}" if c.revoked_at else "active"
        used = c.last_used_at or "never"
        typer.echo(f"{c.id}\tuser {c.user_id}\t{c.cf_client_id}\t{c.label}\t{state}\tlast used {used}")


@app.command("disable-user")
def disable_user(
    user: str = typer.Argument(..., help="Email or id"),
    enable: bool = typer.Option(False, "--enable", help="Turn access back on"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Turn a user's access off (or back on); the owner can't be disabled."""
    conn = _connect(db_path)
    try:
        changed = users.set_disabled(conn, _user_id(conn, user), not enable)
    except users.UserError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{changed.display_name} is now {'enabled' if enable else 'disabled'}")


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


@app.command("record-purchase")
def record_purchase(
    path: Path = typer.Argument(..., help="Cart file: a store CSV, a checkout paste, or a plain list"),
    vendor: str = typer.Option(..., "--vendor", help="Where it was bought"),
    total: str = typer.Option(..., "--total", help="Total paid in dollars, e.g. 12.34"),
    tax: str = typer.Option("0", "--tax", help="Tax in dollars"),
    shipping: str = typer.Option("0", "--shipping", help="Shipping in dollars"),
    date: str = typer.Option("", "--date", help="Purchase date YYYY-MM-DD (default: today)"),
    user: str = typer.Option("", "--user", help="Email or id of the buyer (default: user 1)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be recorded; write nothing"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db", help="SQLite database path"),
):
    """Record singles you bought as cards in the buyer's collection, with
    their cost. Only the collection changes; no deck is touched."""
    import httpx

    from mtgtracker import purchases
    from mtgtracker.importers import cart_text
    from mtgtracker.money import format_cents, parse_cents

    def cents(value: str, label: str) -> int:
        try:
            return parse_cents(value.strip().lstrip("$").replace(",", "") or "0")
        except (ArithmeticError, ValueError):
            raise typer.BadParameter(f"{label} {value!r} isn't an amount in dollars and cents") from None

    conn = _connect(db_path)
    owner_id = _user_id(conn, user)
    try:
        plan = purchases.plan(
            conn, path.read_text(), owner_id=owner_id, vendor=vendor, date=date or None,
            tax_cents=cents(tax, "--tax"), shipping_cents=cents(shipping, "--shipping"),
            total_cents=cents(total, "--total"),
        )
        summary = purchases.summarize(plan) if dry_run else purchases.record(conn, plan)
    except (purchases.PurchaseError, cart_text.CartParseError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise typer.BadParameter("couldn't reach Scryfall; nothing was recorded") from exc
    for p in plan.lines:
        where = f"{p.set_code.upper()} #{p.collector_number}" if p.pinned else f"needs printing ({p.reason})"
        typer.echo(f"{p.line.qty} x {p.card_name}  {where}  {format_cents(p.cost_basis_cents)}")
    for w in plan.wanted_by:
        typer.echo(f"Wanted by {w['deck']}: {w['copies']} of these (nothing added to it)")
    head = "Would record" if dry_run else f"Recorded purchase {summary.transaction_id}:"
    typer.echo(f"{head} {summary.copies} cards on {summary.lines} lines, {format_cents(summary.total_cents)}"
               f" ({summary.unpinned} need a printing).")


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


@app.command("restore-points")
def restore_points(db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db")):
    """List the restore points kept beside the database (newest last)."""
    from mtgtracker import restore

    points = restore.list_points(restore.restore_dir(db_path))
    if not points:
        typer.echo("No restore points.")
    for p in points:
        seq = "?" if p["changelog_seq"] is None else p["changelog_seq"]
        typer.echo(
            f"{p['created_at']:%Y-%m-%d %H:%M:%S}Z  seq {seq:>6}  {p['size']:>9,} B"
            f"  {p['action']}  {p['name']}"
        )


@app.command("recover")
def recover_cmd(
    point: Path = typer.Argument(..., help="A restore point (.db.gz)"),
    out: Path = typer.Option(..., "--out", help="New database file to write (must not exist)"),
    log_db: Path = typer.Option(DEFAULT_DB_PATH, "--log-db", help="DB whose write log to replay (read-only)"),
    until_seq: int = typer.Option(None, "--until-seq", help="Replay log rows up to and including this seq"),
    until_time: str = typer.Option(None, "--until-time", help="Replay log rows up to this ISO time"),
    until_audit: int = typer.Option(None, "--until-audit", help="Stop just before this audit id"),
):
    """Rebuild the database as of a moment: a restore point plus the write log. Never touches the live DB."""
    from mtgtracker import recover as recovery

    try:
        s = recovery.recover(
            point, log_db, out, until_seq=until_seq, until_time=until_time, until_audit=until_audit
        )
    except recovery.RecoverError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Wrote {out}: applied {s.applied} log rows ({s.point_seq} -> {s.final_seq}).")
    for table, (n, live) in s.counts.items():
        mark = "" if live == n else f"   (live: {live})"
        typer.echo(f"  {table:<22}{n:>8}{mark}")
    if s.fk_problems:
        typer.echo(f"WARNING: {len(s.fk_problems)} foreign key problems, e.g. {s.fk_problems[0]}")


@app.command("undo-batch")
def undo_batch_cmd(
    audit_id: int = typer.Argument(..., help="audit_log id of the request to undo"),
    yes: bool = typer.Option(False, "--yes", help="Do it (default: a dry run that writes nothing)"),
    db_path: Path = typer.Option(DEFAULT_DB_PATH, "--db"),
):
    """Reverse one web request's changes, by its audit id."""
    from mtgtracker import recover as recovery

    conn = _connect(db_path)
    try:
        preview = recovery.undo_batch(conn, audit_id, commit=yes)
        if not yes:
            conn.rollback()
    except recovery.RecoverError as exc:
        conn.rollback()
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()
    for step in preview:
        typer.echo(f"  {step['undo']:<10}{step['table']} row {step['row_id']}  (log seq {step['seq']})")
    n = len(preview)
    typer.echo(f"Undid {n} changes." if yes else f"Would undo {n} changes. Re-run with --yes.")


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
