"""
Lightbox verification: previews are actually rendered at the right size, the
report wires each card to its own preview, and the overlay's markup/handlers are
all present and mutually consistent. Also confirms the localhost helper is gone.
"""
import re, shutil, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

T = Path("/tmp/ps_lb")
shutil.rmtree(T, ignore_errors=True)
(T / "2011 Wyoming").mkdir(parents=True)

# the library is read-only, so output must live OUTSIDE the fixture
OUTDIR = Path('/tmp/ps_lb_out')
shutil.rmtree(OUTDIR, ignore_errors=True)
ps.DEFAULT_OUT_DIR = OUTDIR

rng = np.random.default_rng(4)
# deliberately larger than PREVIEW_SIZE so downscaling is exercised
for i in range(5):
    a = rng.integers(0, 255, (14, 20, 3), dtype=np.uint8)
    Image.fromarray(a).resize((3008, 2000), Image.BICUBIC).save(
        T / "2011 Wyoming" / f"DSC_{i:04d}.JPG", "JPEG", quality=90)
# one smaller than PREVIEW_SIZE - must NOT be upscaled
Image.fromarray(rng.integers(0, 255, (600, 900, 3), dtype=np.uint8)).save(
    T / "2011 Wyoming" / "SMALL.JPG", "JPEG", quality=90)

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": 5.0 + self.n * 0.15, "nima_raw": 5.0,
                "subject_score": 90.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
ps.Scorer = FakeScorer

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

ps.main(["--root", str(T)])
out = OUTDIR
h = (out / "report.html").read_text(encoding="utf-8")

print("\n=== previews on disk ===")
previews = sorted((out / "previews").glob("*.jpg"))
check("one preview per image", len(previews) == 6, f"got {len(previews)}")
sizes = {}
for p in previews:
    im = Image.open(p)
    sizes[p.name] = im.size
big = [s for s in sizes.values() if max(s) == ps.PREVIEW_SIZE]
small = [s for s in sizes.values() if max(s) == 900]
check(f"large originals downscaled to {ps.PREVIEW_SIZE}px", len(big) == 5, str(sorted(set(big))))
check("small original NOT upscaled", len(small) == 1, str(small))
check("previews are bigger than thumbnails",
      min(max(s) for s in sizes.values()) > ps.THUMB_SIZE)
thumbs = sorted((out / "thumbs").glob("*.jpg"))
check("thumbnails still generated", len(thumbs) == 6, f"got {len(thumbs)}")
check("thumbs capped at THUMB_SIZE",
      all(max(Image.open(t).size) <= ps.THUMB_SIZE for t in thumbs))

print("\n=== the helper is gone ===")
for gone in ("127.0.0.1", "__TOKEN__", "__HELPER__", "/reveal", "/ping", "fetch("):
    check(f"no trace of '{gone}'", gone not in h)
src = (ROOT / "photo_scout.py").read_text()
for gone in ("def run_serve", "ensure_token", "socketserver", '"--serve"'):
    check(f"source no longer defines {gone}", gone not in src)

print("\n=== lightbox markup ===")
for el in ("lb-img", "lb-prev", "lb-next", "lb-close", "lb-zoom", "lb-full",
           "lb-count", "lb-note", "lb-copy", "lb-missing", "lb-folder"):
    check(f"#{el} present", f'id="{el}"' in h)
check("view link on cards", 'data-view="1"' in h)
check("open-folder link retained", ">open folder<" in h)
check("no direct NEF/file link remains", ">open file<" not in h)

print("\n=== every card is wired to its own preview ===")
card_previews = re.findall(r'data-preview="([^"]*)"', h)
check("one data-preview per card", len(card_previews) == 6, f"got {len(card_previews)}")
check("all previews non-empty", all(p for p in card_previews))
check("previews are distinct", len(set(card_previews)) == 6)
missing = [p for p in card_previews if not (out / p).exists()]
check("every referenced preview exists on disk", not missing, str(missing[:2]))
for attr in ("data-path", "data-name", "data-note", "data-score",
             "data-folderurl", "data-verdicttext"):
    check(f"{attr} on every card", len(re.findall(attr + r'="', h)) == 6)

print("\n=== handlers and keys ===")
for frag in ("ArrowRight", "ArrowLeft", "Escape", "requestFullscreen",
             "toggleZoom", "navigator.clipboard", "new Image().src"):
    check(f"handler/behaviour: {frag}", frag in h)
check("filter and lightbox share the visible() list", "visible()" in h)
check("zoom class toggling defined", "'actual'" in h and "#lb-stage.actual" in h)

print("\n=== missing-preview fallback ===")
for p in previews:
    p.unlink()
ps.main(["--root", str(T), "--report-only"])
h2 = (out / "report.html").read_text(encoding="utf-8")
empties = re.findall(r'data-preview=""', h2)
check("cards report no preview when files are gone", len(empties) == 6, f"got {len(empties)}")
check("overlay still explains itself", "No preview was generated" in h2)

print("\n=== --no-previews ===")
shutil.rmtree(OUTDIR)
ps.main(["--root", str(T), "--no-previews"])
check("no previews directory created", not (OUTDIR / "previews").exists())
check("run still succeeds", (OUTDIR / "report.html").exists())

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
