"""
Dry-run harness: exercises every part of photo_scout except the three ML models,
which are stubbed. Generates synthetic images (including deliberate near-dupes
and a blurry frame) and checks that walk -> score -> cache -> dedup -> report works.
"""
import random, shutil, sys, sqlite3
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

TMP = Path("/tmp/ps_test")
if TMP.exists():
    shutil.rmtree(TMP)

random.seed(7); np.random.seed(7)

# the library is read-only, so output must live OUTSIDE the fixture
OUTDIR = Path('/tmp/ps_test_out')
shutil.rmtree(OUTDIR, ignore_errors=True)
ps.DEFAULT_OUT_DIR = OUTDIR

def synth(seed, size=(900, 600), blur=0):
    rng = np.random.default_rng(seed)
    # low-frequency colour field + noise = something with real edge structure
    base = rng.integers(0, 255, (8, 12, 3), dtype=np.uint8)
    img = Image.fromarray(base).resize(size, Image.BICUBIC)
    noise = Image.fromarray(rng.integers(0, 90, (size[1], size[0], 3), dtype=np.uint8))
    img = Image.blend(img, noise, 0.35)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return img

# --- build a fake library: 2 folders, 1 nested subfolder ---------------------
layout = {
    "2011-06-28 - Wyoming": 6,
    "2011-06-28 - Wyoming/Old Faithful": 3,
    "2016-03-09 - Mustang and Mountains": 4,
}
made = []
for folder, n in layout.items():
    d = TMP / folder
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        p = d / f"DSC_{i:04d}.JPG"
        synth(hash((folder, i)) % 10**6).save(p, "JPEG", quality=92)
        made.append(p)

# deliberate near-duplicates: same content, different exposure + tiny crop
src = TMP / "2011-06-28 - Wyoming" / "DSC_0000.JPG"
base = Image.open(src)
for k, (bright, crop) in enumerate([(1.06, 0), (0.93, 4), (1.0, 8)], start=1):
    v = base.point(lambda x, b=bright: min(255, int(x * b)))
    if crop:
        v = v.crop((crop, crop, v.width - crop, v.height - crop)).resize(base.size)
    p = src.with_name(f"DSC_0000 ({k}).JPG")
    v.save(p, "JPEG", quality=92)
    made.append(p)

# one deliberately blurry frame
blurry = TMP / "2016-03-09 - Mustang and Mountains" / "DSC_9999.JPG"
synth(42, blur=9).save(blurry, "JPEG", quality=92)
made.append(blurry)

# a file inside a skipped dir - must NOT be picked up
skip = TMP / ".tmp.drivedownload"
skip.mkdir(exist_ok=True)
synth(1).save(skip / "junk.JPG", "JPEG")

# a non-image - must NOT be picked up
(TMP / "sync.ffs_db").write_text("not an image")

print(f"built {len(made)} real images + 2 decoys")

# --- stub the models ---------------------------------------------------------
class FakeScorer:
    def __init__(self, *a, **kw):
        self.rng = np.random.default_rng(1)
    def score(self, img):
        # correlate fake aesthetic with actual sharpness so blur really lands low
        sharp = ps.sharpness_variance(img)
        aes = float(np.clip(4.2 + np.log1p(sharp) / 3.0 + self.rng.normal(0, .3), 3.5, 8.0))
        tier = self.rng.choice(["primary", "secondary", "distractor"], p=[.55, .25, .20])
        return {
            "aesthetic_raw": aes,
            "nima_raw": float(np.clip(4.4 + (aes - 5.5) * .4 + self.rng.normal(0, .2), 3.5, 6.5)),
            "subject_score": float({"primary": 88, "secondary": 62, "distractor": 18}[tier]
                                   + self.rng.normal(0, 5)),
            "subject_label": {"primary": ps.PRIMARY_PROMPTS[0][1],
                              "secondary": ps.SECONDARY_PROMPTS[0][1],
                              "distractor": ""}[tier],
            "subject_tier": tier,
        }
ps.Scorer = FakeScorer

# --- run it ------------------------------------------------------------------
rc = ps.main(["--root", str(TMP)])
assert rc == 0, f"exit code {rc}"

out = OUTDIR
db = sqlite3.connect(out / "scores.sqlite3"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos").fetchall()

print("\n=== CHECKS ===")
ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    suffix = f"  {extra}" if extra else ""
    print(f"{'PASS' if cond else 'FAIL'}  {label}{suffix}")

check("scored exactly the real images", len(rows) == len(made), f"got {len(rows)}, want {len(made)}")
check("no errors", all(r["error"] is None for r in rows),
      str([r["error"] for r in rows if r["error"]][:2]))
check("skip-dir excluded", not any(".tmp.drivedownload" in r["path"] for r in rows))
check("non-image excluded", not any(r["filename"] == "sync.ffs_db" for r in rows))
check("nested subfolder walked", any("Old Faithful" in (r["folder"] or "") for r in rows))
check("every row has a note", all(r["note"] for r in rows))
check("every row has a verdict", all(r["verdict"] for r in rows))
check("composites in 0-100", all(0 <= r["composite"] <= 100 for r in rows))

dups = [r for r in rows if r["dup_of"]]
dup_group = {r["filename"] for r in dups}
check("near-dupes flagged", len(dups) == 3, f"got {len(dups)}: {sorted(dup_group)}")
check("dupes are the DSC_0000 family", all("DSC_0000" in f for f in dup_group))
# NB: each of the 3 folders has its own DSC_0000.JPG with different content,
# so scope this to the Wyoming folder where the dup family actually lives.
wy0 = [r for r in rows if r["folder"] == "2011-06-28 - Wyoming" and "DSC_0000" in r["filename"]]
check("dup family is 4 files", len(wy0) == 4, f"got {len(wy0)}")
check("exactly one keeper survives",
      sum(1 for r in wy0 if not r["dup_of"]) == 1,
      f"keepers: {[r['filename'] for r in wy0 if not r['dup_of']]}")
check("same-name files in other folders NOT merged",
      all(not r["dup_of"] for r in rows
          if r["filename"] == "DSC_0000.JPG" and r["folder"] != "2011-06-28 - Wyoming"))

br = next(r for r in rows if r["filename"] == "DSC_9999.JPG")
check("blurry frame detected", br["sharpness"] < ps.BLUR_VARIANCE_FLOOR, f"var={br['sharpness']:.1f}")
check("blurry frame penalised", "soft or out of focus" in br["note"])

# reports
html_p, csv_p, short_p = out / "report.html", out / "report.csv", out / "shortlist.csv"
check("report.html written", html_p.exists() and html_p.stat().st_size > 2000)
check("report.csv written", csv_p.exists())
check("shortlist.csv written", short_p.exists())
thumbs = list((out / "thumbs").glob("*.jpg"))
check("thumbnails generated", len(thumbs) == len(made), f"{len(thumbs)}/{len(made)}")

h = html_p.read_text(encoding="utf-8")
check("html retains file:/// hrefs as a no-JS fallback", "file:///" in h)
check("lightbox present", 'id="lb-img"' in h and 'data-view="1"' in h)
check("copy-path fallback present", "navigator.clipboard" in h)
check("no localhost helper", "127.0.0.1" not in h)
ncards = h.count('class="card')
check("html card count matches", ncards == len(made), f"{ncards} cards")
check("no unreplaced placeholders", "__CARDS__" not in h and "__STATS__" not in h)

# resume behaviour: second run must score nothing new
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(TMP)])
second = buf.getvalue()
check("resume skips already-scored", "0 to do" in second, second.splitlines()[1] if len(second.splitlines())>1 else "")

# --folder scoping
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    ps.main(["--root", str(TMP), "--folder", "2016-03-09 - Mustang and Mountains", "--force"])
check("--folder scopes the walk", "Found 5 images and 0 videos" in buf2.getvalue(),
      str([l for l in buf2.getvalue().splitlines() if "Found" in l]))

# --- direct check of the scoring math, independent of the fake model ---------
print("\n=== compose() math ===")
def mk(aes, nima, subj, tier, sharp=500.0, hi=0.0, lo=0.0):
    r = ps.PhotoResult(path="x", rel_path="x", folder="f", filename="x.NEF", mtime=0, size=0,
                       aesthetic_raw=aes, nima_raw=nima, subject_score=subj,
                       subject_tier=tier, subject_label=ps.PRIMARY_PROMPTS[0][1] if tier == "primary" else ps.SECONDARY_PROMPTS[0][1],
                       sharpness=sharp, clip_hi=hi, clip_lo=lo)
    ps.compose(r)
    return r

# Derive the expectation from the configured weights rather than hardcoding a
# number, so retuning the weights can't silently invalidate this check.
_aes_pts = (7.0 - ps.AESTHETIC_RANGE[0]) / (ps.AESTHETIC_RANGE[1] - ps.AESTHETIC_RANGE[0]) * 100
_tec_pts = (5.8 - ps.NIMA_RANGE[0]) / (ps.NIMA_RANGE[1] - ps.NIMA_RANGE[0]) * 100
_expect = (ps.WEIGHT_AESTHETIC * _aes_pts + ps.WEIGHT_TECHNICAL * _tec_pts
           + ps.WEIGHT_SUBJECT * 92.0)
best = mk(7.0, 5.8, 92.0, "primary")
check("composite matches the weighted formula", abs(best.composite - _expect) < 0.05,
      f"got {best.composite:.2f}, expected {_expect:.2f} "
      f"({ps.WEIGHT_AESTHETIC}/{ps.WEIGHT_TECHNICAL}/{ps.WEIGHT_SUBJECT})")
check("weights total 1.0",
      abs(ps.WEIGHT_AESTHETIC + ps.WEIGHT_TECHNICAL + ps.WEIGHT_SUBJECT - 1.0) < 1e-9)
check("high scorer is TOP PICK", best.verdict == "TOP PICK", best.verdict)

worst = mk(4.1, 3.9, 12.0, "distractor")
check("weak image is PASS", worst.verdict == "PASS", f"{worst.composite:.1f}")

soft = mk(7.0, 5.8, 92.0, "primary", sharp=10.0)
check("blur penalty applied", abs((best.composite - soft.composite) - ps.BLUR_PENALTY) < 0.01,
      f"{best.composite:.1f} -> {soft.composite:.1f}")
check("blur mentioned in note", "out of focus" in soft.note)

blown = mk(7.0, 5.8, 92.0, "primary", hi=0.30)
check("highlight clipping penalised", blown.composite < best.composite)
check("clipping mentioned", "highlights are blown" in blown.note)

sec = mk(6.5, 5.5, 70.0, "secondary")
# The tier is what discounts the score; it is no longer spelled out in words on
# the card, where it was the same phrase on four cards in five.
check("secondary tier is recorded", sec.subject_tier == "secondary", sec.subject_tier)
check("and the note names the subject it matched",
      sec.note.split(ps.NOTE_SEP)[2].lower() == ps.SECONDARY_PROMPTS[0][1].lower(),
      sec.note)

print("\n--- the feedback line is short and uniform")
for res in (best, worst, soft, blown, sec):
    parts = res.note.split(ps.NOTE_SEP)
    check(f"{res.verdict:8s} -> {res.note}",
          3 <= len(parts) <= 4
          and parts[0].startswith("Aesthetic ") and parts[1].startswith("Technical ")
          and parts[2][0].isupper()
          and (len(parts) == 3 or parts[3] in ps.DEFECT_TEXTS))
check("nothing the card already shows is repeated",
      not any(g in res.note for res in (best, worst, soft, blown, sec)
              for g in ("TOP PICK", "PASS", "/100", "execution", "squarely")))

check("scores clamp at 100", mk(12.0, 9.0, 100.0, "primary").composite <= 100.0)
check("scores clamp at 0", mk(0.0, 0.0, 0.0, "distractor", sharp=0.0).composite >= 0.0)
check("no-NIMA fallback works", mk(6.5, None, 80.0, "primary").composite > 0)

# monotonicity: better aesthetic must never lower the composite
seq = [mk(a, 5.0, 70.0, "primary").composite for a in (4.5, 5.0, 5.5, 6.0, 6.5, 7.0)]
check("composite monotonic in aesthetic", all(b >= a for a, b in zip(seq, seq[1:])),
      str([round(s, 1) for s in seq]))

# --- perceptual hash sanity --------------------------------------------------
print("\n=== phash ===")
a = synth(101); b = a.point(lambda x: min(255, int(x * 1.1))); c = synth(202)
ha, hb, hc = ps.perceptual_hash(a), ps.perceptual_hash(b), ps.perceptual_hash(c)
check("hash fits 64 bits", all(0 <= h < 2**64 for h in (ha, hb, hc)))
check("exposure shift stays near", ps.hamming64(ha, hb) <= ps.PHASH_HAMMING_THRESHOLD,
      f"distance {ps.hamming64(ha, hb)}")
check("different image is far", ps.hamming64(ha, hc) > ps.PHASH_HAMMING_THRESHOLD,
      f"distance {ps.hamming64(ha, hc)}")

# url helpers on a windows-style path
u = ps.win_file_url(r"D:\Photos\2011-06-28 - Wyoming\DSC_0001.NEF")
f = ps.win_folder_url(r"D:\Photos\2011-06-28 - Wyoming\DSC_0001.NEF")
check("file url shape", u == "file:///D:/Photos/2011-06-28%20-%20Wyoming/DSC_0001.NEF", u)
check("folder url shape", f == "file:///D:/Photos/2011-06-28%20-%20Wyoming", f)

print("\nSample notes:")
for r in sorted(rows, key=lambda r: -r["composite"])[:3]:
    print(f"  [{r['verdict']:9s} {r['composite']:5.1f}] {r['filename']}: {r['note']}")
print(f"  [{br['verdict']:9s} {br['composite']:5.1f}] {br['filename']}: {br['note']}")

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
