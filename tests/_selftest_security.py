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
print("\n=== nor by a folder name ===")
# Folder names reach further than file names do: the folder index puts them in
# a <script> block as JSON, in a tile label, and in the "open folder" link as a
# file:/// URL. A folder arrives on a disk rather than from the photographer's
# keyboard, and on Linux and macOS it may legally hold quotes and angle
# brackets, so each of those three is a place a name could get out.
EVIL_LIB = Path("/tmp/sec_evil"); EVIL_OUT = Path("/tmp/sec_evil_out")
for d in (EVIL_LIB, EVIL_OUT):
    shutil.rmtree(d, ignore_errors=True)

# A folder name cannot hold '/', so a literal '</script>' is impossible here;
# these are what a folder CAN be called. The double-quote is the one that
# matters most - it ends an HTML attribute, and the "open folder" URL is one.
EVIL_FOLDERS = [
    '2011-01-01 - <!--<script>window.PWNED=1;<',
    '2011-02-02 - "><script>window.PWNED=2<',
    "2011-03-03 - '+window.PWNED=3+'",
    '2011-04-04 - <img src=x onerror="window.PWNED=4">',
]
made = []
if os.name != "nt":
    for name in EVIL_FOLDERS:
        try:
            d = EVIL_LIB / name
            d.mkdir(parents=True)
        except OSError:
            continue             # some filesystems refuse it; not a failure
        for i in range(2):
            Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
                 .resize((800, 560), Image.BICUBIC) \
                 .save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90)
        made.append(name)

if not made:
    print("SKIP  this filesystem would not take a hostile folder name")
else:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ps.main(["--root", str(EVIL_LIB), "--out", str(EVIL_OUT)])
    ev = (EVIL_OUT / "report.html").read_text(encoding="utf-8")

    groups = re.search(r"const GROUPS = (.*?);\n", ev, re.S)
    check("the report embeds a GROUPS block", groups is not None)
    if groups:
        raw = groups.group(1)
        # The two sequences that change where an HTML parser thinks the script
        # ends. Neither can appear in valid JSON outside a string, and both
        # survive escaping as the same string, so removing them costs nothing.
        check("the script block cannot be closed early",
              "</" not in raw and "<!--" not in raw, raw[:160])
        check("and the folder names are all still there",
              len(json.loads(raw)) == len(made), raw[:160])

    # The one attribute that used to interpolate a path without escaping it.
    urls = re.findall(r'data-folderurl="([^"]*)"', ev)
    check("every folder URL survived as one attribute",
          len(urls) == 2 * len(made), f"{len(urls)} for {len(made)} folders")
    check("and none of them carries a live quote or bracket",
          not any(c in u for u in urls for c in '"<>'), str(urls[:1]))
    check("the same goes for the open-folder links",
          not re.search(r'<a href="file:[^"]*[<>]', ev))
    # Inside the JSON block a bare '<script' is inert text: only '</' and
    # '<!--' move an HTML parser out of script data, and both are gone above.
    # Everywhere else the name is markup, and there it must be escaped.
    markup = ev.replace(groups.group(1), "") if groups else ev
    check("no folder name became live markup",
          "<script>window.PWNED" not in markup and "<img src=x" not in markup)
    check("and in the markup the names are escaped, not live",
          "&lt;img src=x onerror=" in markup)

    from playwright.sync_api import sync_playwright        # noqa: E402
    dialogs, page_errors = [], []
    with sync_playwright() as _pw:
        _br = _pw.chromium.launch()
        _pg = _br.new_context().new_page()
        _pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        _pg.on("pageerror", lambda e: page_errors.append(str(e)))
        _pg.goto((EVIL_OUT / "report.html").resolve().as_uri())
        _pg.wait_for_timeout(700)
        seen = _pg.evaluate("""() => ({
            pwned: window.PWNED ?? null,
            scripts: document.querySelectorAll('script').length,
            strayImgs: [...document.querySelectorAll('img')]
                         .filter(i => i.getAttribute('src') === 'x').length,
            tiles: document.querySelectorAll('.fold').length,
            // A label must be TEXT. Any element inside one means a name was
            // parsed as markup rather than written as characters.
            labelKids: [...document.querySelectorAll('.fold .foldname')]
                         .reduce((n, e) => n + e.children.length, 0)})""")
        check("nothing in a folder name executed", seen["pwned"] is None,
              str(seen["pwned"]))
        check("the page has exactly its own one script", seen["scripts"] == 1,
              str(seen["scripts"]))
        check("no element was smuggled in", seen["strayImgs"] == 0)
        check("the index still built every tile", seen["tiles"] == len(made),
              f"{seen['tiles']} of {len(made)}")
        check("and every label is text, not markup", seen["labelKids"] == 0)
        _pg.click(".fold"); _pg.wait_for_timeout(400)
        check("a folder still opens", _pg.eval_on_selector_all(
            ".card", "e => e.length") > 0)
        check("no dialog was raised", not dialogs, str(dialogs[:2]))
        check("and no page errors", not page_errors, "; ".join(page_errors[:2]))
        _br.close()

# The tile colour is written into the stylesheet, so it is the other new way in.
print("\n=== the folder outline cannot carry CSS ===")
for bad in ("red;}body{display:none}.x{", "#b09468;}*{display:none", "</style>",
            "expression(alert(1))", ""):
    check(f"refused: {bad!r}", not ps.outline_ok(bad))
for good in ("#b09468", "#b96", "b09468", "  #B09468  "):
    check(f"accepted: {good!r}", ps.outline_ok(good))
# Whatever comes out is rebuilt from parsed numbers, never from the input text.
for value in ("#b09468", "#000", "#2e6f4f"):
    pal = ps.folder_palette(value)
    check(f"{value}: both colours are plain hex",
          all(re.fullmatch(r"#[0-9a-f]{6}", v) for v in pal.values()), str(pal))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ps.main(["--root", str(LIB), "--out", str(OUT), "--report-only",
                  "--folder-outline", "red;}body{display:none}.x{"])
check("and the script stops rather than writing it", rc == 2, f"rc={rc}")
check("with a sentence, not a traceback",
      "is not a colour" in buf.getvalue() and "Traceback" not in buf.getvalue(),
      buf.getvalue()[:160])


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
