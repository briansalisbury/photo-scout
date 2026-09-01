"""
The heart service: API, abuse handling and persistence.

Runs the real Flask app against a real SQLite file in /tmp. Nothing is mocked
except the clock, and only where a test needs to reach across an hour boundary.
"""
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hearts"))

DB = Path("/tmp/hearts_test/hearts.sqlite3")
shutil.rmtree(DB.parent, ignore_errors=True)
os.environ["HEARTS_DB"] = str(DB)
os.environ["HEARTS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["HEARTS_WRITE_LIMIT"] = "60"

import app as hearts                                   # noqa: E402
importlib.reload(hearts)
hearts.init_db()
hearts.app.config["TESTING"] = True
C = hearts.app.test_client()

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


ADMIN = {"X-Admin-Token": "test-admin-token"}
P1 = "a3f9c1d84b0e7726"
P2 = "7b21e0c9aa145d3f"
P3 = "0000ffff0000ffff"


def tok():
    return str(uuid.uuid4())


def V(t):
    return {"X-Heart-Token": t}


print("=== registering the allowlist ===")
r = C.post("/api/hearts/_photos", json={"photos": [
    {"photo_id": P1, "rel_path": "2011-06-28 - Wyoming/DSC_0989.NEF"},
    {"photo_id": P2, "rel_path": "2010-03-12 - Arches/DSC_0004.NEF"},
    {"photo_id": P3, "rel_path": "2016-03-09 - Mustang/DSC_0100.NEF"},
    {"photo_id": "NOT-HEX", "rel_path": "junk"},
    {"photo_id": "short", "rel_path": "junk"},
    "not even an object",
]}, headers=ADMIN)
check("registration succeeds", r.status_code == 200, str(r.status_code))
check("valid ids registered", r.json["registered"] == 3, str(r.json))
check("malformed ids rejected, not stored", r.json["rejected"] == 3, str(r.json))

r = C.post("/api/hearts/_photos", json={"photos": []})
check("no credentials means 401", r.status_code == 401, str(r.status_code))
r = C.post("/api/hearts/_photos", json={"photos": []},
           headers={"X-Admin-Token": "wrong"})
check("wrong token means 401", r.status_code == 401)
r = C.post("/api/hearts/_photos", json={"nope": 1}, headers=ADMIN)
check("a malformed body is a 400, not a crash", r.status_code == 400, str(r.json))

print("\n=== hearting ===")
a = tok()
r = C.get("/api/hearts")
check("an empty gallery reports nothing", r.json == {"counts": {}, "mine": [], "total": 0},
      str(r.json))

r = C.post(f"/api/hearts/{P1}", headers=V(a))
check("first heart counts one", r.status_code == 200 and r.json["count"] == 1,
      str(r.json))
check("and reports itself as hearted", r.json["hearted"] is True)

r = C.post(f"/api/hearts/{P1}", headers=V(a))
check("hearting twice is not an error", r.status_code == 200, str(r.status_code))
check("and does not double count", r.json["count"] == 1, str(r.json))

b = tok()
C.post(f"/api/hearts/{P1}", headers=V(b))
check("a second browser adds a second heart",
      C.get("/api/hearts").json["counts"][P1] == 2,
      str(C.get("/api/hearts").json["counts"]))

r = C.get("/api/hearts", headers=V(a))
check("mine lists only this browser's hearts", r.json["mine"] == [P1], str(r.json))
r = C.get("/api/hearts")
check("without a token, mine is empty", r.json["mine"] == [], str(r.json))
check("counts are public even without a token", r.json["counts"][P1] == 2)

print("\n=== un-hearting ===")
r = C.delete(f"/api/hearts/{P1}", headers=V(a))
check("removing works", r.status_code == 200 and r.json["count"] == 1, str(r.json))
check("and reports itself as not hearted", r.json["hearted"] is False)
r = C.delete(f"/api/hearts/{P1}", headers=V(a))
check("removing twice is not an error", r.status_code == 200 and r.json["count"] == 1,
      str(r.json))
check("the other browser's heart is untouched",
      C.get("/api/hearts", headers=V(b)).json["mine"] == [P1])

print("\n=== a toggle sequence always lands on the truth ===")
c = tok()
for i in range(7):
    (C.post if i % 2 == 0 else C.delete)(f"/api/hearts/{P2}", headers=V(c))
# odd number of steps starting with an add -> hearted
check("seven toggles leave it hearted",
      C.get("/api/hearts", headers=V(c)).json["mine"] == [P2],
      str(C.get("/api/hearts", headers=V(c)).json))
check("count agrees with the rows",
      C.get("/api/hearts").json["counts"][P2] == 1)
raw = sqlite3.connect(DB).execute(
    "SELECT COUNT(*) FROM hearts WHERE photo_id=?", (P2,)).fetchone()[0]
check("the derived count matches the database exactly", raw == 1, str(raw))

print("\n=== rejecting what should be rejected ===")
for bad, why in ((". ./../etc/passwd", "path traversal"),
                 ("' OR 1=1 --", "sql fragment"),
                 ("a" * 200, "oversized"),
                 ("ZZZZZZZZZZZZZZZZ", "non-hex"),
                 ("a3f9c1d84b0e772", "fifteen characters"),
                 ("a3f9c1d84b0e77260", "seventeen characters")):
    r = C.post(f"/api/hearts/{bad}", headers=V(tok()))
    check(f"{why} refused", r.status_code in (400, 404, 405), f"{bad!r} -> {r.status_code}")

r = C.post(f"/api/hearts/{'f' * 16}", headers=V(tok()))
check("a well-formed but unregistered id is a 404", r.status_code == 404, str(r.json))
r = C.post(f"/api/hearts/{P1}")
check("a write with no voter token is a 400", r.status_code == 400, str(r.json))
r = C.post(f"/api/hearts/{P1}", headers={"X-Heart-Token": "short"})
check("a malformed voter token is a 400", r.status_code == 400)
r = C.post(f"/api/hearts/{P1}", headers={"X-Heart-Token": "<script>alert(1)</script>xx"})
check("a voter token with markup is a 400", r.status_code == 400)

print("\n=== the ranked list is private ===")
r = C.get("/api/hearts/top")
check("no credentials means 401", r.status_code == 401, str(r.status_code))
check("and it says how to authenticate",
      "WWW-Authenticate" in r.headers, str(dict(r.headers)))
import base64
basic = base64.b64encode(b"anyone:test-admin-token").decode()
r = C.get("/api/hearts/top", headers={"Authorization": f"Basic {basic}"})
check("http basic works, so a browser can reach it", r.status_code == 200,
      str(r.status_code))
r = C.get("/api/hearts/top?limit=2", headers=ADMIN)
check("ranked most-hearted first",
      [x["count"] for x in r.json["top"]] ==
      sorted([x["count"] for x in r.json["top"]], reverse=True), str(r.json["top"]))
check("it includes the paths, which is why it is private",
      any("Wyoming" in x["rel_path"] for x in r.json["top"]), str(r.json["top"]))
check("limit is honoured", len(r.json["top"]) <= 2)
r = C.get("/api/hearts/top?limit=abc", headers=ADMIN)
check("a nonsense limit is a 400, not a crash", r.status_code == 400)

print("\n=== the public endpoints never leak a path ===")
pub = json.dumps(C.get("/api/hearts", headers=V(a)).json)
check("no folder name in the public payload",
      not any(w in pub for w in ("Wyoming", "Arches", "Mustang", "NEF", "/")), pub[:120])

print("\n=== rate limiting ===")
os.environ["HEARTS_WRITE_LIMIT"] = "5"
hearts.WRITE_LIMIT = 5
d = tok()
codes = [C.post(f"/api/hearts/{P3}", headers=V(d)).status_code for _ in range(8)]
check("the budget runs out", 429 in codes, str(codes))
check("and it runs out at the limit, not before", codes[:5] == [200] * 5, str(codes))
e = tok()
check("a different voter is unaffected",
      C.post(f"/api/hearts/{P3}", headers=V(e)).status_code == 200)
check("reads are never rate limited", C.get("/api/hearts").status_code == 200)
hearts.WRITE_LIMIT = 60

print("\n=== what the database does NOT contain ===")
blob = DB.read_bytes()
# The WAL may hold recent writes that have not been checkpointed.
for extra in (DB.parent / "hearts.sqlite3-wal",):
    if extra.exists():
        blob += extra.read_bytes()
_leaked = [t for t in (a, b, c, d, e) if t.encode() in blob]
check("no raw voter token anywhere in the file", not _leaked,
      f"{len(_leaked)} raw tokens found on disk" if _leaked else "")
check("no IP address column even exists",
      "ip" not in [r[1].lower() for r in
                   sqlite3.connect(DB).execute("PRAGMA table_info(hearts)")],
      str([r[1] for r in sqlite3.connect(DB).execute("PRAGMA table_info(hearts)")]))
check("voter identifiers are sha256 hex",
      all(re.fullmatch(r"[0-9a-f]{64}", r[0]) for r in
          sqlite3.connect(DB).execute("SELECT voter FROM hearts")))

print("\n=== the salt ===")
s1 = sqlite3.connect(DB).execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
hearts.init_db()
s2 = sqlite3.connect(DB).execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
check("generated once and never regenerated", s1 == s2,
      "" if s1 == s2 else "the salt changed on restart")
check("it is long enough to be a salt", len(s1) >= 64, str(len(s1)))

print("\n=== persistence ===")
before = C.get("/api/hearts").json["counts"]
importlib.reload(hearts)
hearts.init_db()
hearts.app.config["TESTING"] = True
C = hearts.app.test_client()
check("hearts survive a restart of the service",
      C.get("/api/hearts").json["counts"] == before, str(C.get("/api/hearts").json))
check("and this browser is still recognised",
      C.get("/api/hearts", headers=V(b)).json["mine"] == [P1])

# A rescore rewrites scores.sqlite3 and republishes; the allowlist is rewritten
# too. Hearts must not care.
C.post("/api/hearts/_photos", json={"photos": [
    {"photo_id": P1, "rel_path": "renamed folder/DSC_0989.NEF"}]}, headers=ADMIN)
check("re-registering a photograph keeps its hearts",
      C.get("/api/hearts").json["counts"].get(P1) == 1,
      str(C.get("/api/hearts").json["counts"]))
check("dropping a photograph from the gallery keeps its hearts dormant",
      C.get("/api/hearts").json["counts"].get(P2) == 1)

print("\n=== health ===")
r = C.get("/api/hearts/_health")
check("health check answers 200", r.status_code == 200 and r.json["ok"] is True,
      str(r.json))

print("\n=== a service with no admin token configured is closed, not open ===")
hearts.ADMIN_TOKEN = ""
check("admin endpoints refuse everything",
      C.get("/api/hearts/top", headers=ADMIN).status_code == 401 and
      C.post("/api/hearts/_photos", json={"photos": []}, headers=ADMIN).status_code == 401)
check("but the public endpoints keep working",
      C.get("/api/hearts").status_code == 200)

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
