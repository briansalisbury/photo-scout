# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brian Salisbury and contributors.
# Part of Photo Scout. This program comes with ABSOLUTELY NO WARRANTY.
# See the LICENSE file, or <https://www.gnu.org/licenses/>.
"""
Heart service for a published Photo Scout gallery.

A single-purpose counter: anonymous visitors heart photographs, the gallery
reads the totals back. Nothing else. It stores no personal data, no IP
addresses, and no raw visitor tokens.

Implements SPEC_hearts.md with the four open decisions resolved:

  1. Counts ARE visible to visitors.
  2. /api/hearts/top is authenticated (it exposes folder names).
  3. Deployed as a Docker sidecar beside Ghost.
  4. Listens on 8091, published on the host's loopback only.

Run it:

    HEARTS_DB=/data/hearts.sqlite3 HEARTS_ADMIN_TOKEN=... python app.py

or, in production, under Waitress (see the Dockerfile). Everything is
configured by environment variable so nothing secret lives in this file.

Design notes worth knowing before changing anything:

* Counts are DERIVED from rows, never stored in a counter column. A toggle
  therefore cannot drift out of sync with its own tally.
* The voter identifier on disk is sha256(salt + token). The raw token the
  browser sends is never written anywhere, so the database cannot be used to
  correlate a visitor with anything outside this service.
* Only photo_ids that the publish script has registered can be hearted, so a
  stranger cannot bloat the database with invented identifiers.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, request

# ---------------------------------------------------------------------------
# Configuration - all of it from the environment
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("HEARTS_DB", "/data/hearts.sqlite3")
# Guards the two admin endpoints. Without it set, they refuse every request
# rather than defaulting to open - a service that quietly loses its own
# authentication is worse than one that fails loudly.
ADMIN_TOKEN = os.environ.get("HEARTS_ADMIN_TOKEN", "")
# Writes per voter per hour. Generous for a person, restrictive for a script.
WRITE_LIMIT = int(os.environ.get("HEARTS_WRITE_LIMIT", "60"))
WRITE_WINDOW = 3600
PORT = int(os.environ.get("HEARTS_PORT", "8091"))
# Four is deliberate, not a default left unexamined. This runs on a single-core
# VM alongside Ghost and MySQL; more threads than that would add memory and
# context switching without adding throughput, and every request here is a
# sub-millisecond SQLite read.
THREADS = int(os.environ.get("HEARTS_THREADS", "4"))

PHOTO_ID = re.compile(r"^[0-9a-f]{16}$")
TOKEN_OK = re.compile(r"^[A-Za-z0-9-]{16,64}$")

SCHEMA_VERSION = 1

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS hearts (
    photo_id   TEXT    NOT NULL,
    voter      TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (photo_id, voter)
);
CREATE INDEX IF NOT EXISTS idx_hearts_photo ON hearts(photo_id);

CREATE TABLE IF NOT EXISTS photos (
    photo_id  TEXT PRIMARY KEY,
    rel_path  TEXT NOT NULL,
    added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    voter      TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions ON actions(voter, created_at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets readers carry on during a write, which matters when a page load
    # reads every count while someone else is clicking.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


class DataDirNotWritable(RuntimeError):
    """The mounted volume cannot be written to. Almost always ownership."""


def init_db() -> None:
    """Create the schema and mint the salt. Safe to call on every start."""
    parent = os.path.dirname(DB_PATH)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise DataDirNotWritable(str(exc)) from None
    try:
        conn = connect()
    except sqlite3.OperationalError as exc:
        # "unable to open database file" here almost never means a missing
        # directory - it means the directory is there but this user cannot
        # write to it. Docker creates a bind mount owned by root, and this
        # container deliberately runs as an unprivileged user, so a fresh
        # install hits this every time unless the host directory is chowned.
        raise DataDirNotWritable(str(exc)) from None
    try:
        conn.executescript(SCHEMA)
        # The salt is generated once, on first run, and never changes. Changing
        # it would orphan every existing heart, since voter identifiers are
        # derived from it.
        row = conn.execute("SELECT value FROM meta WHERE key='salt'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('salt', ?)",
                         (secrets.token_hex(32),))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()


def salt() -> str:
    return db().execute("SELECT value FROM meta WHERE key='salt'").fetchone()["value"]


# ---------------------------------------------------------------------------
# Identity and authentication
# ---------------------------------------------------------------------------

def voter_hash() -> str | None:
    """
    The visitor's opaque identifier, or None when they sent no usable token.

    The browser mints a UUID once and keeps it in localStorage. We store only
    sha256(salt + token), so the raw value never reaches disk.
    """
    raw = request.headers.get("X-Heart-Token", "")
    if not TOKEN_OK.match(raw):
        return None
    return hashlib.sha256((salt() + raw).encode()).hexdigest()


def admin_ok() -> bool:
    """
    Accepts the token as a header or as HTTP Basic, so both a script and a
    browser address bar can reach the admin endpoints with one credential.
    """
    if not ADMIN_TOKEN:
        return False                     # unset means closed, never open
    sent = request.headers.get("X-Admin-Token", "")
    if sent and hmac.compare_digest(sent, ADMIN_TOKEN):
        return True
    auth = request.authorization
    if auth and auth.password and hmac.compare_digest(auth.password, ADMIN_TOKEN):
        return True
    return False


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not admin_ok():
            resp = jsonify({"error": "unauthorised"})
            resp.status_code = 401
            # Prompts a browser for credentials; harmless to a script.
            resp.headers["WWW-Authenticate"] = 'Basic realm="photo-scout hearts"'
            return resp
        return fn(*a, **kw)
    return wrapper


def rate_limited(voter: str) -> bool:
    """
    True when this voter has spent their hourly write budget.

    Rows outside the window are deleted as we go, so the table stays small
    without a separate cleanup job.
    """
    now = int(time.time())
    conn = db()
    conn.execute("DELETE FROM actions WHERE created_at < ?", (now - WRITE_WINDOW,))
    n = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE voter=?",
                     (voter,)).fetchone()["c"]
    if n >= WRITE_LIMIT:
        conn.commit()
        return True
    conn.execute("INSERT INTO actions (voter, created_at) VALUES (?,?)", (voter, now))
    conn.commit()
    return False


def known_photo(photo_id: str) -> bool:
    return db().execute("SELECT 1 FROM photos WHERE photo_id=?",
                        (photo_id,)).fetchone() is not None


def count_for(photo_id: str) -> int:
    return db().execute("SELECT COUNT(*) AS c FROM hearts WHERE photo_id=?",
                        (photo_id,)).fetchone()["c"]


def err(message: str, code: int):
    resp = jsonify({"error": message})
    resp.status_code = code
    return resp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@app.get("/api/hearts")
def get_all():
    """Every non-zero count, plus which ones this browser has hearted."""
    conn = db()
    counts = {r["photo_id"]: r["c"] for r in conn.execute(
        "SELECT photo_id, COUNT(*) AS c FROM hearts GROUP BY photo_id")}
    voter = voter_hash()
    mine = []
    if voter:
        mine = [r["photo_id"] for r in conn.execute(
            "SELECT photo_id FROM hearts WHERE voter=?", (voter,))]
    resp = jsonify({"counts": counts, "mine": mine, "total": sum(counts.values())})
    # The gallery is a published Ghost page that may sit behind a CDN; the
    # counts must not be cached or every visitor sees the first one's numbers.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _write(photo_id: str, add: bool):
    if not PHOTO_ID.match(photo_id or ""):
        return err("malformed photo_id", 400)
    voter = voter_hash()
    if voter is None:
        return err("missing or malformed X-Heart-Token", 400)
    if not known_photo(photo_id):
        return err("unknown photo_id", 404)
    if rate_limited(voter):
        return err("too many actions, try later", 429)

    conn = db()
    if add:
        # INSERT OR IGNORE makes a second heart a no-op rather than an error,
        # so the client can be careless and the count stays right.
        conn.execute("INSERT OR IGNORE INTO hearts (photo_id, voter, created_at) "
                     "VALUES (?,?,?)", (photo_id, voter, int(time.time())))
    else:
        conn.execute("DELETE FROM hearts WHERE photo_id=? AND voter=?",
                     (photo_id, voter))
    conn.commit()
    resp = jsonify({"photo_id": photo_id, "count": count_for(photo_id),
                    "hearted": add})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/hearts/<photo_id>")
def add_heart(photo_id):
    return _write(photo_id, True)


@app.delete("/api/hearts/<photo_id>")
def remove_heart(photo_id):
    return _write(photo_id, False)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@app.get("/api/hearts/top")
@admin_required
def top():
    """
    Ranked list for the site owner. Authenticated because rel_path contains
    folder names, which the public endpoints deliberately never expose.
    """
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 1000)
    except ValueError:
        return err("limit must be a number", 400)
    rows = db().execute(
        """SELECT h.photo_id, COUNT(*) AS c,
                  COALESCE(p.rel_path, '') AS rel_path
             FROM hearts h LEFT JOIN photos p ON p.photo_id = h.photo_id
            GROUP BY h.photo_id
            ORDER BY c DESC, h.photo_id
            LIMIT ?""", (limit,)).fetchall()
    return jsonify({"top": [{"photo_id": r["photo_id"], "count": r["c"],
                             "rel_path": r["rel_path"]} for r in rows]})


@app.post("/api/hearts/_photos")
@admin_required
def register_photos():
    """
    The allowlist, written by the publish script.

    Additive on purpose: a photograph dropped from the gallery keeps its
    hearts, dormant, in case it comes back.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("photos"), list):
        return err("expected {\"photos\": [...]}", 400)
    now = int(time.time())
    rows = []
    for item in body["photos"]:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("photo_id", ""))
        if not PHOTO_ID.match(pid):
            continue
        rows.append((pid, str(item.get("rel_path", ""))[:500], now))
    conn = db()
    conn.executemany(
        "INSERT INTO photos (photo_id, rel_path, added_at) VALUES (?,?,?) "
        "ON CONFLICT(photo_id) DO UPDATE SET rel_path=excluded.rel_path",
        rows)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
    return jsonify({"registered": len(rows), "rejected": len(body["photos"]) - len(rows),
                    "known_photos": total})


@app.get("/api/hearts/_health")
def health():
    try:
        db().execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "schema_version": SCHEMA_VERSION})
    except sqlite3.Error as exc:
        return err(f"database unavailable: {exc}", 503)


@app.errorhandler(sqlite3.Error)
def _db_error(exc):
    return err(f"database unavailable: {exc}", 503)


def _explain_data_dir(exc: Exception) -> None:
    parent = os.path.dirname(DB_PATH) or "."
    try:
        uid, gid = os.getuid(), os.getgid()
    except AttributeError:                     # not POSIX
        uid = gid = "?"
    print("=" * 68, flush=True)
    print(f"hearts: cannot open {DB_PATH}", flush=True)
    print(f"        {exc}", flush=True)
    print("", flush=True)
    print(f"This process runs as uid {uid}, and {parent} is not writable by it.",
          flush=True)
    try:
        st = os.stat(parent)
        print(f"        {parent} is owned by uid {st.st_uid}, gid {st.st_gid}, "
              f"mode {oct(st.st_mode & 0o777)}", flush=True)
    except OSError:
        print(f"        {parent} could not even be inspected", flush=True)
    print("", flush=True)
    print("Docker creates a bind-mounted directory owned by root, and this", flush=True)
    print("container deliberately runs unprivileged. On the HOST, run:", flush=True)
    print("", flush=True)
    print(f"    sudo chown -R {uid}:{gid} ./hearts-data", flush=True)
    print("    docker compose up -d hearts", flush=True)
    print("=" * 68, flush=True)


if __name__ == "__main__":
    try:
        init_db()
    except DataDirNotWritable as exc:
        _explain_data_dir(exc)
        # Exit 0, not 1. The problem needs a human on the host; restarting
        # forever just buries the explanation under a scrolling log.
        raise SystemExit(0)
    if not ADMIN_TOKEN:
        print("WARNING: HEARTS_ADMIN_TOKEN is not set - the admin endpoints "
              "will refuse every request.", flush=True)
    try:
        from waitress import serve
        print(f"hearts: serving on 0.0.0.0:{PORT} (db {DB_PATH})", flush=True)
        serve(app, host="0.0.0.0", port=PORT, threads=THREADS,
              # Keep the accept queue short: on one core, a backlog that grows
              # without bound turns a traffic spike into swap pressure for
              # Ghost. Refusing fast is kinder than queueing slowly.
              connection_limit=64, channel_timeout=30)
    except ImportError:
        # Development only. Waitress is what runs in the container.
        app.run(host="127.0.0.1", port=PORT)
