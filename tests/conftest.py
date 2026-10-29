import shutil
from pathlib import Path

import httpx
import pytest

# Imported at module load, before any test patches httpx.Client: TestClient
# subclasses httpx.Client, so importing it after install_transport() would
# route the app's own test requests through the fake transport.
from fastapi.testclient import TestClient

from mtgtracker import db, scryfall
from mtgtracker.importers.order_wallet import import_order_file
from mtgtracker.importers.precon_decklist import seed_precon_deck_for_product
from mtgtracker.web.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"
_REAL_HTTPX_CLIENT = httpx.Client  # captured before any test patches it


@pytest.fixture(autouse=True)
def scryfall_throttle(monkeypatch):
    """Every test starts with Scryfall's throttle fresh (no cooldown left by
    another test's 429) and never actually waits between requests."""
    monkeypatch.setattr(scryfall, "_last_sent", {})
    monkeypatch.setattr(scryfall, "_cooldown_until", 0.0)
    monkeypatch.setattr(scryfall, "_pause", lambda _seconds: None)


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    """A file-backed DB (per-request web connections can't share :memory:)
    with the scrubbed order imported and both precons seeded from the
    committed Scryfall fixture -- no network. Built once per session; tests
    get a copy via `db_path`."""
    tmp = tmp_path_factory.mktemp("seeded")
    cache_dir = tmp / "scryfall_cache"
    cache_dir.mkdir()
    shutil.copy(FIXTURES / "scryfall_fdc.json", cache_dir / "fdc.json")

    path = tmp / "template.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scryfall, "CACHE_DIR", cache_dir)
        conn = db.connect(path)
        # Seeding commits per card; skip fsync so setup stays fast.
        conn.execute("PRAGMA synchronous = OFF")
        db.migrate(conn)
        result = import_order_file(conn, FIXTURES / "order_scrubbed.order", tmp / "receipts")
        for product in conn.execute(
            "SELECT id FROM products WHERE transaction_id = ?", (result.transaction_id,)
        ).fetchall():
            seed_precon_deck_for_product(conn, product["id"])
        conn.close()
    return path


@pytest.fixture
def db_path(seeded_template, tmp_path):
    path = tmp_path / "test.db"
    shutil.copy(seeded_template, path)
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point scryfall at a per-test cache holding only the committed fdc
    fixture (never the gitignored real cache), and make any network fetch
    fail loudly. Tests that need a fetch can install their own transport via
    `scryfall_transport`."""
    path = tmp_path / "scryfall_cache"
    path.mkdir()
    shutil.copy(FIXTURES / "scryfall_fdc.json", path / "fdc.json")
    monkeypatch.setattr(scryfall, "CACHE_DIR", path)

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    install_transport(monkeypatch, httpx.MockTransport(no_network))
    return path


@pytest.fixture
def client(db_path, cache_dir):
    """Web app over a copy of the seeded DB, with the fixture Scryfall cache."""
    return TestClient(create_app(db_path))


def install_transport(monkeypatch, transport: httpx.BaseTransport) -> None:
    """Make scryfall.py's httpx.Client use `transport` (replacing any
    transport installed earlier in the same test)."""

    class _Client(_REAL_HTTPX_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(scryfall.httpx, "Client", _Client)
