"""
hide_from_photo_scout: folders whose contents never reach a report.

Two halves have to work, and the second is the one people actually need:

  1. The walker prunes the folder, so nothing inside is ever opened or scored.
  2. The report filters on the path, so dropping the folder into a library that
     has ALREADY been scored hides those photographs immediately, with no
     rescore - and renaming it back brings them straight back.
"""
import contextlib, io, shutil, sqlite3, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps                      # noqa: E402
import photo_scout_ghost as pg                # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


print("=== recognising a hidden path ===")
H = ps.HIDE_DIR_NAME
for path, want in (
        (f"{H}/DSC_0001.NEF", True),
        (f"2011 Wyoming/{H}/DSC_0001.NEF", True),
        (f"2011 Wyoming/{H}/further/down/DSC_0001.NEF", True),   # any depth
        (f"D:\\Photos\\2011 Wyoming\\{H}\\DSC_0001.NEF", True),  # windows paths
        (f"{H.upper()}/DSC_0001.NEF", True),                     # case
        (f"  {H}  /DSC_0001.NEF", True),                         # stray spaces
        ("2011 Wyoming/DSC_0001.NEF", False),
        # near misses that must NOT be hidden
        (f"{H}_backup/DSC_0001.NEF", False),
        (f"my_{H}/DSC_0001.NEF", False),
        (f"2011 Wyoming/{H}.NEF", False),        # a FILE by that name is not a folder
        ("", False), (None, False)):
    got = ps.is_hidden(path)
    check(f"{str(path)[:44]!r} -> {want}", got == want, "" if got == want else f"got {got}")

print("\n=== end to end ===")
LIB = Path("/tmp/hid_lib"); OUT = Path("/tmp/hid_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
rng = np.random.default_rng(21)


def shoot(folder: Path, n: int, tag: int):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
                  .resize((800, 560), Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"2011:06:28 {8 + i % 10:02d}:00:00"
        im.save(folder / f"IMG_{tag}{i:03d}.JPG", "JPEG", quality=88, exif=ex)


shoot(LIB / "2011-06-28 - Wyoming", 20, 1)
shoot(LIB / "2010-03-12 - Arches", 20, 2)
SECRET = LIB / "2010-03-12 - Arches" / "hide_from_photo_scout"
shoot(SECRET, 6, 9)
shoot(SECRET / "even deeper", 4, 8)


class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": float(np.clip(5.0 + np.sin(self.n * 1.7) * 0.6, 3.9, 6.1)),
                "nima_raw": float(np.clip(4.9 + np.cos(self.n * 1.1) * 0.5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}


ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT
pg.ps.DEFAULT_OUT_DIR = OUT

found = [str(p) for p in ps.iter_media(LIB, None)]
check("the walker finds the visible photographs", len(found) == 40, str(len(found)))
check("and none of the hidden ones",
      not any("hide_from_photo_scout" in f for f in found),
      str([f for f in found if "hide_from_photo_scout" in f][:2]))
check("including the ones nested deeper inside it",
      not any("even deeper" in f for f in found))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB)])
log = buf.getvalue()
check("it says which folder it skipped", "hide_from_photo_scout" in log,
      [l for l in log.splitlines() if "Hidden" in l][:1])

db = sqlite3.connect(OUT / "scores.sqlite3"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos").fetchall()
check("nothing hidden was scored", len(rows) == 40, str(len(rows)))
check("no hidden path in the database",
      not any("hide_from_photo_scout" in (r["path"] or "") for r in rows))

h = (OUT / "report.html").read_text(encoding="utf-8")
check("no hidden photograph in the report",
      "IMG_9000" not in h and "IMG_8000" not in h)

print("\n=== hiding photographs that were ALREADY scored ===")
# This is the real use: the library is scored, then you decide to hide a folder.
# Nothing should need rescoring.
LIB2 = Path("/tmp/hid_lib2"); OUT2 = Path("/tmp/hid_out2")
for d in (LIB2, OUT2):
    shutil.rmtree(d, ignore_errors=True)
shoot(LIB2 / "2011-06-28 - Wyoming", 20, 1)
PRIVATE = LIB2 / "2011-06-28 - Wyoming" / "Private"
shoot(PRIVATE, 12, 7)
ps.DEFAULT_OUT_DIR = OUT2
pg.ps.DEFAULT_OUT_DIR = OUT2
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB2)])

db2 = sqlite3.connect(OUT2 / "scores.sqlite3"); db2.row_factory = sqlite3.Row
before = db2.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
h = (OUT2 / "report.html").read_text(encoding="utf-8")
check("everything is scored to begin with", before == 32, str(before))
check("and the private folder is in the report", "IMG_7000" in h)

# Now rename it. No rescore - just build the reports again.
PRIVATE.rename(LIB2 / "2011-06-28 - Wyoming" / "hide_from_photo_scout")
cache = ps.Cache(OUT2 / "scores.sqlite3")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.build_reports(cache, OUT2, LIB2)
log2 = buf.getvalue()
h = (OUT2 / "report.html").read_text(encoding="utf-8")
csv = (OUT2 / "report.csv").read_text(encoding="utf-8-sig")
check("the report drops them with no rescore", "IMG_7000" not in h)
check("so does the csv", "IMG_7000" not in csv)
check("the visible photographs are untouched", "IMG_1000" in h)
check("and it says how many it left out, and why",
      "12 photographs are no longer where they were scored" in log2,
      [l for l in log2.splitlines() if "Hidden" in l][:1])
check("their rows are still in the database, not deleted",
      db2.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == before)
check("nothing was moved or deleted on disk",
      len(list((LIB2 / "2011-06-28 - Wyoming" / "hide_from_photo_scout").iterdir())) == 12)

print("\n--- a full rescan agrees")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB2)])
rescan = buf.getvalue()
check("a normal rescan also leaves them out",
      "IMG_7000" not in (OUT2 / "report.html").read_text(encoding="utf-8"))
check("and does not rescore anything inside the hidden folder",
      "hide_from_photo_scout" in rescan)

print("\n--- an unplugged drive does not empty the report")
# The dangerous branch: if the library is unreachable, EVERY file looks missing.
# It must fall back to the database rather than reporting that nothing exists.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    kept = ps.filter_hidden(cache.all_rows(), Path("/tmp/definitely-not-mounted"))
gone = buf.getvalue()
check("nothing is dropped when the library cannot be reached",
      len(kept) == before, f"{len(kept)} of {before}")
check("and it says so plainly", "not reachable" in gone,
      [l for l in gone.splitlines()][:1])

print("\n--- and un-hiding is just as immediate")
(LIB2 / "2011-06-28 - Wyoming" / "hide_from_photo_scout").rename(PRIVATE)
with contextlib.redirect_stdout(io.StringIO()):
    ps.build_reports(cache, OUT2, LIB2)
check("renaming the folder back restores them, still with no rescore",
      "IMG_7000" in (OUT2 / "report.html").read_text(encoding="utf-8"))
cache.close() if hasattr(cache, "close") else None

print("\n=== the Ghost page honours it too ===")
PRIVATE.rename(LIB2 / "2011-06-28 - Wyoming" / "hide_from_photo_scout")
with contextlib.redirect_stdout(io.StringIO()):
    ps.build_reports(ps.Cache(OUT2 / "scores.sqlite3"), OUT2, LIB2)
items = pg.load_shortlist(OUT2 / "scores.sqlite3", OUT2, LIB2)
check("the shortlist excludes hidden photographs",
      not any("IMG_7" in i["filename"] for i in items),
      str([i["filename"] for i in items if "IMG_7" in i["filename"]][:3]))
check("and still contains the visible ones", len(items) > 0, str(len(items)))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pg.main(["--site", "https://example.com", "--out", str(OUT2),
                  "--manifest", "/tmp/hid_manifest.sqlite3",
                  "--dry-run", "--emit-html", "hid.html"])
page = (OUT2 / "hid.html").read_text(encoding="utf-8")
check("publish succeeds", rc == 0, f"rc={rc}")
check("no hidden photograph reaches the published markup", "IMG_7000" not in page)
check("no hidden folder name leaks into it", "hide_from_photo_scout" not in page)

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
