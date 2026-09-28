import sqlite3

from mtgtracker import db


def test_migrate_fresh_database_applies_all_migrations():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    version = db.migrate(conn)

    assert version >= 1
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {
        "transactions",
        "products",
        "lots",
        "decks",
        "deck_cards",
        "deck_costs",
        "dispositions",
        "receipts",
        "scryfall_cards",
    } <= tables


def test_migrate_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")

    first = db.migrate(conn)
    second = db.migrate(conn)

    assert first == second


def test_one_equipped_per_lot_is_enforced():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    db.migrate(conn)

    conn.execute(
        "INSERT INTO transactions (vendor, created_at, updated_at, subtotal_cents,"
        " tax_cents, total_cents, source) VALUES ('Test', 't', 't', 0, 0, 0, 'manual')"
    )
    conn.execute(
        "INSERT INTO lots (transaction_id, card_name, acquired_date)"
        " VALUES (1, 'Sol Ring', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO decks (name, created_at) VALUES ('Deck A', 't'), ('Deck B', 't')"
    )
    conn.execute(
        "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO deck_cards (deck_id, lot_id, equipped) VALUES (2, 1, 0)"
    )

    try:
        conn.execute(
            "UPDATE deck_cards SET equipped = 1 WHERE deck_id = 2 AND lot_id = 1"
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True

    assert raised, "equipping the same lot in two decks at once should be rejected"


def test_commander_migration_backfills_seeded_precons():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    first = sorted(db.MIGRATIONS_DIR.glob("*.sql"))[0]
    conn.executescript(first.read_text())
    conn.execute("PRAGMA user_version = 1")
    for name in ("Reign of Dragons", "Keen Engineering", "Blue Pile"):
        conn.execute(
            "INSERT INTO decks (name, format, created_at) VALUES (?, 'Commander', '2026-09-27')", (name,)
        )

    assert db.migrate(conn) >= 2
    commanders = dict(conn.execute("SELECT name, commander FROM decks").fetchall())
    assert commanders == {
        "Reign of Dragons": "Lathliss, Dragon Queen",
        "Keen Engineering": "Sai, Master Thopterist",
        "Blue Pile": None,
    }
