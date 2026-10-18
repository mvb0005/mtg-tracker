"""The profile page, the admin's edit of another person, and the API."""

import pytest
from fastapi.testclient import TestClient

from mtgtracker import audit, db, users
from mtgtracker.web.app import create_app
from mtgtracker.web.auth import AuthConfig
from tests.test_multiuser import _client, two_users  # noqa: F401
from tests.test_system_user import CLIENT_ID, world  # noqa: F401


def _last_audit(db_path, principal=1):
    conn = db.connect(db_path)
    row = audit.recent(conn, principal)[0]
    conn.close()
    return row


def _user(db_path, user_id):
    conn = db.connect(db_path)
    u = users.get(conn, user_id)
    conn.close()
    return u


def _loc(resp):
    return resp.headers["location"]


# --- /profile ---------------------------------------------------------------

def test_rename_yourself_notice_and_audit(db_path, two_users):  # noqa: F811
    admin = _client(db_path, 1)
    page = admin.get("/profile").text
    assert 'value="Owner"' in page
    assert "Only an admin can change a sign-in email" in page
    assert 'href="/people/1"' in page
    resp = admin.post("/profile", data={"display_name": "  Pat  "}, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=Saved." in _loc(resp)
    assert _user(db_path, 1).display_name == "Pat"
    row = _last_audit(db_path)
    assert row["actor_id"] == 1
    assert row["notice"] == "Renamed Owner to Pat"


@pytest.mark.parametrize("name", ["", "   ", "x" * 61])
def test_rename_validation(db_path, two_users, name):  # noqa: F811
    resp = _client(db_path, 1).post("/profile", data={"display_name": name}, follow_redirects=False)
    assert "error=" in _loc(resp)
    assert _user(db_path, 1).display_name == "Owner"
    assert _user(db_path, 1).display_name != name


def test_friend_edits_only_their_own_profile(db_path, two_users):  # noqa: F811
    friend = two_users["friend"]
    _client(db_path, friend.id).post("/profile", data={"display_name": "Alexa"})
    assert _user(db_path, friend.id).display_name == "Alexa"
    assert _user(db_path, 1).display_name == "Owner"


def test_header_links_to_profile(db_path, two_users):  # noqa: F811
    assert 'class="whoami" href="/profile"' in _client(db_path, 1).get("/decks").text


def test_single_mode_profile_and_no_edit(db_path, cache_dir):
    client = TestClient(create_app(db_path, auth=AuthConfig(mode="single")))
    assert client.get("/profile").status_code == 200
    assert client.post("/profile", data={"display_name": "Pat"}).status_code == 200
    assert _user(db_path, 1).display_name == "Pat"
    assert client.get("/people/1/edit").status_code == 403
    assert client.post("/people/1/edit", data={"display_name": "X"}).status_code == 403


# --- /people/{id}/edit ------------------------------------------------------

def test_non_admin_and_friend_cannot_edit(db_path, two_users):  # noqa: F811
    friend = _client(db_path, two_users["friend"].id)
    assert friend.get("/people/1/edit").status_code == 403
    resp = friend.post("/people/1/edit", data={"display_name": "Hax", "email": "hax@example.com"})
    assert resp.status_code == 403
    own = f"/people/{two_users['friend'].id}/edit"
    assert friend.post(own, data={"display_name": "A"}).status_code == 403
    assert _user(db_path, 1).display_name == "Owner"


def test_admin_edits_name_and_email_of_someone_who_never_signed_in(db_path, two_users):  # noqa: F811
    admin, fid = _client(db_path, 1), two_users["friend"].id
    assert f"/people/{fid}/edit" in admin.get(f"/people/{fid}").text
    assert "I understand" not in admin.get(f"/people/{fid}/edit").text
    resp = admin.post(f"/people/{fid}/edit", data={"display_name": "Alexander", "email": "new@example.com"},
                      follow_redirects=False)
    assert "notice=Saved." in _loc(resp)
    conn = db.connect(db_path)
    assert users.by_email(conn, "alex@example.com") is None
    assert users.by_email(conn, "NEW@example.com").id == fid
    conn.close()
    notice = _last_audit(db_path)["notice"]
    assert notice == "Renamed Alex to Alexander; Changed Alexander's sign-in email"
    assert "example.com" not in notice


def test_duplicate_email_refused_case_insensitively(db_path, two_users):  # noqa: F811
    conn = db.connect(db_path)
    users.create(conn, "Bo", "bo@example.com")
    conn.close()
    fid = two_users["friend"].id
    resp = _client(db_path, 1).post(
        f"/people/{fid}/edit", data={"display_name": "Alex", "email": "BO@Example.com"},
        follow_redirects=False)
    assert "error=" in _loc(resp)
    assert "already" in _loc(resp)
    assert _user(db_path, fid).email == "alex@example.com"


def test_own_email_and_system_users_cannot_be_edited(world):  # noqa: F811
    admin = _client(world["db_path"], 1)
    page = admin.get("/people/1/edit")
    assert page.status_code == 200
    assert "Your own sign-in email can't be changed" in page.text
    resp = admin.post("/people/1/edit", data={"display_name": "Owner", "email": "me@example.com"},
                      follow_redirects=False)
    assert "error=" in _loc(resp)
    assert _user(world["db_path"], 1).email is None
    cid = world["claude"].id
    assert admin.get(f"/people/{cid}/edit").status_code == 403
    bad = {"display_name": "X", "email": "x@example.com"}
    assert admin.post(f"/people/{cid}/edit", data=bad).status_code == 403
    assert admin.get("/people/999/edit").status_code == 404


def test_signed_in_person_needs_confirmation(db_path, two_users):  # noqa: F811
    fid = two_users["friend"].id
    conn = db.connect(db_path)
    users.touch(conn, fid)
    conn.close()
    admin = _client(db_path, 1)
    assert 'name="confirm_email"' in admin.get(f"/people/{fid}/edit").text
    data = {"display_name": "Alex", "email": "moved@example.com"}
    resp = admin.post(f"/people/{fid}/edit", data=data, follow_redirects=False)
    assert "error=" in _loc(resp)
    assert _user(db_path, fid).email == "alex@example.com"
    resp = admin.post(f"/people/{fid}/edit", data={**data, "confirm_email": "1"}, follow_redirects=False)
    assert "notice=" in _loc(resp)
    conn = db.connect(db_path)
    assert users.by_email(conn, "alex@example.com") is None
    assert users.by_email(conn, "moved@example.com").id == fid
    conn.close()
    assert _last_audit(db_path)["notice"] == "Changed Alex's sign-in email"


def test_set_email_service_rules(db_path, two_users):  # noqa: F811
    conn = db.connect(db_path)
    with pytest.raises(users.UserError):
        users.set_email(conn, two_users["friend"].id, "not-an-email")
    system = users.create_system(conn, "Claude")
    with pytest.raises(users.UserError):
        users.set_email(conn, system.id, "c@example.com")
    assert users.set_email(conn, two_users["friend"].id, "Alex@Example.com").email == "Alex@Example.com"
    conn.close()


def test_cli_set_email(db_path, two_users):  # noqa: F811
    from typer.testing import CliRunner

    from mtgtracker.cli import app
    runner = CliRunner()
    fid = str(two_users["friend"].id)
    ok = runner.invoke(app, ["set-email", fid, "cli@example.com", "--db", str(db_path)])
    assert ok.exit_code == 0
    dup = runner.invoke(app, ["set-email", "1", "CLI@example.com", "--db", str(db_path)])
    assert dup.exit_code != 0
    conn = db.connect(db_path)
    assert users.by_email(conn, "cli@example.com").id == two_users["friend"].id
    conn.close()


# --- system actor and the API ------------------------------------------------

def _claude(w, **headers):
    client = TestClient(create_app(w["db_path"], auth=AuthConfig(mode="dev")))
    client.headers.update({"X-Dev-Service": CLIENT_ID, "X-Act-For": "1", **headers})
    return client


def test_api_profile_get_and_rename_by_system_actor(world):  # noqa: F811
    claude = _claude(world)
    got = claude.get("/api/v1/profile").json()["profile"]
    assert got["id"] == 1
    assert "email" not in got
    person = _client(world["db_path"], 1).get("/api/v1/profile").json()["profile"]
    assert "email" in person
    resp = claude.put("/api/v1/profile", json={"display_name": "Pat"})
    assert resp.status_code == 200
    assert resp.json()["profile"]["display_name"] == "Pat"
    row = _last_audit(world["db_path"])
    assert row["actor_id"] == world["claude"].id
    assert row["principal_id"] == 1
    assert row["notice"] == "Renamed Owner to Pat"
    assert claude.put("/api/v1/profile", json={"display_name": " "}).status_code in (400, 422)


def test_api_dry_run_writes_nothing(world):  # noqa: F811
    resp = _claude(world).put("/api/v1/profile?dry_run=1", json={"display_name": "Pat"})
    assert resp.json()["dry_run"] is True
    assert resp.json()["profile"]["display_name"] == "Pat"
    assert _user(world["db_path"], 1).display_name == "Owner"
    conn = db.connect(world["db_path"])
    sql = "SELECT COUNT(*) FROM audit_log WHERE actor_id = ?"
    assert conn.execute(sql, (world["claude"].id,)).fetchone()[0] == 0
    conn.close()


def test_system_actor_cannot_edit_people(world):  # noqa: F811
    claude = _claude(world)
    fid = world["friend"].id
    assert claude.get(f"/people/{fid}/edit").status_code == 403
    bad = {"display_name": "X", "email": "x@example.com"}
    assert claude.post(f"/people/{fid}/edit", data=bad).status_code == 403
    assert _user(world["db_path"], fid).email == "alex@example.com"
