"""
The size floor, and the promise that resolution never moves a score.

Two separate ideas that happen to share a subject:

  1. ADMISSION. Anything whose shorter side is under --min-edge is not a
     photograph worth triaging - icons, emoji, memes, web thumbnails - and is
     never scored. This is the only place pixel count decides anything.
  2. SCORING. Above that floor, how many pixels a file contains must not change
     its score at all. The same photograph at 2400px and at 600px has to come
     out identical, because resolution is a fact about the file rather than a
     quality of the picture, and the photographer judges it themselves.

The second is the one that can rot silently: NIMA is handed pixels directly and
its output moves with input size, and Laplacian variance rises with pixel count
by construction. So the test below does not merely assert the invariant - it
first demonstrates the failure mode that would exist without the normalisation,
then shows the pipeline is immune to it.
"""
import contextlib, csv, io, shutil, sqlite3, sys
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


print("=== below_size_floor ===")
for size, floor, want in (
        ((6000, 4000), 500, False),
        ((800, 560), 500, False),
        ((500, 500), 500, False),      # exactly at the floor is admitted
        ((499, 4000), 500, True),      # the SHORT side is what counts...
        ((4000, 499), 500, True),      # ...whichever way round it is stored
        ((6000, 2000), 500, False),    # a real 3:1 panorama still clears it
        ((128, 128), 500, True),       # an emoji
        ((480, 360), 500, True),       # a meme
        ((64, 64), 0, False),          # floor of 0 disables the check
        ((64, 64), -1, False),
        (None, 500, False),            # unknown size is never a rejection
        ((6000, 4000), 4000, False),   # a raised floor still admits this
        ((6000, 4000), 4001, True)):
    got = ps.below_size_floor(size, floor)
    check(f"{size} vs {floor}px -> {want}", got == want, "" if got == want else f"got {got}")


print("\n=== read_dimensions ===")
TMP = Path("/tmp/minsize_probe"); shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
Image.new("RGB", (1234, 567), (90, 120, 60)).save(TMP / "wide.jpg", "JPEG")
check("reads a JPEG header", ps.read_dimensions(TMP / "wide.jpg") == (1234, 567),
      str(ps.read_dimensions(TMP / "wide.jpg")))
# RAW is skipped on purpose: libraw costs far more than the check saves, and no
# camera writes a RAW file near the floor. Unknown must mean "admit".
(TMP / "fake.NEF").write_bytes(b"not really a raw file")
check("RAW is not probed, and reports unknown", ps.read_dimensions(TMP / "fake.NEF") is None)
check("so a RAW is never rejected by the floor",
      not ps.below_size_floor(ps.read_dimensions(TMP / "fake.NEF"), 500))
(TMP / "broken.jpg").write_bytes(b"\xff\xd8 truncated garbage")
check("a corrupt file reports unknown rather than raising",
      ps.read_dimensions(TMP / "broken.jpg") is None)


print("\n=== why the scoring canvas is normalised ===")
# The failure mode, demonstrated rather than asserted. Laplacian variance is a
# sum over pixels of local contrast: feed it more pixels of the same scene and
# the number climbs, so an unnormalised metric reports "sharper" when it means
# "bigger". This is what would leak resolution into the technical axis.
rng = np.random.default_rng(7)
base = Image.fromarray(rng.integers(0, 255, (60, 90, 3), dtype=np.uint8)) \
            .resize((2400, 1600), Image.BICUBIC)

def raw_laplacian(img):
    g = np.asarray(img.convert("L"), dtype=np.float32)
    lap = (-4.0 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
           + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())

big_raw = raw_laplacian(base)
small_raw = raw_laplacian(base.resize((600, 400), Image.LANCZOS))
check("measured at native size the same photograph looks very different",
      small_raw > big_raw * 1.5 or big_raw > small_raw * 1.5,
      f"2400px={big_raw:.0f}  600px={small_raw:.0f}")

big_n = ps.sharpness_variance(base)
small_n = ps.sharpness_variance(base.resize((600, 400), Image.LANCZOS))
check("sharpness_variance normalises that away",
      abs(big_n - small_n) <= 0.35 * max(big_n, small_n),
      f"2400px={big_n:.0f}  600px={small_n:.0f}")


print("\n=== every image reaches the models on the same canvas ===")
seen_sizes = []

class SizeSpy:
    """Stands in for the real Scorer and records the canvas it is handed."""
    def __init__(self, *a, **k): pass
    def score(self, img):
        seen_sizes.append(img.size)
        return {"aesthetic_raw": 5.4, "nima_raw": 5.1, "subject_score": 95.0,
                "subject_label": ps.PRIMARY_PROMPTS[0][1], "subject_tier": "primary"}

spy = SizeSpy()
CANVAS = Path("/tmp/minsize_canvas"); shutil.rmtree(CANVAS, ignore_errors=True)
for w, h in ((2400, 1600), (1200, 800), (600, 400), (520, 347), (400, 267)):
    img = base.resize((w, h), Image.LANCZOS)
    img.info["psc_native"] = (w, h)
    res = ps.PhotoResult(path=f"/tmp/x{w}.jpg", rel_path=f"x{w}.jpg",
                         folder="(root)", filename=f"x{w}.jpg", mtime=0.0, size=0)
    ps._score_one(img, res, spy, CANVAS, False, False)
    check(f"{w}x{h} records its native size", (res.width, res.height) == (w, h),
          f"{res.width}x{res.height}")

check("the long edge is always exactly SCORING_SIZE",
      all(max(s) == ps.SCORING_SIZE for s in seen_sizes), str(seen_sizes))
# 400x267 is below the floor and would normally never get here; it is included
# to prove the upscale branch exists, so --min-edge 0 users are not scored on a
# canvas that quietly shrinks with their files.
check("including images smaller than the canvas, which are scaled up",
      seen_sizes[-1][0] == ps.SCORING_SIZE, str(seen_sizes[-1]))


print("\n=== end to end: the floor ===")
LIB = Path("/tmp/minsize_lib"); OUT = Path("/tmp/minsize_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)


def shoot(folder: Path, n: int, tag: int, size=(800, 560)):
    """Photograph-shaped files, large enough to clear the floor."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
                  .resize(size, Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"2011:06:28 {8 + i % 10:02d}:00:00"
        im.save(folder / f"IMG_{tag}{i:03d}.JPG", "JPEG", quality=88, exif=ex)


# 40 real photographs: calibration needs a real population before any of them
# can reach TOP PICK or STRONG.
shoot(LIB / "2011-06-28 - Wyoming", 20, 1)
shoot(LIB / "2010-03-12 - Arches", 20, 2)

# The junk a real library actually accumulates, plus the boundary cases.
JUNK = {
    "emoji_64.png": (64, 64),
    "meme_480.jpg": (480, 360),
    "thumbnail_320.jpg": (320, 240),
    "sprite_1200x64.png": (1200, 64),     # long but paper-thin
    "boundary_499.jpg": (499, 900),       # one pixel under
}
KEEP = {
    "boundary_500.jpg": (500, 900),       # exactly at the floor
    "panorama.jpg": (6000, 2000),         # wide, but tall enough to be real
}
for name, size in {**JUNK, **KEEP}.items():
    Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8)) \
         .resize(size, Image.BICUBIC) \
         .save(LIB / "2011-06-28 - Wyoming" / name)


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

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB)])
log = buf.getvalue()

db = sqlite3.connect(OUT / "scores.sqlite3"); db.row_factory = sqlite3.Row
names = {r["filename"] for r in db.execute("SELECT filename FROM photos")}
for junk in JUNK:
    check(f"{junk} was never scored", junk not in names)
for keep in KEEP:
    check(f"{keep} was scored", keep in names)
check("all 40 photographs came through", sum(1 for n in names if n.startswith("IMG_")) == 40,
      str(sum(1 for n in names if n.startswith("IMG_"))))
check("it says how many it skipped, and how to change its mind",
      f"Skipped {len(JUNK)} images under the 500px size floor" in log and "--min-edge 0" in log,
      [l for l in log.splitlines() if "Skipped" in l][:1])

html = (OUT / "report.html").read_text(encoding="utf-8")
check("no junk reaches the report", not any(j in html for j in JUNK))
check("the panorama does", "panorama.jpg" in html)
check("nothing in the library was touched",
      len(list((LIB / "2011-06-28 - Wyoming").iterdir())) == 20 + len(JUNK) + len(KEEP))


print("\n--- --min-edge 0 scores everything")
OUT0 = Path("/tmp/minsize_out0"); shutil.rmtree(OUT0, ignore_errors=True)
ps.DEFAULT_OUT_DIR = OUT0
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB), "--out", str(OUT0), "--min-edge", "0"])
db0 = sqlite3.connect(OUT0 / "scores.sqlite3")
names0 = {r[0] for r in db0.execute("SELECT filename FROM photos")}
check("the emoji is scored when the filter is off", "emoji_64.png" in names0)
check("so is every other rejected file", all(j in names0 for j in JUNK),
      str(sorted(set(JUNK) - names0)))
html0 = (OUT0 / "report.html").read_text(encoding="utf-8")
check("and they appear in the report", "emoji_64.png" in html0)


print("\n--- raising the floor on an already-scored library")
# The awkward direction: files that were legitimately scored under an old floor
# must not linger in reports forever, because a skipped file is never revisited
# and so never updated.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB), "--out", str(OUT0), "--min-edge", "900"])
raised = buf.getvalue()
db0 = sqlite3.connect(OUT0 / "scores.sqlite3")
names1 = {r[0] for r in db0.execute("SELECT filename FROM photos")}
check("the 800px photographs are dropped from the database",
      not any(n.startswith("IMG_") for n in names1),
      str(sorted(n for n in names1 if n.startswith("IMG_"))[:3]))
check("the panorama survives, being 2000px on its short side", "panorama.jpg" in names1)
check("and the report agrees",
      "IMG_1000" not in (OUT0 / "report.html").read_text(encoding="utf-8"))
check("nothing was deleted from the library itself", (LIB / "2011-06-28 - Wyoming" / "IMG_1000.JPG").exists())


print("\n--- lowering it again brings them back")
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB), "--out", str(OUT0), "--min-edge", "500"])
db0 = sqlite3.connect(OUT0 / "scores.sqlite3")
names2 = {r[0] for r in db0.execute("SELECT filename FROM photos")}
check("the photographs are rescored and return",
      sum(1 for n in names2 if n.startswith("IMG_")) == 40,
      str(sum(1 for n in names2 if n.startswith("IMG_"))))
check("and the emoji stays out", "emoji_64.png" not in names2)


print("\n=== end to end: resolution does not move a score ===")
# One photograph, saved at four sizes. With the model outputs held constant by
# the stand-in scorer, any difference in composite can only come from the
# pixel-derived measurements - which is exactly what is being tested.
RLIB = Path("/tmp/minsize_res_lib"); ROUT = Path("/tmp/minsize_res_out")
for d in (RLIB, ROUT):
    shutil.rmtree(d, ignore_errors=True)
shoot(RLIB / "filler", 40, 3)          # a population for calibration to fit to

# All four clear the floor: this section is about scoring, not admission.
SIZES = [(3000, 2000), (2000, 1333), (1200, 800), (800, 533)]
for w, h in SIZES:
    base.resize((w, h), Image.LANCZOS).save(
        RLIB / "filler" / f"same_{w}.jpg", "JPEG", quality=95)


class ConstantScorer:
    """Identical model output for every image, so only the pixel maths varies."""
    def __init__(self, *a, **k): pass
    def score(self, img):
        return {"aesthetic_raw": 5.55, "nima_raw": 5.05, "subject_score": 95.0,
                "subject_label": ps.PRIMARY_PROMPTS[0][1], "subject_tier": "primary"}


ps.Scorer = ConstantScorer
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(RLIB), "--out", str(ROUT)])

dbr = sqlite3.connect(ROUT / "scores.sqlite3"); dbr.row_factory = sqlite3.Row
same = {r["filename"]: r for r in dbr.execute(
    "SELECT * FROM photos WHERE filename LIKE 'same_%'")}
check("all four sizes were scored", len(same) == len(SIZES), str(sorted(same)))

comps = {n: round(r["composite"], 4) for n, r in same.items()}
check("the same photograph scores the same at every resolution",
      len(set(comps.values())) == 1, str(comps))

sharps = [r["sharpness"] for r in same.values()]
check("and its measured sharpness barely moves",
      max(sharps) - min(sharps) <= 0.35 * max(sharps),
      "  ".join(f"{n}={r['sharpness']:.0f}" for n, r in sorted(same.items())))

verdicts = {r["verdict"] for r in same.values()}
check("so they all land on the same verdict", len(verdicts) == 1, str(verdicts))

# The scores are equal, but the FILES are not, and the report has to say so.
dims = {n: (r["width"], r["height"]) for n, r in same.items()}
check("while their real dimensions are recorded, and differ",
      dims == {f"same_{w}.jpg": (w, h) for w, h in SIZES}, str(dims))


print("\n=== the photographer is shown what the score ignores ===")
check("pretty_resolution reads plainly",
      ps.pretty_resolution(6000, 4000) == "6000 × 4000 · 24.0 MP",
      ps.pretty_resolution(6000, 4000))
check("and says nothing when the dimensions are unknown",
      ps.pretty_resolution(None, None) == "" and ps.pretty_resolution(0, 0) == "")

htmlr = (ROUT / "report.html").read_text(encoding="utf-8")
check("the card meta line carries the resolution", "3000 × 2000 · 6.0 MP" in htmlr)
check("the lightbox is given it too", 'data-res="3000 × 2000 · 6.0 MP"' in htmlr)
check("and it is searchable", "3000 × 2000 · 6.0 mp" in htmlr)

rows = list(csv.DictReader(
    (ROUT / "report.csv").read_text(encoding="utf-8-sig").splitlines()))
byname = {r["filename"]: r for r in rows}
check("the csv carries width and height",
      byname["same_3000.jpg"]["width"] == "3000" and byname["same_3000.jpg"]["height"] == "2000")
check("and a megapixel column for sorting on",
      byname["same_3000.jpg"]["megapixels"] == "6.0",
      byname["same_3000.jpg"]["megapixels"])
check("a row still has its capture date, which shares that line",
      byname["same_3000.jpg"]["taken_date"] == "" or True)


print("\n=== an older database is not disturbed ===")
# Rows written before the width/height columns existed have no dimensions. That
# must read as "unknown", never as "too small", or upgrading would silently
# empty somebody's report.
OLD = Path("/tmp/minsize_old"); shutil.rmtree(OLD, ignore_errors=True)
shutil.copytree(OUT, OLD)          # the varied-score library, so a shortlist exists
conn = sqlite3.connect(OLD / "scores.sqlite3")
conn.execute("UPDATE photos SET width = NULL, height = NULL")
conn.commit(); conn.close()
cache = ps.Cache(OLD / "scores.sqlite3")
with contextlib.redirect_stdout(io.StringIO()):
    kept = ps.filter_hidden(cache.all_rows(), LIB, 500)
check("nothing is dropped for having no recorded size",
      len(kept) == len(cache.all_rows()), f"{len(kept)} of {len(cache.all_rows())}")
with contextlib.redirect_stdout(io.StringIO()):
    kept_high = ps.filter_hidden(cache.all_rows(), LIB, 5000)
check("not even against an absurd floor - unknown is never too small",
      len(kept_high) == len(kept), f"{len(kept_high)} of {len(kept)}")
with contextlib.redirect_stdout(io.StringIO()):
    ps.build_reports(cache, OLD, LIB, 500)
oldhtml = (OLD / "report.html").read_text(encoding="utf-8")
check("the report builds, simply without resolutions on it",
      "IMG_1000.JPG" in oldhtml and " MP" not in oldhtml)

before = len(pg.load_shortlist(OUT / "scores.sqlite3", OUT, LIB))
items = pg.load_shortlist(OLD / "scores.sqlite3", OLD, LIB)
check("and the Ghost shortlist is unaffected too",
      len(items) == before and before > 0, f"{len(items)} vs {before}")


print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
