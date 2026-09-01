"""
Proves the photo library is read-only.

Takes a byte-level fingerprint of every file under the library (name, size,
mtime, content hash) plus the full directory tree, runs the complete pipeline,
and asserts the fingerprint is bit-identical afterwards. Also checks that output
defaults outside the library and that pointing it inside is refused.
"""
import hashlib, io, contextlib, shutil, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

LIB = Path("/tmp/ro_library")
WORK = Path("/tmp/ro_workdir")
for d in (LIB, WORK):
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)

rng = np.random.default_rng(21)
# >= 30 keepers so --calibrate has enough to work with
for folder, n in {"2011 Wyoming": 18, "2011 Wyoming/Old Faithful": 8, "2010 Arches": 12}.items():
    d = LIB / folder
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
             .resize((1400, 950), Image.BICUBIC) \
             .save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90)
# a clip too, so the video path is exercised against the read-only library
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                "-i", "testsrc2=s=960x540:r=25:d=8", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(LIB / "2011 Wyoming" / "CLIP.MP4")], check=True)
# sidecars and oddities that must also survive untouched
(LIB / "2011 Wyoming" / "DSC_0000.xmp").write_text("<x>sidecar</x>")
(LIB / "sync.ffs_db").write_bytes(b"\x00\x01sync state")

def fingerprint(base: Path) -> dict:
    out = {}
    for p in sorted(base.rglob("*")):
        rel = str(p.relative_to(base))
        if p.is_dir():
            out[rel + "/"] = ("dir", None, None)
        else:
            st = p.stat()
            out[rel] = (st.st_size, int(st.st_mtime),
                        hashlib.sha256(p.read_bytes()).hexdigest())
    return out

before = fingerprint(LIB)
print(f"library fingerprint: {len(before)} entries")

class FakeScorer:
    def __init__(self, *a, **k): pass
    def score(self, img):
        # high enough that surviving video frames clear VIDEO_EXTRACT_MIN_SCORE,
        # so the extraction path writes into the work dir during this run
        self.n = getattr(self, "n", 0) + 1
        return {"aesthetic_raw": 6.9 - (self.n % 7) * 0.05, "nima_raw": 6.0,
                "subject_score": 99.0,
                "subject_label": ps.PRIMARY_PROMPTS[0][1], "subject_tier": "primary"}
ps.Scorer = FakeScorer

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

OUT = WORK / "_photo_scout"

print("\n=== a full pipeline run against the library ===")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(LIB), "--out", str(OUT)])
    ps.main(["--root", str(LIB), "--out", str(OUT), "--calibrate"])
    ps.main(["--root", str(LIB), "--out", str(OUT), "--recompute"])
    ps.main(["--root", str(LIB), "--out", str(OUT), "--report-only"])
    ps.main(["--root", str(LIB), "--out", str(OUT), "--force"])
log = buf.getvalue()
check("pipeline ran", (OUT / "report.html").exists())
check("log states the library is read-only", "Library (read-only)" in log)

after = fingerprint(LIB)
added = sorted(set(after) - set(before))
removed = sorted(set(before) - set(after))
changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])

print("\n=== the library must be byte-identical ===")
check("nothing added to the library", not added, str(added[:5]))
check("nothing removed from the library", not removed, str(removed[:5]))
check("no file contents or mtimes changed", not changed, str(changed[:5]))
check("entry count unchanged", len(before) == len(after), f"{len(before)} -> {len(after)}")
check("no _photo_scout inside the library", not (LIB / "_photo_scout").exists())
check("no stray files at the library root",
      sorted(p.name for p in LIB.iterdir()) ==
      ["2010 Arches", "2011 Wyoming", "sync.ffs_db"],
      str(sorted(p.name for p in LIB.iterdir())))

print("\n=== everything landed in the work directory instead ===")
for want in ("scores.sqlite3", "report.html", "report.csv", "thumbs",
             "previews", "calibration.json"):
    check(f"output has {want}", (OUT / want).exists())
check("extracted stills go there too", (OUT / "extracted_stills").exists())

print("\n=== pointing --out inside the library is refused ===")
for bad in (LIB, LIB / "_photo_scout", LIB / "2011 Wyoming" / "out"):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ps.main(["--root", str(LIB), "--out", str(bad)])
    check(f"refused --out {bad.name!r}", rc == 2 and "read-only" in buf.getvalue(),
          f"rc={rc}")
check("refusal created nothing", not (LIB / "_photo_scout").exists())

print("\n=== --reset cannot touch the library ===")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    refused = ps.run_reset(LIB, LIB / "_photo_scout", True)
check("reset refuses a target inside the library", refused is False)
# and it must refuse a directory that isn't ours, whatever it's called
FOREIGN = Path("/tmp/ro_foreign"); shutil.rmtree(FOREIGN, ignore_errors=True)
FOREIGN.mkdir(); (FOREIGN / "someones_taxes.pdf").write_text("important")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    refused = ps.run_reset(LIB, FOREIGN, True)
check("reset refuses a directory with no scores.sqlite3",
      refused is False and (FOREIGN / "someones_taxes.pdf").exists(),
      buf.getvalue().strip().splitlines()[0][11:60] if buf.getvalue() else "")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    refused = ps.run_reset(LIB, LIB, True)
check("reset refuses the library root", refused is False and LIB.exists())
check("library still intact after reset attempts", fingerprint(LIB) == before)

print("\n=== default output location ===")
check("default is beside the script, not in the library",
      ps.DEFAULT_OUT_DIR.parent == Path(ps.__file__).resolve().parent,
      str(ps.DEFAULT_OUT_DIR))

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
