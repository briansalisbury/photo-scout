"""
Security behaviour: script-block escaping, the plaintext-HTTP refusal, and the
heart service's request limits and error reticence.

Every check here corresponds to something in SECURITY.md. If one of them starts
failing, a promise made in that file has stopped being true.
"""
import importlib
import io
import contextlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hearts"))

import photo_scout as ps                               # noqa: E402
import photo_scout_ghost as pg                         # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------------------
print("=== script_json ===")
# The two sequences an HTML parser reacts to inside a script block, in the two
# places they can realistically arrive: a file path and a tag value.
hostile = {
    "/photos/a</script><img src=x onerror=alert(1)>.jpg": ["Lake"],
    "/photos/b.jpg": ["<!--<script>bad</script>"],
}
blob = ps.script_json(hostile)
check("no raw </ survives", "</" not in blob, blob[:120])
check("no raw <!-- survives", "<!--" not in blob)
check("it is still JSON, and unchanged after parsing",
      json.loads(blob) == hostile)
check("non-ASCII is escaped by default",
      "\\u00e9" in ps.script_json({"k": "café"}))
check("ensure_ascii=False keeps it literal, still escaped",
      ps.script_json({"k": "café </b>"}, ensure_ascii=False)
      == '{"k":"café <\\/b>"}')


# ---------------------------------------------------------------------------
print("\n=== the local report cannot be broken out of by a tag file ===")
LIB = Path("/tmp/sec_lib"); OUT = Path("/tmp/sec_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
LIB.mkdir(parents=True)
rng = np.random.default_rng(11)
folder = LIB / "2011 Wyoming"; folder.mkdir()

def shot(name):
    Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
         .resize((800, 560), Image.BICUBIC).save(folder / name, "JPEG", quality=90)

for i in range(4):
    shot(f"DSC_{i:04d}.JPG")

# '<', '>' and '!' are legal in a file name on Linux and macOS, so a photograph
# can genuinely be called this. It cannot contain a literal "</script>" - '/' is
# the one byte a POSIX file name may not hold - but '<!--' alone is enough to
# change how a parser reads the rest of the block. Windows forbids '<' and '>'
# outright, and there the check is moot.
BREAKOUT_NAME = "DSC_0009<!--<img src=x onerror=alert(1)>.JPG"
hostile_file = None
if os.name != "nt":
    try:
        shot(BREAKOUT_NAME)
        hostile_file = folder / BREAKOUT_NAME
    except OSError:
        pass                     # some filesystems refuse it; not a failure

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": 5.0 + self.n * 0.1, "nima_raw": 5.0,
                "subject_score": 90.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}

ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB), "--out", str(OUT)])

# A hand-edited tags.json is an ordinary thing to have; a hostile one is the
# same file with a different key. The report must survive both identically.
tags_file = OUT / ps.TAGS_FILE
entries = {str(folder / "DSC_0000.JPG") + "</script><b>": ["Unknown"]}
if hostile_file:
    entries[str(hostile_file)] = ["Lake"]
tags_file.write_text(json.dumps(entries), encoding="utf-8")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB), "--out", str(OUT), "--report-only"])

report = (OUT / "report.html").read_text(encoding="utf-8")
block = re.search(r"const TAGS = (.*?);\n", report, re.S)
check("the report embeds a TAGS block", block is not None)
if block:
    raw, parsed = block.group(1), json.loads(block.group(1))
    check("the block cannot be closed early",
          "</" not in raw and "<!--" not in raw, raw[:200])
    check("a key for no photograph in the report is dropped outright",
          not any("<b>" in k for k in parsed), str(list(parsed))[:200])
    if hostile_file:
        check("a real photograph with that name keeps its tags",
              parsed.get(str(hostile_file)) == ["Lake"], str(list(parsed))[:200])
        # Inside the JSON block a bare '<' is inert text - only '</' and '<!--'
        # matter there, and both are gone. In the markup it must be escaped.
        markup = report.replace(raw, "")
        check("and in the markup the name is escaped, not live",
              "<img src=x" not in markup and
              "&lt;img src=x onerror=alert(1)&gt;" in markup)
    else:
        print("SKIP  this filesystem would not take the hostile file name")


# ---------------------------------------------------------------------------
print("\n=== the model checkpoint is pinned ===")
# A checkpoint is loaded into a model, not merely read, so it is the one
# download worth verifying. file:// stands in for the real host.
SRC = Path("/tmp/sec_dl"); shutil.rmtree(SRC, ignore_errors=True); SRC.mkdir()
good = SRC / "good.bin"; good.write_bytes(b"a genuine checkpoint")
GOOD_SHA = ps.sha256_file(good)
dest = SRC / "cached.bin"

ps.download_verified(good.as_uri(), dest, GOOD_SHA, 1 << 20)
check("a matching download is kept", dest.read_bytes() == b"a genuine checkpoint")

dest.unlink()
try:
    ps.download_verified(good.as_uri(), dest, "0" * 64, 1 << 20)
    check("a mismatched download is refused", False)
except ps.ChecksumMismatch as exc:
    check("a mismatched download is refused", True)
    check("and says what it wanted and what it got",
          "0" * 64 in str(exc) and GOOD_SHA in str(exc))
    check("and names the override for a legitimate republish",
          "PHOTO_SCOUT_LAION_SHA256" in str(exc))
check("nothing is left behind after a refusal", not dest.exists())
check("not even a partial file", not list(SRC.glob("*.part")))

big = SRC / "big.bin"; big.write_bytes(b"x" * 5000)
try:
    ps.download_verified(big.as_uri(), dest, ps.sha256_file(big), 1024)
    check("an oversized download is cut off", False)
except ps.ChecksumMismatch as exc:
    check("an oversized download is cut off", "ceiling" in str(exc))
check("and leaves nothing behind either", not dest.exists())

check("the pinned digest is a real sha256",
      re.fullmatch(r"[0-9a-f]{64}", ps.LAION_WEIGHTS_SHA256) is not None,
      ps.LAION_WEIGHTS_SHA256)


print("\n=== plaintext endpoints ===")
for url, want in [("https://brians.life", False),
                  ("http://brians.life", True),
                  ("http://192.168.1.5:2368", True),
                  ("http://localhost:2368", False),
                  ("http://127.0.0.1:8091", False),
                  ("http://[::1]:2368", False),
                  ("brians.life", False)]:
    check(f"{url} {'is refused' if want else 'is allowed'}",
          pg.plaintext_endpoint(url) is want)


def ghost(*a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pg.main(["--out", str(OUT)] + list(a))
    return rc, buf.getvalue()

os.environ["GHOST_ADMIN_KEY"] = "6" * 24 + ":" + "a" * 64
rc, out = ghost("--site", "http://brians.life")
check("publishing over http exits 2", rc == 2, f"rc={rc}")
check("and says why", "in the clear" in out, out[-300:])
check("and names the escape hatch", "--insecure-http" in out)

rc, out = ghost("--site", "http://brians.life", "--dry-run")
check("a dry run is not blocked - it sends no key", rc != 2 or
      "in the clear" not in out, out[-200:])

os.environ["HEARTS_ADMIN_TOKEN"] = "t" * 32
rc, out = ghost("--site", "https://brians.life",
                "--hearts-url", "http://hearts.example/api/hearts",
                "--hearts-register-only")
check("a plaintext heart endpoint is refused too", rc == 2, f"rc={rc}")
check("and the message names the heart token",
      "heart admin token" in out, out[-300:])
os.environ.pop("HEARTS_ADMIN_TOKEN", None)
os.environ.pop("GHOST_ADMIN_KEY", None)


# ---------------------------------------------------------------------------
print("\n=== the heart service ===")
DB = Path("/tmp/sec_hearts/hearts.sqlite3")
shutil.rmtree(DB.parent, ignore_errors=True)
os.environ["HEARTS_DB"] = str(DB)
os.environ["HEARTS_ADMIN_TOKEN"] = "test-admin-token"
os.environ["HEARTS_MAX_BODY"] = "4096"

import app as hearts                                   # noqa: E402
importlib.reload(hearts)
hearts.init_db()
hearts.app.config["TESTING"] = True
C = hearts.app.test_client()

ADMIN = {"X-Admin-Token": "test-admin-token"}
PID = "a3f9c1d84b0e7726"
C.post("/api/hearts/_photos", headers=ADMIN,
       json={"photos": [{"photo_id": PID, "rel_path": "Wyoming/DSC_0989.NEF"}]})

r = C.get("/api/hearts")
check("responses carry nosniff", r.headers.get("X-Content-Type-Options") == "nosniff")
check("responses carry a referrer policy",
      r.headers.get("Referrer-Policy") == "no-referrer")

r = C.post("/api/hearts/_photos", headers=ADMIN,
           data=b'{"photos":[' + b'0,' * 4096 + b'0]}',
           content_type="application/json")
check("an oversized body is refused, not buffered", r.status_code == 413,
      f"status={r.status_code}")

# A database failure must not describe the database to whoever caused it.
real_db = hearts.db
hearts.db = lambda: (_ for _ in ()).throw(
    hearts.sqlite3.OperationalError("attempt to write a readonly database: "
                                    "/data/hearts.sqlite3"))
try:
    r = C.get("/api/hearts/_health")
    body = r.get_data(as_text=True)
    check("a database error answers 503", r.status_code == 503, body)
    check("and does not leak the path or the sqlite message",
          "hearts.sqlite3" not in body and "readonly" not in body, body)
finally:
    hearts.db = real_db

check("the service still works afterwards",
      C.get("/api/hearts").status_code == 200)

r = C.post(f"/api/hearts/{PID}", headers={"X-Heart-Token": str(uuid.uuid4())})
check("and a heart still lands", r.status_code == 200 and r.json["count"] == 1,
      str(r.json))

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
