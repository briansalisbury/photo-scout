"""
Video-path verification. Generates real clips with ffmpeg, runs the full
pipeline over a mixed photo+video library, and checks sampling, timestamp
accuracy, frame dedup, full-resolution extraction, and v1->v2 DB migration.

Each clip segment has a known solid colour and a white box in a known position,
so an extracted still can be verified two ways: the colour proves the seek
landed in the right segment, the box gives phash real structure to work with.
"""
import shutil, sqlite3, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

TMP = Path("/tmp/ps_vid")          # the "library" the script scans
BUILD = Path("/tmp/ps_vid_build")  # scratch for intermediate segments - MUST be
                                   # outside TMP, or dedup correctly merges the
                                   # segments with the concatenated clip
for d in (TMP, BUILD):
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

# the library is read-only, so output must live OUTSIDE the fixture
OUTDIR = Path('/tmp/ps_vid_out')
shutil.rmtree(OUTDIR, ignore_errors=True)
ps.DEFAULT_OUT_DIR = OUTDIR

W, H = 1280, 720
SEG = 5.0                       # seconds per colour segment
EVERY = 3.0                     # sampling interval under test
SEGMENTS = [                    # (hex colour, box x, box y)
    ("0xE02020", 100, 100),
    ("0x20C020", 500, 120),
    ("0x2040E0", 900, 300),
    ("0xE0D020", 200, 420),
    ("0xC020C0", 700, 450),
]
EXPECTED_RGB = [(0xE0, 0x20, 0x20), (0x20, 0xC0, 0x20), (0x20, 0x40, 0xE0),
                (0xE0, 0xD0, 0x20), (0xC0, 0x20, 0xC0)]

def ff(args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])

# --- multi-segment clip: 5 x 5s = 25s ---------------------------------------
parts = []
for i, (col, bx, by) in enumerate(SEGMENTS):
    p = BUILD / f"seg{i}.mp4"
    ff(["-f", "lavfi", "-i", f"color=c={col}:s={W}x{H}:r=25:d={SEG}",
        "-vf", f"drawbox=x={bx}:y={by}:w=220:h=220:color=white@1:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", str(p)])
    parts.append(p)

concat_list = BUILD / "list.txt"
concat_list.write_text("".join(f"file '{p}'\n" for p in parts))
MULTI = TMP / "2019-07-04 - Canyon Drive" / "CLIP_0001.MP4"
MULTI.parent.mkdir(parents=True, exist_ok=True)
ff(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(MULTI)])

# --- static clip: 12s of one unchanging composition -------------------------
STATIC = TMP / "2019-07-04 - Canyon Drive" / "CLIP_0002.MP4"
ff(["-f", "lavfi", "-i", f"color=c=0x304060:s={W}x{H}:r=25:d=12",
    "-vf", "drawbox=x=400:y=200:w=300:h=250:color=white@1:t=fill",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(STATIC)])

# --- a couple of stills in the same tree, so it's genuinely one mixed pass ---
rng = np.random.default_rng(3)
for i in range(3):
    a = rng.integers(0, 255, (6, 9, 3), dtype=np.uint8)
    Image.fromarray(a).resize((800, 533), Image.BICUBIC).save(
        MULTI.parent / f"DSC_{i:04d}.JPG", "JPEG", quality=92)

print(f"built 2 clips + 3 stills; multi-clip duration "
      f"{ps.probe_duration(MULTI):.2f}s (expect {SEG*len(SEGMENTS):.2f})")

# --- stub the models ---------------------------------------------------------
class FakeScorer:
    def __init__(self, *a, **kw): self.rng = np.random.default_rng(5)
    def score(self, img):
        return {"aesthetic_raw": 7.2, "nima_raw": 5.9, "subject_score": 90.0,
                "subject_label": ps.PRIMARY_PROMPTS[0][1], "subject_tier": "primary"}
ps.Scorer = FakeScorer
ps.VIDEO_EXTRACT_MIN_SCORE = 0.0      # extract every survivor
ps.VIDEO_EXTRACT_MAX_PER_VIDEO = 3    # ...but exercise the per-clip cap

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

# --- v1 -> v2 migration ------------------------------------------------------
print("\n=== migration from a v1 database ===")
out = OUTDIR
out.mkdir(parents=True, exist_ok=True)
v1 = sqlite3.connect(out / "scores.sqlite3")
v1.executescript("""
CREATE TABLE photos (path TEXT PRIMARY KEY, rel_path TEXT, folder TEXT, filename TEXT,
 mtime REAL, size INTEGER, phash TEXT, aesthetic_raw REAL, nima_raw REAL,
 subject_score REAL, subject_label TEXT, subject_tier TEXT, sharpness REAL,
 clip_hi REAL, clip_lo REAL, composite REAL, verdict TEXT, note TEXT,
 dup_of TEXT, error TEXT, scored_at REAL);
INSERT INTO photos (path, folder, filename, composite, verdict, note, phash, mtime, size)
 VALUES ('D:\\old\\LEGACY.NEF','old','LEGACY.NEF',91.5,'TOP PICK','from v1','00ff00ff00ff00ff',1.0,10);
""")
v1.commit(); v1.close()

c = ps.Cache(out / "scores.sqlite3")
cols = {r["name"] for r in c.conn.execute("PRAGMA table_info(photos)")}
check("new columns added", {"source_type", "source_video", "timestamp_s",
                            "extracted_path"} <= cols)
legacy = c.conn.execute("SELECT * FROM photos WHERE filename='LEGACY.NEF'").fetchone()
check("v1 row preserved", legacy["composite"] == 91.5 and legacy["note"] == "from v1")
check("v1 row backfilled as photo", legacy["source_type"] == "photo")
c.conn.close()

# --- full run ----------------------------------------------------------------
print("\n=== mixed photo + video pass ===")
rc = ps.main(["--root", str(TMP), "--video-every", str(EVERY)])
assert rc == 0

db = sqlite3.connect(out / "scores.sqlite3"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos WHERE filename != 'LEGACY.NEF'").fetchall()
frames = [r for r in rows if r["source_type"] == "video_frame"]
photos = [r for r in rows if r["source_type"] == "photo"]
multi = sorted([r for r in frames if r["source_video"] == str(MULTI)],
               key=lambda r: r["timestamp_s"])
static = [r for r in frames if r["source_video"] == str(STATIC)]

check("stills scored alongside video", len(photos) == 3, f"got {len(photos)}")
check("exactly the two clips sampled",
      {r["source_video"] for r in frames} == {str(MULTI), str(STATIC)},
      str(sorted(Path(v).name for v in {r["source_video"] for r in frames})))
# 25s at one frame per 3s -> t=0,3,...,21 on the grid, plus one tail frame
check("multi-clip frame count", len(multi) == 9, f"got {len(multi)}")
check("static-clip frame count", len(static) == 5, f"got {len(static)}")
check("timestamps follow the sampling grid",
      [round(r["timestamp_s"], 3) for r in multi[:8]] == [i * EVERY for i in range(8)],
      str([round(r["timestamp_s"], 2) for r in multi]))
check("tail of the clip is sampled",
      25.0 - multi[-1]["timestamp_s"] < EVERY,
      f"last frame at {multi[-1]['timestamp_s']:.2f}s of 25.00s")
check("no clip is left unsampled", len(frames) > 0 and
      {r["source_video"] for r in frames} == {str(MULTI), str(STATIC)})
check("no frame errors", all(r["error"] is None for r in frames),
      str([r["error"] for r in frames if r["error"]][:1]))
# The timestamp lives in the VIDEO badge on the card (checked below), so the
# note carries only what is specific to the frame: its scores and subject.
check("frames get the same short note as photographs",
      all((r["note"] or "").startswith("Aesthetic ") for r in frames),
      str([r["note"] for r in frames][:1]))
check("without restating what the badge already says",
      not any("Video frame at" in (r["note"] or "") for r in frames))

# virtual path round-trip
base, ts = ps.split_virtual_path(multi[2]["path"])
check("virtual path round-trips", base == str(MULTI) and abs(ts - 6.0) < 1e-6,
      f"{base} @ {ts}")
check("plain path unaffected by round-trip",
      ps.split_virtual_path(r"D:\Photos\DSC_0001.NEF") == (r"D:\Photos\DSC_0001.NEF", None))

# dedup: a locked-off clip should collapse to one survivor
static_keepers = [r for r in static if not r["dup_of"]]
check("static clip collapses to 1 keeper", len(static_keepers) == 1,
      f"{len(static_keepers)} keepers of {len(static)}")
multi_keepers = [r for r in multi if not r["dup_of"]]
check("distinct segments survive dedup", len(multi_keepers) >= 5,
      f"{len(multi_keepers)} keepers of {len(multi)}")

# --- extraction --------------------------------------------------------------
print("\n=== full-resolution extraction ===")
extracted = [r for r in frames if r["extracted_path"]]
check("per-clip cap enforced",
      all(sum(1 for r in extracted if r["source_video"] == v) <= 3
          for v in {r["source_video"] for r in extracted}),
      f"{len(extracted)} extracted total")
check("extracted files exist on disk",
      all(Path(r["extracted_path"]).exists() for r in extracted))

for r in extracted:
    im = Image.open(r["extracted_path"])
    if im.size != (W, H):
        check("extracted stills are NATIVE resolution", False,
              f"{Path(r['extracted_path']).name} is {im.size}, want {(W, H)}")
        break
else:
    check("extracted stills are NATIVE resolution", True,
          f"all {len(extracted)} are {W}x{H} (scoring proxy was {ps.SCORING_SIZE}px)")

# colour check proves the seek landed in the correct segment
print("  seek accuracy (colour of extracted frame vs its segment):")
acc_ok = True
for r in sorted(extracted, key=lambda r: (r["source_video"], r["timestamp_s"])):
    if r["source_video"] != str(MULTI):
        continue
    ts = r["timestamp_s"]
    seg = int(ts // SEG)
    if abs(ts - seg * SEG) < 0.4 or seg >= len(SEGMENTS):
        continue  # too close to a cut to assert on
    im = Image.open(r["extracted_path"]).convert("RGB")
    px = np.asarray(im.resize((32, 18)))[2, 2]  # top-left corner = background
    want = np.array(EXPECTED_RGB[seg])
    dist = float(np.abs(px.astype(int) - want).max())
    good = dist < 40
    acc_ok &= good
    print(f"    t={ts:5.1f}s seg{seg}  got RGB{tuple(int(v) for v in px)} "
          f"want RGB{tuple(want)}  {'ok' if good else 'MISMATCH'}")
check("extracted frames come from the right timestamp", acc_ok)

# --- report ------------------------------------------------------------------
print("\n=== report ===")
h = (out / "report.html").read_text(encoding="utf-8")
check("video badge rendered", 'class="badge VIDEO' in h)
check("kind filter present", 'id="kind"' in h)
check("view button present on frames", 'data-view="1"' in h)
check("open-folder link present", ">open folder<" in h)
check("extracted-still link present", ">extracted still<" in h)
check("video frames get previews", 'data-preview="previews/' in h)
check("photo cards still render", 'data-kind="photo"' in h)
check("no unreplaced placeholders", "__CARDS__" not in h and "__STATS__" not in h)

import csv as _csv
with open(out / "report.csv", encoding="utf-8-sig") as fh:
    hdr = next(_csv.reader(fh))
check("csv exposes video columns",
      {"source_type", "timestamp_s", "extracted_path", "source_video"} <= set(hdr))

# --- resume + --no-video -----------------------------------------------------
print("\n=== flags ===")
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ps.main(["--root", str(TMP), "--video-every", str(EVERY)])
check("re-run rescans nothing", "0 to do" in buf.getvalue(),
      [l for l in buf.getvalue().splitlines() if "to do" in l][0].strip())

buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    ps.main(["--root", str(TMP), "--no-video", "--force", "--limit", "3"])
o = buf2.getvalue()
check("--no-video hides clips from the walk", "Found 3 images and 0 videos" in o,
      [l for l in o.splitlines() if "Found" in l][0].strip())

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
