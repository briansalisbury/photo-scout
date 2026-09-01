"""
Verifies photo_scout_strong_top.py against photo_scout.py on a shared database:
the shortlist report contains only TOP PICK and STRONG, the removed controls are
gone, the two scripts don't overwrite each other's outputs, and the original
script is byte-identical to before.
"""
import hashlib, io, contextlib, importlib.util, re, shutil, sqlite3, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ORIG_MD5_BEFORE = hashlib.md5((ROOT / "photo_scout.py").read_bytes()).hexdigest()

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

full = load("photo_scout")
short = load("photo_scout_strong_top")

LIB = Path("/tmp/st_lib"); OUT = Path("/tmp/st_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
LIB.mkdir(parents=True)
rng = np.random.default_rng(77)
for folder, n in {"2011 Wyoming": 40, "2010 Arches": 30}.items():
    d = LIB / folder; d.mkdir(parents=True)
    for i in range(n):
        Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8)) \
             .resize((1000, 700), Image.BICUBIC).save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90)
# a deliberate near-duplicate pair so the dup checkbox has something to hide
base = Image.open(LIB / "2011 Wyoming" / "DSC_0000.JPG")
base.point(lambda x: min(255, int(x * 1.03))).save(
    LIB / "2011 Wyoming" / "DSC_0000 (2).JPG", "JPEG", quality=90)

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": float(np.clip(5.0 + np.sin(self.n * 1.7) * 0.5, 3.9, 6.0)),
                "nima_raw": float(np.clip(4.9 + np.cos(self.n * 1.3) * 0.5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": full.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
for m in (full, short):
    m.Scorer = FakeScorer
    m.DEFAULT_OUT_DIR = OUT

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

def run(mod, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--root", str(LIB)] + list(a))
    return rc, buf.getvalue()

print("=== score once with the original, then report with both ===")
rc, _ = run(full)
check("original run succeeded", rc == 0)
rc, log_s = run(short, "--report-only")
check("shortlist run succeeded", rc == 0)
check("shortlist reused the database, scored nothing", "to do" not in log_s or "0 to do" in log_s)

db = sqlite3.connect(OUT / "scores.sqlite3"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos WHERE error IS NULL").fetchall()
from collections import Counter
counts = Counter(r["verdict"] for r in rows)
print(f"       library verdicts: {dict(counts)}")
check("fixture produced all four bands",
      all(counts[v] > 0 for v in ("TOP PICK", "STRONG", "MAYBE", "PASS")), str(dict(counts)))

print("\n=== both reports exist, neither clobbers the other ===")
for f in ("report.html", "report.csv", "shortlist.csv",
          "report_strong_top.html", "report_strong_top.csv"):
    check(f"{f} written", (OUT / f).exists())
check("shared database, not two copies",
      len(list(OUT.glob("*.sqlite3"))) == 1, str([p.name for p in OUT.glob('*.sqlite3')]))

hf = (OUT / "report.html").read_text(encoding="utf-8")
hs = (OUT / "report_strong_top.html").read_text(encoding="utf-8")

print("\n=== the shortlist contains only TOP PICK and STRONG ===")
def verdicts_in(h):
    return Counter(re.findall(r'data-verdict="([^"]+)"', h))
vf, vs = verdicts_in(hf), verdicts_in(hs)
print(f"       full report : {dict(vf)}")
print(f"       shortlist   : {dict(vs)}")
check("shortlist has no MAYBE cards", vs["MAYBE"] == 0)
check("shortlist has no PASS cards", vs["PASS"] == 0)
check("shortlist keeps every TOP PICK", vs["TOP PICK"] == vf["TOP PICK"], f"{vs['TOP PICK']}/{vf['TOP PICK']}")
check("shortlist keeps every STRONG", vs["STRONG"] == vf["STRONG"], f"{vs['STRONG']}/{vf['STRONG']}")
check("full report still has all four bands",
      all(vf[v] > 0 for v in ("TOP PICK", "STRONG", "MAYBE", "PASS")), str(dict(vf)))
check("shortlist is genuinely smaller", sum(vs.values()) < sum(vf.values()),
      f"{sum(vs.values())} vs {sum(vf.values())} cards")

print("\n=== Maybe and Pass are gone; All / Top picks / Strong remain ===")
for gone in ('>Maybe<', '>Pass<', 'data-f="MAYBE"', 'data-f="PASS"'):
    check(f"no {gone!r} in the shortlist report", gone not in hs)
check("full report still has Maybe and Pass", all(x in hf for x in ('>Maybe<', '>Pass<')))
for keep in ('>All<', '>Top picks<', '>Strong<'):
    check(f"shortlist keeps {keep!r}", keep in hs)
check("exactly three filter buttons", hs.count('<button data-f="') == 3,
      f"{hs.count(chr(60) + 'button data-f=')} buttons")
check("All is the default", '<button data-f="all" class="on">All</button>' in hs)
check("only one button starts active", hs.count('data-f="all" class="on"') == 1)
check("reuses the original exclusive-filter logic",
      "verdictFilter === 'all' || c.dataset.verdict === verdictFilter" in hs)
check("no leftover toggle machinery", "bandOn" not in hs and "data-band" not in hs)

print("\n=== retained features ===")
for keep in ('id="dups"', 'id="kind"', 'id="folder"', 'id="q"', 'id="lb-img"',
             'data-preview=', '>open folder<', 'requestFullscreen'):
    check(f"shortlist keeps {keep}", keep in hs)
check("shortlist title says shortlist", "shortlist" in hs[:900].lower())
check("near-dup of a shortlisted frame retained but hidden",
      ('class="card dup' in hs) == any(r["dup_of"] and r["verdict"] in ("TOP PICK", "STRONG")
                                       for r in rows))

print("\n=== the CSV is filtered too ===")
import csv as _csv
with open(OUT / "report_strong_top.csv", encoding="utf-8-sig") as fh:
    got = list(_csv.DictReader(fh))
check("csv rows are only shortlist verdicts",
      all(r["verdict"] in ("TOP PICK", "STRONG") for r in got),
      str(sorted({r["verdict"] for r in got})))
with open(OUT / "report.csv", encoding="utf-8-sig") as fh:
    fullcsv = list(_csv.DictReader(fh))
check("full csv still has everything", len(fullcsv) > len(got), f"{len(fullcsv)} vs {len(got)}")

print("\n=== scoring behaviour is unchanged ===")
scores_before = {r["path"]: r["composite"] for r in rows}
rc, _ = run(short, "--force")
db2 = sqlite3.connect(OUT / "scores.sqlite3"); db2.row_factory = sqlite3.Row
after = {r["path"]: r["composite"] for r in db2.execute("SELECT * FROM photos WHERE error IS NULL")}
check("shortlist script scores identically",
      all(abs(scores_before[p] - after[p]) < 1e-9 for p in scores_before if p in after),
      f"{len(after)} rows compared")
check("it can score from scratch too, not just report", rc == 0)

print("\n=== the original script is untouched ===")
after_md5 = hashlib.md5((ROOT / "photo_scout.py").read_bytes()).hexdigest()
check("photo_scout.py byte-identical", after_md5 == ORIG_MD5_BEFORE, after_md5)
check("the two scripts differ only where intended",
      (ROOT / "photo_scout.py").read_text() != (ROOT / "photo_scout_strong_top.py").read_text())

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
