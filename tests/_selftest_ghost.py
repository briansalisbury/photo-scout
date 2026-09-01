"""
Verifies photo_scout_ghost.py against a mock Ghost Admin API.

The mock behaves like the real thing in the ways that matter: it mints
date-based URLs the client cannot predict, and it deduplicates repeated
filenames by appending -1, -2 exactly as Ghost does. That second behaviour is
the trap the whole naming scheme exists to avoid, so the test asserts it never
fires.
"""
import base64, contextlib, hashlib, hmac, io, json, os, re, shutil, sqlite3, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps
import photo_scout_ghost as pg

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------------------
# A mock Ghost
# ---------------------------------------------------------------------------
STATE = {"uploads": [], "pages": {}, "auth": [], "seen_filenames": {}, "ua": []}
KEY_ID = "6413a2b1c9d0e5f708192a3b"
KEY_SECRET = "0123456789abcdef" * 4
ADMIN_KEY = f"{KEY_ID}:{KEY_SECRET}"


class MockGhost(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        hdr = self.headers.get("Authorization", "")
        STATE["auth"].append(hdr)
        STATE["ua"].append(self.headers.get("User-Agent", ""))
        if not hdr.startswith("Ghost "):
            self._json(401, {"error": "no token"}); return False
        token = hdr[6:]
        try:
            h_b64, p_b64, sig_b64 = token.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)
            header = json.loads(base64.urlsafe_b64decode(pad(h_b64)))
            payload = json.loads(base64.urlsafe_b64decode(pad(p_b64)))
            expect = hmac.new(bytes.fromhex(KEY_SECRET),
                              f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(base64.urlsafe_b64decode(pad(sig_b64)), expect):
                self._json(401, {"error": "bad signature"}); return False
            if header.get("kid") != KEY_ID or payload.get("aud") != "/admin/":
                self._json(401, {"error": "bad claims"}); return False
        except Exception as exc:
            self._json(401, {"error": f"malformed: {exc}"}); return False
        return True

    def do_POST(self):
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if self.path.startswith("/ghost/api/admin/images/upload"):
            m = re.search(rb'filename="([^"]+)"', raw)
            name = m.group(1).decode() if m else "unknown.jpg"
            # Ghost's real deduplication behaviour.
            n = STATE["seen_filenames"].get(name, 0)
            STATE["seen_filenames"][name] = n + 1
            stored = name if n == 0 else re.sub(r"(\.\w+)$", f"-{n}\\1", name)
            url = f"https://mock.ghost/content/images/2026/08/{stored}"
            STATE["uploads"].append({"sent": name, "stored": stored, "bytes": len(raw)})
            return self._json(201, {"images": [{"url": url, "ref": "x"}]})

        if self.path.startswith("/ghost/api/admin/pages"):
            # Real Ghost 6 rejects ?source=lexical: 'source' is only for
            # converting FROM html, and lexical is the native format.
            if "source=lexical" in self.path:
                return self._json(422, {"errors": [{
                    "message": "Validation error, cannot save page.",
                    "context": "Validation (AllowedValues) failed for source",
                    "type": "ValidationError"}]})
            body = json.loads(raw)["pages"][0]
            slug = body["slug"]
            page = {"id": "pg_" + slug, "slug": slug, "title": body["title"],
                    "lexical": body["lexical"], "status": body["status"],
                    "updated_at": "2026-08-17T00:00:00.000Z"}
            STATE["pages"][slug] = page
            return self._json(201, {"pages": [page]})
        self._json(404, {"error": "no route"})

    def do_PUT(self):
        if not self._check_auth():
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)["pages"][0]
        slug = body["slug"]
        if slug not in STATE["pages"]:
            return self._json(404, {"error": "missing"})
        page = STATE["pages"][slug]
        if body.get("updated_at") != page["updated_at"]:
            return self._json(409, {"error": "stale updated_at"})
        page.update({"title": body["title"], "lexical": body["lexical"],
                     "status": body["status"], "updated_at": "2026-08-17T00:00:01.000Z"})
        self._json(200, {"pages": [page]})

    def do_GET(self):
        if not self._check_auth():
            return
        m = re.match(r"/ghost/api/admin/pages/slug/([^/?]+)", self.path)
        if m:
            slug = m.group(1)
            if slug in STATE["pages"]:
                return self._json(200, {"pages": [STATE["pages"][slug]]})
            return self._json(404, {"errors": [{"type": "NotFoundError"}]})
        self._json(404, {"error": "no route"})


srv = HTTPServer(("127.0.0.1", 0), MockGhost)
threading.Thread(target=srv.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# Fixture: a scored library whose filenames deliberately collide across folders
# ---------------------------------------------------------------------------
LIB = Path("/tmp/gh_lib"); OUT = Path("/tmp/gh_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
LIB.mkdir(parents=True)
rng = np.random.default_rng(9)
# One folder carries its date in the name (which must be stripped from the
# display) and one is a bare year (which must be left alone).
FOLDER_DATES = {"2011-06-28 - Wyoming": "2011:06:28",
                "2010 Arches":          "2010:03:12"}
# Different clock time per frame, running opposite to the file names, so a sort
# that stopped at the day could not accidentally come out right.
TIMES = [f"{6 + (19 - i) // 2:02d}:{((19 - i) % 2) * 30:02d}:00" for i in range(20)]
FOLDER_SHOWN = {"2011-06-28 - Wyoming": "Wyoming", "2010 Arches": "2010 Arches"}
for folder, shot in FOLDER_DATES.items():
    d = LIB / folder; d.mkdir(parents=True)
    for i in range(20):
        im = Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8)) \
                  .resize((1000, 700), Image.BICUBIC)
        exif = im.getexif()
        # One file per folder deliberately has no timestamp, so the page has to
        # cope with undated photographs.
        if i:
            exif.get_ifd(0x8769)[36867] = f"{shot} {TIMES[i]}"   # DateTimeOriginal
        im.save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90,   # same names in both!
                exif=exif)

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": float(np.clip(5.0 + np.sin(self.n * 1.7) * 0.55, 3.9, 6.1)),
                "nima_raw": float(np.clip(4.9 + np.cos(self.n * 1.2) * 0.5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT
pg.ps.DEFAULT_OUT_DIR = OUT
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB)])

db = sqlite3.connect(OUT / "scores.sqlite3"); db.row_factory = sqlite3.Row
short = db.execute("SELECT * FROM photos WHERE error IS NULL AND dup_of IS NULL "
                   "AND verdict IN ('TOP PICK','STRONG')").fetchall()
print(f"fixture: {len(short)} shortlisted photographs\n")

# Tags, keyed by the local path exactly as the browser would export them.
tags = {short[0]["path"]: ["Lake Photos", "Sunset"], short[1]["path"]: ["Desert"]}
(OUT / "tags.json").write_text(json.dumps(tags), encoding="utf-8")


MANIFEST = Path("/tmp/gh_manifest.sqlite3")
MANIFEST.unlink(missing_ok=True)

def run(*a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pg.main(["--site", SITE, "--out", str(OUT),
                      "--manifest", str(MANIFEST)] + list(a))
    return rc, buf.getvalue()


print("=== identity and naming ===")
pid = pg.photo_id_for("2011 Wyoming/DSC_0000.JPG")
check("photo_id is 16 hex chars", re.fullmatch(r"[0-9a-f]{16}", pid) is not None, pid)
check("separators do not matter",
      pg.photo_id_for(r"2011 Wyoming\DSC_0000.JPG") == pid)
check("case does not matter",
      pg.photo_id_for("2011 WYOMING/dsc_0000.jpg") == pid)
check("different folders give different ids",
      pg.photo_id_for("2010 Arches/DSC_0000.JPG") != pid)
check("leading slash does not matter",
      pg.photo_id_for("/2011 Wyoming/DSC_0000.JPG") == pid)
check("upload filename is url-safe",
      re.fullmatch(r"psc-[0-9a-f]{16}-[tp]\.jpg", pg.upload_filename(pid, "thumb")) is not None,
      pg.upload_filename(pid, "thumb"))
check("thumb and preview names differ",
      pg.upload_filename(pid, "thumb") != pg.upload_filename(pid, "preview"))
check("absolute url reduced to site-relative",
      pg.to_site_relative("https://brians.life/content/images/2026/08/x.jpg")
      == "/content/images/2026/08/x.jpg")

print("\n=== JWT ===")
tok = pg.ghost_jwt(ADMIN_KEY)
h, p_, sg = tok.split(".")
pad = lambda s: s + "=" * (-len(s) % 4)
hdr = json.loads(base64.urlsafe_b64decode(pad(h)))
pay = json.loads(base64.urlsafe_b64decode(pad(p_)))
check("header alg/typ/kid", hdr == {"alg": "HS256", "typ": "JWT", "kid": KEY_ID}, str(hdr))
check("aud is /admin/", pay["aud"] == "/admin/")
check("exp is within 5 minutes", 0 < pay["exp"] - pay["iat"] <= 300,
      f"{pay['exp'] - pay['iat']}s")
check("signature verifies with the hex-decoded secret",
      hmac.compare_digest(
          base64.urlsafe_b64decode(pad(sg)),
          hmac.new(bytes.fromhex(KEY_SECRET), f"{h}.{p_}".encode(), hashlib.sha256).digest()))
try:
    pg.ghost_jwt("no-colon-here"); check("malformed key rejected", False)
except ValueError:
    check("malformed key rejected", True)

print("\n=== dry run touches nothing ===")
rc, log = run("--dry-run")
check("dry run succeeds", rc == 0, f"rc={rc}")
check("nothing uploaded", len(STATE["uploads"]) == 0)
check("no page created", not STATE["pages"])
check("preview html written", (OUT / "ghost_preview.html").exists())

# The point of a dry run is seeing the gallery. Every image it references must
# actually exist on disk relative to the preview file.
_pv = (OUT / "ghost_preview.html").read_text(encoding="utf-8")
_data = json.loads(re.search(r'class="psc-data">(.*?)</script>', _pv, re.S).group(1)
                   .replace("<\\/", "</"))
_refs = [e["th"] for e in _data] + [e["pv"] for e in _data]
_broken = [r for r in _refs if not r or not (OUT / r).exists()]
check("every image in the dry-run preview resolves on disk",
      not _broken, f"{len(_broken)} broken of {len(_refs)}: {_broken[:3]}")
check("dry-run preview points at local files, not invented URLs",
      all(r.startswith(("thumbs/", "previews/")) for r in _refs),
      str(sorted({r.split('/')[0] for r in _refs})))
check("no DRY-RUN placeholder path anywhere", "DRY-RUN" not in _pv)
check("dry run reports what it would upload", "would be uploaded" in log)
check("manifest still empty", pg.Manifest(MANIFEST).count() == 0)

print("\n=== first real publish ===")
rc, log = run("--key", ADMIN_KEY, "--slug", "photos", "--title", "Photographs")
check("publish succeeds", rc == 0, f"rc={rc}\n{log[-400:]}")
check("every image uploaded", len(STATE["uploads"]) == len(short) * 2,
      f"{len(STATE['uploads'])} uploads for {len(short)} photos")
check("all authenticated as Ghost tokens",
      all(a.startswith("Ghost ") for a in STATE["auth"]))

sent = [u["sent"] for u in STATE["uploads"]]
stored = [u["stored"] for u in STATE["uploads"]]
check("no filename sent twice", len(set(sent)) == len(sent),
      f"{len(sent) - len(set(sent))} duplicates")
check("Ghost never had to deduplicate", sent == stored,
      str([(a, b) for a, b in zip(sent, stored) if a != b][:2]))
check("every stored name matches the scheme",
      all(re.fullmatch(r"psc-[0-9a-f]{16}-[tp]\.jpg", n) for n in stored))
check("no original filename leaked into Ghost",
      not any("DSC_" in n for n in stored))

print("\n=== page creation uses the native lexical format ===")
# Look at the request URLs actually used, not at prose mentioning the mistake.
import inspect
_create = inspect.getsource(pg.GhostClient.create_page)
_paths = re.findall(r'_request\(\s*"(\w+)",\s*"([^"]+)"', _create)
check("create_page posts to a bare /pages/", ("POST", "/pages/") in _paths, str(_paths))
check("no source= query parameter on the request",
      not any("source=" in path for _, path in _paths), str(_paths))
# The mock now 422s on source=lexical, so a regression fails the publish above.
check("the mock would have caught it", "source=lexical" in Path(__file__).read_text())

print("\n=== the published page ===")
page = STATE["pages"]["photos"]
lex = json.loads(page["lexical"])
node = lex["root"]["children"][0]
check("lexical root with one html card", node["type"] == "html", str(node.get("type")))
gal = node["html"]
check("page is a draft by default", page["status"] == "draft", page["status"])
check("no file:// links survive", "file://" not in gal)
check("no windows paths survive", "D:\\" not in gal and "D:/" not in gal)
check("no local thumbs/ references", 'src="thumbs/' not in gal and "'thumbs/" not in gal)
check("images are site-relative", "/content/images/2026/08/psc-" in gal)
check("no absolute mock host in the markup", "http://127.0.0.1" not in gal
      and "mock.ghost" not in gal)

data = json.loads(re.search(r'class="psc-data">(.*?)</script>', gal, re.S).group(1)
                  .replace("<\\/", "</"))
check("one payload entry per photograph", len(data) == len(short), f"{len(data)}")
check("every entry has a thumbnail url", all(e["th"] for e in data))
check("every entry has a photo_id", all(re.fullmatch(r"[0-9a-f]{16}", e["id"]) for e in data))
check("only shortlist verdicts present",
      {e["v"] for e in data} <= {"TOP PICK", "STRONG"}, str({e["v"] for e in data}))
tagged = [e for e in data if e.get("t")]
check("tags carried through, re-keyed by photo_id", len(tagged) == 2,
      str([(e["n"], e["t"]) for e in tagged]))
check("heart buttons wired to photo ids", "data-photo-id" in gal or "photoId" in gal)

print("\n=== capture dates reach the payload ===")
check("every entry carries a date field", all("d" in e for e in data))
iso = {e["d"] for e in data if e["d"]}
check("dates are the ISO form of the EXIF written",
      {v[:10] for v in iso} <= {"2011-06-28", "2010-03-12"} and iso,
      str(sorted(iso)[:2]))
check("the clock time travels with the date",
      all(len(v) == 19 for v in iso), str(sorted(iso)[:2]))
check("undated photographs are empty, not guessed",
      any(e["d"] == "" for e in data),
      f"{sum(1 for e in data if not e['d'])} undated of {len(data)}")
check("the long form is NOT duplicated into the payload",
      not any("June 28, 2011" in json.dumps(e) for e in data))
check("folder still carried", all(e["f"] for e in data))
check("sort control present in the markup", 'class="psc-sort"' in gal)
for opt in ("score-desc", "date-asc", "name-asc", "folder-desc"):
    check(f"sort option {opt}", f'value="{opt}"' in gal)

print("\n=== an emitted page after a real publish is still viewable ===")
rc, log2 = run("--key", ADMIN_KEY, "--slug", "photos", "--emit-html", "look.html")
_lp = (OUT / "look.html").read_text(encoding="utf-8")
_ld = json.loads(re.search(r'class="psc-data">(.*?)</script>', _lp, re.S).group(1)
                 .replace("<\\/", "</"))
check("emitted page uses local paths", all(e["th"].startswith("thumbs/") for e in _ld))
check("emitted page images resolve", all((OUT / e["th"]).exists() for e in _ld))
check("the PUBLISHED markup still uses Ghost urls, not local paths",
      "/content/images/2026/08/psc-" in json.loads(
          STATE["pages"]["photos"]["lexical"])["root"]["children"][0]["html"])

print("\n=== republish is idempotent ===")
before = len(STATE["uploads"])
rc, log = run("--key", ADMIN_KEY, "--slug", "photos", "--title", "Photographs")
check("second publish succeeds", rc == 0, f"rc={rc}")
check("nothing re-uploaded", len(STATE["uploads"]) == before,
      f"{len(STATE['uploads']) - before} extra uploads")
check("page updated, not duplicated", len(STATE["pages"]) == 1)
check("log says images were reused", "already in Ghost" in log)

print("\n=== a regenerated preview is re-uploaded, others are not ===")
victim = short[0]
prev = OUT / "previews" / ps.thumb_name(victim["path"])
img = Image.open(prev); img.point(lambda x: min(255, int(x * 1.2))).save(prev, "JPEG", quality=88)
before = len(STATE["uploads"])
rc, log = run("--key", ADMIN_KEY, "--slug", "photos")
check("exactly one re-upload", len(STATE["uploads"]) - before == 1,
      f"{len(STATE['uploads']) - before}")
check("and it kept the same filename",
      STATE["uploads"][-1]["sent"] == pg.upload_filename(
          pg.photo_id_for(victim["rel_path"]), "preview"))

print("\n=== the manifest lives outside the resettable directory ===")
check("default manifest is beside the script, not in the output dir",
      pg.HERE / pg.PUBLISH_DB != OUT / pg.PUBLISH_DB)
check("warns if pointed inside the output directory",
      "output directory" in run("--dry-run", "--manifest", str(OUT / "x.sqlite3"))[1])

print("\n=== survives a photo_scout --reset ===")
manifest_before = pg.Manifest(MANIFEST).count()
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB), "--reset", "--yes"])
check("scores database was rebuilt", (OUT / "scores.sqlite3").exists())
check("upload manifest survived", MANIFEST.exists())
check("manifest rows intact", pg.Manifest(MANIFEST).count() == manifest_before,
      f"{pg.Manifest(MANIFEST).count()} vs {manifest_before}")
before = len(STATE["uploads"])
rc, log = run("--key", ADMIN_KEY, "--slug", "photos")
extra = STATE["uploads"][before:]
# The rescore regenerated every thumbnail and preview from the RAWs. Only the
# one preview deliberately brightened earlier differs from what is in the
# manifest, so exactly that file should re-upload and nothing else - proving the
# manifest keys on CONTENT, not on "did the file get rewritten".
victim_preview = pg.upload_filename(pg.photo_id_for(victim["rel_path"]), "preview")
check("only genuinely-changed content re-uploads after a full rescore",
      [u["sent"] for u in extra] == [victim_preview],
      f"{len(extra)} re-uploaded: {[u['sent'] for u in extra][:3]}")
check("the other images were all reused from the manifest",
      len(extra) < 2, f"{len(extra)} of 18")

print("\n=== error handling ===")
rc, log = run("--key", "garbage-no-colon")
check("bad key fails cleanly with a message, not a traceback",
      rc == 2 and "must look like" in log and "Traceback" not in log, f"rc={rc}")
rc, log = run("--out", "/tmp/does-not-exist", "--key", ADMIN_KEY)
check("missing database explained", rc == 2 and "no scores database" in log)

print("\n--- a wrong key says WHICH wrong key it is")
# The two mistakes people actually make. Both look plausible in Ghost's
# integration panel, and neither is diagnosable from the key alone.
rc, log = run("--key", "a" * 26)
check("the Content API key is named as such",
      rc == 2 and "CONTENT API key" in log and "read-only" in log,
      [l for l in log.splitlines() if "CONTENT" in l][:1])
rc, log = run("--key", "b" * 64)
check("the secret half alone is diagnosed",
      rc == 2 and "secret half on its own" in log,
      [l for l in log.splitlines() if "secret half" in l][:1])
check("and the length and colon count are reported back",
      "64 characters, 0 colon(s)" in log,
      [l for l in log.splitlines() if "Got:" in l][:1])
rc, log = run("--key", "c" * 24 + ":" + "zz" * 32)
check("a right-shaped key with a non-hex secret is caught before any upload",
      rc == 2 and "not hex" in log, [l for l in log.splitlines()[:1]])
check("no key is echoed back in full anywhere",
      not any(k in log for k in ("c" * 24, "zz" * 32)))

print("\n--- no key at all points at Ghost, and at both shells")
saved = os.environ.pop("GHOST_ADMIN_KEY", None)
try:
    rc, log = run()
finally:
    if saved is not None:
        os.environ["GHOST_ADMIN_KEY"] = saved
check("it names the exact place in Ghost admin",
      rc == 2 and "Settings -> Advanced -> Integrations" in log,
      [l for l in log.splitlines() if "Integrations" in l][:1])
check("it says ADMIN, not just 'API key'", "ADMIN API key" in log)
check("it gives the bash form", "export GHOST_ADMIN_KEY=" in log)
# PowerShell is the trap: 'export' there fails silently, so the variable is
# never set and the very next run prints this same error.
check("and the PowerShell form, which is not the same",
      "$env:GHOST_ADMIN_KEY" in log,
      [l for l in log.splitlines() if "env:" in l][:1])
check("it warns the variable is per terminal session",
      "terminal session" in log)
check("and offers --dry-run as the no-credentials path", "--dry-run" in log)

print("\n--- the flag's own help is self-sufficient")
helptext = io.StringIO()
with contextlib.redirect_stdout(helptext):
    try:
        pg.main(["--help"])
    except SystemExit:
        pass
ht = helptext.getvalue()
check("--help explains where the key comes from",
      "Integrations" in ht and "Content" in ht, "")
check("--help warns it is not needed for a dry run", "--dry-run" in ht)

print("\n=== the request identifies itself (Cloudflare 1010) ===")
check("a User-Agent is always sent", all(u for u in STATE["ua"]),
      f"{sum(1 for u in STATE['ua'] if not u)} requests with none")
check("it is not the default urllib signature",
      not any("Python-urllib" in u for u in STATE["ua"]),
      str(sorted(set(STATE["ua"]))[:1]))
check("it names the tool", all("photo-scout" in u for u in STATE["ua"]))

# an opaque edge block must be explained, not just echoed
hint = pg._edge_block_hint(403, "error code: 1010", "https://admin.brians.life")
check("1010 is identified as Cloudflare", "Cloudflare" in hint and "never reached" in hint)
check("1010 hint gives the exact WAF rule", "/ghost/api/" in hint and "Skip" in hint)
check("413 explains the size limit", "client_max_body_size" in pg._edge_block_hint(413, "", ""))
check("401 distinguishes admin from content key",
      "ADMIN API key" in pg._edge_block_hint(401, "", "x"))
check("an ordinary Ghost error gets no spurious hint",
      pg._edge_block_hint(422, '{"errors":[{"message":"Validation error"}]}', "x") == "")

print("\n=== a separate admin host ===")
# Ghost can serve its admin API on admin.brians.life while every image URL it
# returns lives on brians.life. API calls must go to the admin host; the markup
# must still work on the public site.
STATE["uploads"].clear(); STATE["pages"].clear(); STATE["seen_filenames"].clear()
MANIFEST.unlink(missing_ok=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pg.main(["--site", "https://brians.life", "--admin-url", SITE,
                  "--out", str(OUT), "--manifest", str(MANIFEST),
                  "--key", ADMIN_KEY, "--slug", "split"])
log = buf.getvalue()
check("publish succeeds with a split admin host", rc == 0, f"rc={rc}\n{log[-300:]}")
check("uploads still happened", len(STATE["uploads"]) > 0, f"{len(STATE['uploads'])}")
check("page created on the admin host", "split" in STATE["pages"])
check("both hosts reported", "Admin:" in log and "brians.life" in log)
check("final link points at the PUBLIC site, not the admin host",
      "https://brians.life/split/" in log, [l for l in log.splitlines() if "View" in l])

gal2 = json.loads(STATE["pages"]["split"]["lexical"])["root"]["children"][0]["html"]
check("image paths are site-relative, so they resolve on the public site",
      "/content/images/2026/08/psc-" in gal2)
check("no admin hostname leaked into the published markup",
      "admin." not in gal2 and "127.0.0.1" not in gal2)
check("no absolute image origins at all", 'src="http' not in gal2 and '"th":"http' not in gal2)

print("\n=== tags saved from the published page can be baked back in ===")
# The page downloads psc-web-tags.json keyed by photo_id, which is exactly the
# key the publisher already uses - no translation, only validation.
ids = [pg.photo_id_for(r["rel_path"] or r["filename"]) for r in short[:2]]
WEB = OUT.parent / "psc-web-tags.json"
WEB.write_text(json.dumps({
    ids[0]: ["Gallery Wall", "Sold"],
    ids[1]: ["<script>alert(1)</script>", "Print Me"],
    "deadbeefdeadbeef": ["Orphan"],     # a photograph no longer in the report
    "notanid": ["Junk"],
}), encoding="utf-8")
rc, log = run("--dry-run", "--web-tags", str(WEB), "--emit-html", "web.html")
check("publish accepts the file", rc == 0, f"rc={rc}\n{log[-300:]}")
_wd = json.loads(re.search(r'class="psc-data">(.*?)</script>',
                           (OUT / "web.html").read_text(encoding="utf-8"), re.S)
                 .group(1).replace("<\\/", "</"))
by_id = {e["id"]: e.get("t", []) for e in _wd}
check("web tags reach the payload", by_id.get(ids[0]) == ["Gallery Wall", "Sold"],
      str(by_id.get(ids[0])))
check("markup is stripped from a hand-edited file",
      by_id.get(ids[1]) == ["Print Me", "scriptalert1script"] or
      all("<" not in t for t in by_id.get(ids[1], [])), str(by_id.get(ids[1])))
check("no raw script tag reaches the page",
      "<script>alert(1)</script>" not in (OUT / "web.html").read_text(encoding="utf-8"))
check("an id that is no longer in the report is reported, not crashed on",
      "no longer in the report" in log, [l for l in log.splitlines() if "Web tags" in l])
rc, log = run("--dry-run", "--web-tags", str(OUT / "nope.json"))
check("a missing file fails cleanly", rc == 2 and "no such file" in log, f"rc={rc}")

print("\n=== the gallery in a real browser ===")
rc, _ = run("--dry-run", "--emit-html", "browse.html")
page_path = OUT / "browse.html"
check("preview page emitted", rc == 0 and page_path.exists())

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    br = pw.chromium.launch(); P = br.new_page()
    js_errors = []
    P.on("pageerror", lambda e: js_errors.append(str(e)))
    P.set_viewport_size({"width": 1500, "height": 950})
    P.goto(page_path.resolve().as_uri()); P.wait_for_timeout(400)

    def order(attr):
        return P.eval_on_selector_all(
            ".psc-card:not(.psc-hidden)", f"e => e.map(x => x.dataset.{attr})")
    def shown():
        return P.eval_on_selector_all(".psc-card:not(.psc-hidden)", "e => e.length")

    total = shown()
    check("cards rendered", total == len(short), f"{total} of {len(short)}")

    print("\n--- folder and date on the card")
    texts = P.eval_on_selector_all(".psc-meta", "e => e.map(x => x.textContent)")
    check("a meta line is rendered on every card", len(texts) == total,
          f"{len(texts)} of {total}")
    check("it shows the folder", any("Wyoming" in t for t in texts))
    check("a date in the folder name is stripped from the display",
          not any("2011-06-28 -" in t for t in texts), str(texts[:3]))
    check("a bare year in a folder name is left alone",
          any("2010 Arches" in t for t in texts), str(texts[:3]))
    check("it shows the long-form date",
          any("June 28, 2011" in t for t in texts) and
          any("March 12, 2010" in t for t in texts), str(texts[:2]))
    check("and the time of day beside it, 24 hour",
          # The resolution follows the time on the same line, so the clock is
          # no longer necessarily the end of the string.
          all(re.search(r"\d{2}:\d{2}(?:$| ·)", t) for t in texts if "," in t),
          str([t for t in texts if "," in t][:3]))
    check("folder and date are separated, not run together",
          all(("·" in t) for t in texts if "," in t and " - " not in t))
    check("undated photographs show no date, and no stray separator",
          any("," not in t and t.split(" · ")[0] in FOLDER_SHOWN.values()
              for t in texts), str(texts[:3]))
    check("no empty segment anywhere on a meta line",
          not any("· ·" in t or t.strip().startswith("·") or t.strip().endswith("·")
                  for t in texts), str([t for t in texts if "· ·" in t][:2]))

    print("\n--- resolution on the card, because the score ignores it")
    check("every card states its pixel dimensions",
          all(re.search(r"\d+ × \d+ · \d+\.\d MP$", t) for t in texts),
          str([t for t in texts if " MP" not in t][:3]))
    check("the figures match the fixture",
          all(re.search(r"\b1000 × 700 · 0\.7 MP$", t) for t in texts),
          str(texts[:2]))
    check("and they reach the payload as one pre-rendered string",
          all(re.match(r"^\d+ × \d+ · \d+\.\d MP$", e["r"]) for e in data),
          str([e.get("r") for e in data][:2]))
    check("resolution is searchable",
          P.eval_on_selector(".psc-card", "e => e.dataset.search").count("mp") == 1,
          P.eval_on_selector(".psc-card", "e => e.dataset.search"))

    print("\n--- sorting")
    P.select_option(".psc-sort", "name-asc"); P.wait_for_timeout(250)
    names = order("name")
    check("by file name ascending", names == sorted(names), str(names[:2]))
    P.select_option(".psc-sort", "name-desc"); P.wait_for_timeout(250)
    check("and descending", order("name") == sorted(names, reverse=True))

    P.select_option(".psc-sort", "date-asc"); P.wait_for_timeout(250)
    ds = [d for d in order("date") if d]
    check("by date, oldest first", ds == sorted(ds), str(ds[:1] + ds[-1:]))
    sameday = [d for d in ds if d.startswith("2011-06-28")]
    check("same-day photographs order by the clock",
          len(sameday) > 1 and sameday == sorted(sameday),
          str([d[11:16] for d in sameday]))
    check("undated sink to the bottom", order("date")[-1] == "",
          f"last is {order('date')[-1]!r}")
    P.select_option(".psc-sort", "date-desc"); P.wait_for_timeout(250)
    ds = [d for d in order("date") if d]
    check("by date, newest first", ds == sorted(ds, reverse=True))
    sameday = [d for d in ds if d.startswith("2011-06-28")]
    check("and the clock reverses with it", sameday == sorted(sameday, reverse=True),
          str([d[11:16] for d in sameday]))
    check("undated still at the bottom", order("date")[-1] == "")

    P.select_option(".psc-sort", "folder-asc"); P.wait_for_timeout(250)
    fs = order("folder")
    check("by folder", fs == sorted(fs), str(fs[:1] + fs[-1:]))

    P.select_option(".psc-sort", "score-desc"); P.wait_for_timeout(250)
    sc = [float(x) for x in order("score")]
    check("by rating, best first", sc == sorted(sc, reverse=True), str(sc[:3]))
    P.select_option(".psc-sort", "score-asc"); P.wait_for_timeout(250)
    sc = [float(x) for x in order("score")]
    check("and worst first", sc == sorted(sc))

    print("\n--- searching the new fields")
    P.select_option(".psc-sort", "score-desc"); P.wait_for_timeout(200)
    for term, label in (("June 28, 2011", "long-form date"),
                        ("2010-03-12", "ISO date"),
                        ("Wyoming", "folder"),
                        ("2011-06-28 - Wyoming", "the folder's name on disk"),
                        (P.eval_on_selector(".psc-card", "e => e.dataset.name"),
                         "file name")):
        P.fill(".psc-q", term); P.wait_for_timeout(250)
        n = shown()
        check(f"search by {label}", 0 < n < total, f"{n} of {total}")
        P.fill(".psc-q", ""); P.wait_for_timeout(150)

    P.fill(".psc-q", "top pick"); P.wait_for_timeout(250)
    verds = order("verdict")
    check("search by rating returns only that band",
          verds and set(verds) == {"TOP PICK"}, f"{len(verds)} results, {set(verds)}")
    P.fill(".psc-q", ""); P.wait_for_timeout(150)

    print("\n--- the written feedback is searchable, case-insensitively")
    note = P.eval_on_selector(".psc-note", "e => e.textContent")
    phrase = " ".join(note.split()[:3]).strip(" ,;.")
    check("a card carries written feedback", len(phrase) > 4, repr(note[:60]))
    for term in (phrase, "TOP PICK", "wyoming", "June 28, 2011"):
        seen = []
        for variant in (term, term.lower(), term.upper()):
            P.fill(".psc-q", variant); P.wait_for_timeout(220)
            seen.append(shown())
        check(f"{term!r} matches regardless of case",
              len(set(seen)) == 1 and seen[0] > 0, str(seen))
    P.fill(".psc-q", ""); P.wait_for_timeout(150)

    print("\n--- sort composes with the band buttons")
    P.click('.psc-bar button[data-band="STRONG"]'); P.wait_for_timeout(200)
    P.select_option(".psc-sort", "date-asc"); P.wait_for_timeout(250)
    check("band filter survives a sort", set(order("verdict")) == {"STRONG"},
          str(set(order("verdict"))))
    ds = [d for d in order("date") if d]
    check("sort survives a filter", ds == sorted(ds))
    P.click('.psc-bar button[data-band="all"]'); P.wait_for_timeout(200)

    print("\n--- browser-side tagging")
    # The gallery script is an IIFE, so nothing is exposed on window - by
    # design, it is a public page. Everything below is asserted through the DOM
    # and localStorage, exactly what a visitor's browser actually holds.
    def all_tags():
        return sorted(set(P.eval_on_selector_all(
            ".psc-card .psc-tag", "e => e.map(x => x.firstChild.textContent)")))
    def tag_count(name):
        return P.evaluate(
            "n => [...document.querySelectorAll('.psc-card')].filter("
            "c => (c.dataset.tags||'').indexOf('|'+n.toLowerCase()+'|') >= 0).length",
            name)
    def stored():
        return P.evaluate("() => {const k = Object.keys(localStorage)"
                          ".find(x => x.indexOf('psc-tags') === 0);"
                          " return k ? localStorage.getItem(k) : '{}';}")

    P.fill(".psc-q", ""); P.wait_for_timeout(150)
    check("every card has a tag box",
          P.eval_on_selector_all(".psc-taginput", "e => e.length") == total,
          str(P.eval_on_selector_all(".psc-taginput", "e => e.length")))
    check("tags published with the page render as editable chips",
          len(all_tags()) > 0 and P.eval_on_selector_all(
              ".psc-card .psc-tag button", "e => e.length") > 0, str(all_tags()))

    card = P.locator(".psc-card").first
    card.locator(".psc-taginput").click()
    card.locator(".psc-taginput").type("Gallery Wall,")
    P.wait_for_timeout(300)
    check("typing a multi-word tag creates it", "Gallery Wall" in all_tags(),
          str(all_tags()))
    card.locator(".psc-taginput").type("<img src=x onerror=alert(1)>,")
    P.wait_for_timeout(300)
    check("dangerous characters are stripped, not stored",
          not any(ch in "".join(all_tags()) for ch in "<>=\"'"), str(all_tags()))
    check("nothing was injected into the page",
          P.eval_on_selector_all(".psc-card img[src='x']", "e => e.length") == 0)

    P.click(".psc-q"); P.fill(".psc-q", "Gallery"); P.wait_for_timeout(300)
    check("the tag turns up in the dropdown",
          P.eval_on_selector_all(".psc-tagmenu > div", "e => e.length") >= 1,
          str(P.eval_on_selector_all(".psc-tagmenu > div", "e => e.map(x=>x.textContent)")))
    P.keyboard.press("Enter"); P.wait_for_timeout(300)
    check("selecting it puts a chip in the search box",
          P.eval_on_selector_all(".psc-chips .psc-tag",
                                 "e => e.map(x => x.firstChild.textContent)") ==
          ["Gallery Wall"])
    check("and filters the grid to that photograph", shown() == 1, f"{shown()} shown")

    print("\n--- two chips are ORed, not ANDed")
    P.evaluate("() => document.querySelectorAll('.psc-chips .psc-tag button')"
               ".forEach(b => b.click())")
    P.wait_for_timeout(250)
    second = P.locator(".psc-card").nth(1)
    second.locator(".psc-taginput").click()
    second.locator(".psc-taginput").type("Darkroom,")
    P.wait_for_timeout(300)
    for term in ("Gallery", "Darkroom"):
        P.click(".psc-q"); P.fill(".psc-q", term); P.wait_for_timeout(250)
        P.keyboard.press("Enter"); P.wait_for_timeout(250)
    picked = sorted(P.eval_on_selector_all(
        ".psc-chips .psc-tag", "e => e.map(x => x.firstChild.textContent)"))
    check("both chips held", picked == ["Darkroom", "Gallery Wall"], str(picked))
    check("no photograph carries both",
          P.eval_on_selector_all(".psc-card",
              "e => e.filter(c => (c.dataset.tags||'').indexOf('|gallery wall|') >= 0 && "
              "(c.dataset.tags||'').indexOf('|darkroom|') >= 0).length") == 0)
    check("union returns BOTH photographs, not zero", shown() == 2, f"{shown()} shown")

    print("\n--- retiring a tag asks first")
    P.evaluate("() => document.querySelectorAll('.psc-chips .psc-tag button')"
               ".forEach(b => b.click())")
    P.wait_for_timeout(200)
    P.click(".psc-q"); P.fill(".psc-q", "Darkroom"); P.wait_for_timeout(300)
    seen = {}
    P.once("dialog", lambda d: (seen.__setitem__("msg", d.message), d.dismiss()))
    P.locator(".psc-tagmenu .psc-delall").first.click(); P.wait_for_timeout(300)
    check("it warns before deleting", "Darkroom" in seen.get("msg", ""), seen.get("msg"))
    check("the warning says how far it reaches",
          "1 photo that uses it" in seen.get("msg", ""), seen.get("msg"))
    check("cancelling leaves the tag alone", tag_count("Darkroom") == 1)
    P.once("dialog", lambda d: d.accept())
    P.locator(".psc-tagmenu .psc-delall").first.click(); P.wait_for_timeout(300)
    check("accepting removes it everywhere", tag_count("Darkroom") == 0)
    check("and it says so", "Darkroom" in P.eval_on_selector(".psc-toast",
                                                             "e => e.textContent"))
    P.locator(".psc-toast button").click(); P.wait_for_timeout(300)
    check("Undo restores it", tag_count("Darkroom") == 1)

    print("\n--- tags persist in this browser, not in Ghost")
    P.fill(".psc-q", ""); P.wait_for_timeout(150)
    before_reload = stored()
    check("something was stored", len(before_reload) > 2, before_reload[:60])
    check("nothing empty is stored",
          all(v for v in json.loads(before_reload).values()), before_reload[:80])
    check("stored against photo ids, never a local path",
          all(re.fullmatch(r"[0-9a-f]{16}", k) for k in json.loads(before_reload)),
          str(list(json.loads(before_reload))[:2]))
    tags_then = all_tags()
    P.reload(); P.wait_for_timeout(500)
    check("they survive a reload", all_tags() == tags_then,
          f"{all_tags()} vs {tags_then}")
    check("no JS errors anywhere in the tagging session", not js_errors, str(js_errors[:2]))
    P.evaluate("() => localStorage.clear()")

    print("\n--- the lightbox follows the sort")
    P.select_option(".psc-sort", "name-desc"); P.wait_for_timeout(250)
    first, second = order("name")[0], order("name")[1]
    P.locator(".psc-card:not(.psc-hidden) img").first.click(); P.wait_for_timeout(300)
    # The caption is "name - resolution", so the name is a prefix rather than
    # the whole string. What is being tested is WHICH photograph is open.
    def lb_name():
        return P.eval_on_selector(".psc-cap", "e => e.textContent").split("  ·  ")[0]

    check("opens on the card that was clicked", lb_name() == first, first)
    check("the full-screen caption carries the resolution too",
          " MP" in P.eval_on_selector(".psc-cap", "e => e.textContent"),
          P.eval_on_selector(".psc-cap", "e => e.textContent"))
    check("counter counts every visible card",
          P.eval_on_selector(".psc-count-lb", "e => e.textContent")
          == f"1 / {shown()}")
    P.click(".psc-next"); P.wait_for_timeout(250)
    check("next walks the SORTED order, not the original one",
          lb_name() == second, f"expected {second}")
    P.keyboard.press("Escape"); P.wait_for_timeout(200)
    check("escape closes",
          not P.eval_on_selector(".psc-lb", "e => e.classList.contains('open')"))
    br.close()

srv.shutdown()
print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
