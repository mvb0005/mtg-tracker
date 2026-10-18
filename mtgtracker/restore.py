"""Restore points: gzipped online-backup snapshots of the live database, kept
beside it in <db dir>/restore/, each with a .json sidecar.

    20261018T181503123456Z-people_add.db.gz
    20261018T181503123456Z-people_add.json   {created_at, action, changelog_seq, user_version, size}

A point plus the write log (mtgtracker/recover.py) rebuilds the database as
of any later moment: `changelog_seq` says which log rows the snapshot
already contains. Names carry no user data (just the endpoint name).

Retention (prune): the 50 newest points, plus the newest point of every UTC
calendar day that has one, forever, so an old state stays recoverable
however many points come after.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_RECENT = 50
MIN_AGE = timedelta(minutes=10)  # between points taken for ordinary writes
STAMP = "%Y%m%dT%H%M%S%fZ"
_NAME = re.compile(r"^(\d{8}T\d{12}Z)-(.*)\.db\.gz$")


def restore_dir(db_path: str | Path) -> Path:
    return Path(db_path).parent / "restore"


def _slug(action: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", action.lower()).strip("-")[:40] or "point"


def _parse_stamp(name: str) -> datetime | None:
    m = _NAME.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def take(db_path: str | Path, action: str, *, now: datetime | None = None) -> Path:
    """Snapshot the live DB (SQLite online backup, so safe while it's in use)
    into the restore dir as a gzip, written under a temp name then renamed."""
    db_path = Path(db_path)
    out_dir = restore_dir(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    created = now or datetime.now(UTC)
    stem = f"{created.strftime(STAMP)}-{_slug(action)}"
    tmp_db = out_dir / f".{stem}.db.tmp"
    tmp_gz = out_dir / f".{stem}.gz.tmp"
    final = out_dir / f"{stem}.db.gz"
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            has_log = dst.execute("SELECT 1 FROM sqlite_master WHERE name = 'changelog'").fetchone()
            seq = dst.execute("SELECT COALESCE(MAX(seq), 0) FROM changelog").fetchone()[0] if has_log else 0
            version = dst.execute("PRAGMA user_version").fetchone()[0]
        finally:
            dst.close()
            src.close()
        with open(tmp_db, "rb") as f_in, gzip.open(tmp_gz, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        tmp_gz.rename(final)
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_gz.unlink(missing_ok=True)
    sidecar = {
        "created_at": created.isoformat(), "action": _slug(action), "changelog_seq": seq,
        "user_version": version, "size": final.stat().st_size,
    }
    tmp_json = out_dir / f".{stem}.json.tmp"
    tmp_json.write_text(json.dumps(sidecar, sort_keys=True))
    tmp_json.rename(out_dir / f"{stem}.json")
    return final


def sidecar_path(point: Path) -> Path:
    return point.with_name(point.name.removesuffix(".db.gz") + ".json")


def list_points(directory: str | Path) -> list[dict]:
    """Oldest first: {path, name, created_at (datetime, UTC), action,
    changelog_seq, user_version, size}. A point with a missing or broken
    sidecar still lists (time and action from its name, the rest None)."""
    out = []
    directory = Path(directory)
    if not directory.is_dir():
        return out
    for path in directory.glob("*.db.gz"):
        created = _parse_stamp(path.name)
        if created is None:
            continue
        info: dict = {}
        with contextlib.suppress(OSError, ValueError):
            info = json.loads(sidecar_path(path).read_text())
        out.append({
            "path": path, "name": path.name, "created_at": created,
            "action": info.get("action", _NAME.match(path.name).group(2)),
            "changelog_seq": info.get("changelog_seq"), "user_version": info.get("user_version"),
            "size": info.get("size") or path.stat().st_size,
        })
    out.sort(key=lambda p: p["name"])
    return out


def prune(directory: str | Path, *, keep_recent: int = KEEP_RECENT) -> list[str]:
    """Delete every point except the `keep_recent` newest and the newest of
    each UTC day. Returns the deleted names. Never raises over a missing file."""
    points = list_points(directory)
    keep = {p["name"] for p in points[-keep_recent:]}
    newest_of_day: dict[str, str] = {}
    for p in points:  # oldest first, so the last one per day wins
        newest_of_day[p["created_at"].strftime("%Y%m%d")] = p["name"]
    keep |= set(newest_of_day.values())
    deleted = []
    for p in points:
        if p["name"] in keep:
            continue
        p["path"].unlink(missing_ok=True)
        sidecar_path(p["path"]).unlink(missing_ok=True)
        deleted.append(p["name"])
    return deleted


def maybe_take(db_path: str | Path, action: str, *, force: bool = False) -> str | None:
    """The web's entry point: take a point if `force` or the newest is older
    than MIN_AGE, then prune. Returns the file name it took (or the recent
    point that already covers this write), or None if it couldn't. Never
    raises -- a failed snapshot must not fail the request."""
    try:
        points = list_points(restore_dir(db_path))
        if not force and points and datetime.now(UTC) - points[-1]["created_at"] < MIN_AGE:
            return points[-1]["name"]
        path = take(db_path, action)
        prune(path.parent)
        return path.name
    except Exception:
        log.exception("restore point failed")
        return None
