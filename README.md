# Photo Scout

Photo Scout creates a report in a photo gallery style page of your highest-rated
stills, based on technical quality, aesthetics, and configurable subject matter
preferences (for example: desert or night sky). It's intended as a quality and
subject assessment and scoring application for a large photo or video library to
quickly narrow down top-rated stills. The photo gallery report results can also be
uploaded to any Ghost CMS implementation that permits API access.

Everything runs on your own machine — no API calls, no per-image cost, nothing
uploaded.

Point it at a library, walk away, come back to a single report: every photograph
scored, near-duplicate frames collapsed, one line of plain-language feedback each,
and a link straight into your file manager. Scoring is resumable — stop it with
Ctrl+C and the same command picks up where it left off.

Photos and video are handled in the **same pass** — clips get sampled, scored by the
same models, and the best moments exported as full-resolution stills. See section 8.

**Your library is treated as strictly read-only.** Originals are opened for reading
only, and nothing is created, changed or deleted anywhere inside it — not scores,
not thumbnails, not previews. Every byte the script writes goes to its output
directory, which defaults to `_photo_scout` beside the script. Point `--out` inside
the library and the script refuses to run. There is a test whose entire job is to
prove this: it fingerprints every file in a test library before and after a full run
and asserts byte-for-byte identity.

Released under the **GNU General Public License v3.0 or later** — see
[Licence](#13-licence).

### Paths in this document

Examples use POSIX paths. On Windows, write them the Windows way:

```
/path/to/photos      ->  "D:\Photos"
./photo-scout        ->  "C:\Users\you\photo-scout"
```

Quote any path containing spaces, on every platform.

### Contents

| | |
|---|---|
| [1. What it actually measures](#1-what-it-actually-measures) | the three scoring axes |
| [2. The other things the script handles](#2-the-other-things-the-script-handles) | RAW, dedup, resume, the size floor |
| [3. Install](#3-install) | requirements and setup |
| [4. Use](#4-use) | commands and options |
| [5. Output](#5-output) | the report and what else it writes |
| [6. Honest limitations](#6-honest-limitations) | what this cannot do |
| [7. Calibrating to your own library](#7-calibrating-to-your-own-library) | why scores need fitting |
| [8. Video](#8-video) | clips in the same pass |
| [9. Tagging](#9-tagging) | labelling in the browser |
| [10. Hiding photographs](#10-hiding-photographs) | keeping work out of reports |
| [11. Publishing to the web](#11-publishing-to-the-web) | the optional companion scripts |
| [12. Troubleshooting](#12-troubleshooting) | errors whose cause is far from the symptom |
| [13. Licence](#13-licence) | GPL-3.0-or-later |
| [14. Contributing](#14-contributing) | tests, style, how to help |

---

## 1. What it actually measures

There is no model that knows which of your photographs is the good one. What exists
are models that measure *components* of that judgement, and the script combines three
of them.

### Axis 1 — Aesthetic (weight 60%)

**LAION-Aesthetic V2.** A small neural network trained on hundreds of thousands of
human ratings of "how beautiful is this image." It sits on top of CLIP: CLIP turns
the photo into a 768-number summary of its content and style, and the aesthetic
head maps that summary to a score of roughly 1–10.

This is the closest thing to a proxy for "would someone hang this on a wall."

### Axis 2 — Technical (weight 25%)

**NIMA** (Neural Image Assessment, from Google Research), which was trained to
predict technical photographic quality — exposure, noise, tonal handling —
separately from subject appeal.

Plus two cheap arithmetic checks that catch things NIMA is soft on:

- **Sharpness**: variance of the Laplacian. A sharp photo has high edge energy; a
  motion-blurred or missed-focus one is smooth and scores near zero. Below the
  floor, the photo takes an 18-point penalty and the report says so.
- **Clipping**: what fraction of pixels are pure white or pure black. Blown
  highlights are the single most common reason a landscape can't be printed large.

### Axis 3 — Subject match (weight 15%)

This is the part you tune to *your own subject matter*, and it's why the script does
something beyond generic aesthetic scoring.

CLIP can compare an image against a text description and say how well they match.
The script defines three sets of descriptions:

- **Primary** — what you are actually trying to surface. The shipped list targets
  western landscape work: red rock canyons, golden-hour peaks, wide vistas, desert
  terrain, alpine lakes, salt flats, night sky, storm light. **Replace these with
  descriptions of your own subject matter** — it is the single highest-leverage
  change you can make.
- **Secondary** — worth flagging when strong, but not the main target: architecture,
  environmental portraits, wildlife, botanical macro, nature detail, high-contrast
  black and white, abstract texture, roadside americana. Discounted to 80% so the
  primary subject still floats to the top, while a genuinely strong outlier surfaces
  anyway.
- **Distractors** — things that should *not* surface: accidental frames, casual
  snapshots, test shots, underexposed frames, documents, food.

The subject score is how much of CLIP's confidence lands on target subjects versus
distractors. A gorgeous, technically perfect photo of a parking lot scores high on
axes 1 and 2 and gets pulled down here — which is correct.

**Why this axis only carries 15%.** CLIP recognises almost any competent example of
a target subject as on-subject, so on a focused library the subject score saturates —
on the library this was developed against, median 96.6 and 75th percentile 99.7. At a heavier weight it would add nearly the same constant to every
photograph — good at pushing snapshots and test frames down, useless at separating
one strong landscape from another. The weight sits on the axis that actually varies.
Raise it toward 0.30 if your library is mixed enough that "is this even the right
subject?" is a live question — a general archive rather than a focused body of work.

The report also records *which* description matched, so you can filter for "show me
everything that read as desert terrain."

### Composite

```
score = 0.60 x aesthetic + 0.25 x technical + 0.15 x subject
        - penalties for blur / blown highlights / crushed blacks
```

**Resolution is not in that formula, on purpose.** See [Resolution is reported,
never scored](#resolution-is-reported-never-scored) below.

Then banded into `TOP PICK` (78+), `STRONG` (66+), `MAYBE` (54+), `PASS`.

**Those defaults are placeholders, and the script replaces them automatically.**
At the end of a scoring run it fits the scale to your own library (section 7).
Left uncalibrated they squash scores into the middle and starve the top band.

All weights, thresholds, band cutoffs, and prompt lists are constants in a single
CONFIGURATION block at the top of the script; nothing is baked in. After changing a
weight or a band, `--recompute` rebuilds every score from cache in seconds. Only
changes to what the models actually see — the subject prompts, `SCORING_SIZE` —
need a full re-run with `--force`.

---

## 2. The other things the script handles

**RAW decoding, fast.** Every NEF contains a full-size JPEG preview that the camera
generated. The script extracts that instead of demosaicing the Bayer sensor data —
about 40x faster, and irrelevant to the outcome since everything gets resized to
512px for scoring anyway. It falls back to a half-resolution demosaic if a preview
is missing or too small. EXIF orientation is applied so portrait shots aren't
scored sideways.

**Near-duplicate collapsing.** Most libraries contain long runs — `DSC_0049` through
`DSC_0081`, bracketed and burst frames of one composition. The script computes a 64-bit perceptual hash (a "difference hash": resize to 9x8 greyscale,
emit one bit per horizontal brightness comparison), groups anything within 5 bits of
another, and keeps only the highest-scoring frame as the group's representative.

This correctly handles `DSC_0631.NEF` / `DSC_0631 (2).NEF` pairs, which are common
after a card recovery or a careless import: different timestamps and file sizes mean
they may well be *different* photographs. Filename-based dedup would wrongly merge
them; content-based hashing decides on what the pixels actually show.

Duplicates are flagged, never deleted or hidden permanently — there's a checkbox in
the report to show them.

**Resume.** Every result is written to a SQLite database as it's computed, committed
every 25 images. Press Ctrl+C whenever; re-running the same command picks up where
it stopped and skips everything already scored. If you edit a photo, its
modification time changes and it gets re-scored automatically.

**One master report across all folders** — regenerated from the database every run,
so it always reflects everything scored so far.

**Supported formats.** The walker picks up these, and ignores everything else:

| | |
|---|---|
| RAW | `.nef` `.nrw` `.cr2` `.cr3` `.arw` `.dng` `.raf` `.orf` `.rw2` `.pef` `.srw` |
| Standard | `.jpg` `.jpeg` `.png` `.tif` `.tiff` `.webp` `.heic` |
| Video | `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.mts` `.m2ts` `.wmv` `.mpg` `.mpeg` |

The lists are constants at the top of `photo_scout.py`; add an extension and it is
picked up on the next run. `.psd` is deliberately absent — a layered composite is
finished work, not a candidate for triage.

### A practical size floor

**Images whose shorter side is under 500 pixels are not scored at all.** A real
library accumulates things that are not photographs: app icons, emoji, sprite
sheets, saved memes, reaction GIFs, downloaded web thumbnails. Scoring them wastes
time and puts junk in the report, and no aesthetic model will reliably tell you a
meme is not a landscape.

The test is dimensional rather than by file type, because a JPEG can be either:

```bash
python photo_scout.py --root /path/to/photos --min-edge 800   # stricter
python photo_scout.py --root /path/to/photos --min-edge 0     # score everything
```

- **The shorter side is what counts**, because it is the dimension that survives
  cropping. A genuine 3:1 panorama at 6000 × 2000 sails through; a 480 × 360 meme
  does not. Measuring the longer side would let most memes in.
- **500 sits in the empty gap between the two populations.** The smallest export
  any camera or phone produces is around 1024 on the long edge, so no real
  photograph comes near it.
- **Video is judged on the clip's own resolution**, since an extracted still comes
  out at native size. An SD clip is skipped whole.
- **RAW files are never checked.** Reading dimensions out of a NEF costs more than
  the check saves, and no camera has written one anywhere near the floor.
- The run prints how many it skipped and reminds you of `--min-edge 0`.

Change the floor and it takes effect on the next run, in both directions: raising it
drops those photographs from the database and the report, lowering it scores them
again. `MIN_IMAGE_EDGE` at the top of the script sets the default.

### Resolution is reported, never scored

This is the **only** place pixel count decides anything. Above the floor, how many
megapixels a file has never moves its score.

That takes deliberate work, because two of the measurements leak resolution if you
let them. NIMA is handed pixels directly and its output shifts with input size, so a
45 MP body would quietly out-score a 12 MP one on identical scenes. Laplacian
variance climbs with pixel count by construction, reporting "sharper" when it means
"bigger". So every image is normalised to one fixed canvas — longest side 512px —
before anything measures it. CLIP resizes to 224 internally and was always immune.

The reasoning: resolution is a fact about the *file*, not a quality of the
*photograph*. Whether 2.1 MP is enough depends on the licence you have in mind, and
that is your call, not a model's. So the dimensions are put in front of you instead —
on every card, in the lightbox, and as `width`, `height` and `megapixels` columns in
the CSV — while the models go on judging what they are actually good at: composition,
light, focus, exposure, subject.

Dimensions are read during scoring, so an existing database shows blanks in that
spot until you rescore with `--force`. Nothing is removed in the meantime: a row with
no recorded size counts as unknown, and unknown is never treated as too small.

**Hiding photographs you don't want scored or shown.** Name a folder
`hide_from_photo_scout` anywhere in the library and everything inside it — at any
depth — is left out. See §10 below.

---

## 3. Install

**Requirements**

| | |
|---|---|
| Python | 3.10 or newer |
| Platforms | Linux, macOS, Windows — all tested paths, no platform-specific code |
| GPU | Optional. NVIDIA CUDA is used when present; CPU works, roughly 6x slower |
| Disk | ~2 GB for the models, plus ~300 KB per photograph for thumbnails and previews |
| Network | First run only, to download the models |

> **Put the virtual environment OUTSIDE your photo library.** Several gigabytes of
> packages landing inside a folder that is probably backed up or cloud-synced, plus
> dozens of package icons and test images sitting among your photographs. The script
> detects and skips a virtual environment it finds inside the library, but keeping
> them apart is much cleaner.

```bash
git clone https://github.com/<you>/photo-scout.git
cd photo-scout

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# PyTorch first. On a CUDA machine the --index-url is essential: a plain
# `pip install torch` silently gives you the CPU-only build. Get the exact
# command for your machine from https://pytorch.org/get-started/locally/
pip install torch --index-url https://download.pytorch.org/whl/cu128

# CPU-only machines, including Apple Silicon, just need:
# pip install torch

pip install -r requirements.txt
```

`requirements.txt` covers numpy, pillow, rawpy and transformers. `pyiqa` supplies
the NIMA technical model and is optional — if it will not install, pass `--no-nima`
and the script degrades gracefully to two axes.

On Windows, prefer python.org over the Microsoft Store build: the Store version is
sandboxed and causes odd file-permission behaviour with large libraries.

Verify your install — this reports the truth and never throws:

```bash
python -c "import torch; print(torch.__version__, '| built for CUDA:', torch.version.cuda, '| GPU usable:', torch.cuda.is_available())"
```

- `2.12.1+cu130 | built for CUDA: 13.0 | GPU usable: True` — you're set.
- `2.13.0+cpu | built for CUDA: None | GPU usable: False` — CPU-only wheel.

Do **not** diagnose with `torch.cuda.get_device_name(0)`. On a CPU-only build it
raises `AssertionError: Torch not compiled with CUDA enabled`, which reads like a
hardware fault but only means the wrong wheel is installed.

### The Windows CUDA-wheel trap

Skip this unless you are on Windows with an NVIDIA card.

**On Windows, the newest PyTorch release usually has no CUDA build yet.** The
CPU wheels ship first and the `win_amd64` CUDA wheels follow weeks later. So a
plain `pip install torch` gives you the newest version — CPU-only — and then
asking for a CUDA index at that same version finds nothing.

The fix is to let the CUDA index choose the version, and never pin to latest:

```powershell
pip uninstall -y torch
pip cache purge
pip install torch --index-url https://download.pytorch.org/whl/cu130
```


Because the index only contains CUDA builds, pip resolves to the newest version
that actually has a Windows CUDA wheel for your Python — which will be an older
release than PyPI's latest, and that is correct and expected.

Confirm the CUDA version to request by running `nvidia-smi` first: the "CUDA
Version" in its header is the highest your driver supports, so pick an index at or
below it (`cu126`, `cu128`, `cu130`, …). PyTorch bundles its own CUDA runtime, so
you do **not** need the CUDA Toolkit installed — only a current NVIDIA driver.

If `nvidia-smi` isn't recognised at all, there's no NVIDIA driver present, and CPU
is the right answer — see the note below.

The script runs fine on CPU, just slower — roughly 1–2 hours for 3,500 photographs
instead of 10–20 minutes. If CUDA turns into a fight, `--device cpu` is a perfectly
reasonable way to get your first results today.

First run downloads CLIP ViT-L/14 (~1.7 GB) and the LAION head (~15 MB) once, then
caches them. Budget a few minutes and a working connection for that first run only;
everything after is fully offline.

---

## 4. Use

Run from the project directory with the virtual environment active. Output lands in
`_photo_scout/` beside the script; the library is only ever read.

Start with one folder, so you can sanity-check the results before committing to a
full run:

```bash
python photo_scout.py --root /path/to/photos --folder "2011-06-28 Yellowstone"
```

Open `_photo_scout/report.html`. Look at what it called a TOP PICK and
what it called a PASS. If the balance is wrong, adjust the weights or the prompt
lists at the top of the script and re-run with `--force`.

When you're happy with the calibration, turn it loose on everything:

```bash
python photo_scout.py --root /path/to/photos
```

Ctrl+C any time. The same command resumes.

### Options

| Flag | Effect |
|---|---|
| `--folder "NAME"` | Score only that subfolder — **scoping applies to scoring, not to the report** (see below) |
| `--limit 50` | Stop after 50 new images — good for a quick calibration pass |
| `--force` | Re-score even if cached (use after changing weights or prompts) |
| `--reset` | Delete all previous results and start over (see below) |
| `--yes` | Skip the `--reset` confirmation |
| `--report-only` | Rebuild the reports from the database, score nothing |
| `--calibrate` | Force a re-fit of the score scale now (normally automatic — see section 7) |
| `--no-calibrate` | Don't calibrate automatically after scoring |
| `--recompute` | Rebuild scores from cached model outputs after editing weights or bands |
| `--no-previews` | Skip the large JPEGs the lightbox displays (saves ~900 MB) |
| `--no-nima` | Skip the NIMA model if it won't install |
| `--no-thumbs` | Skip contact-sheet thumbnails (smaller output, less useful report) |
| `--min-edge 800` | Raise the size floor (default 500px on the shorter side); `0` scores everything |
| `--no-dedup` | Don't group near-duplicates |
| `--no-video` | Stills only; ignore video files (see section 8) |
| `--video-every 1.5` | Sample video frames every 1.5s instead of 3.0 |
| `--no-extract` | Score video frames but don't export full-resolution stills |
| `--device cpu` | Force CPU |
| `--verbose` | Full tracebacks when an image fails |

### Starting completely over

```bash
python photo_scout.py --root /path/to/photos --reset
```

It prints what will go — how many scored results, thumbnails, previews and
extracted stills, and how many megabytes — then waits for you to type `yes`. Add
`--yes` to skip the prompt in a script. After deleting, it immediately begins a
fresh scoring run, so this one command is the whole clean-slate workflow.

Only the `_photo_scout` directory is removed. The command refuses to run if the
target isn't a directory named exactly `_photo_scout` sitting directly inside
`--root`, so a mistyped path can't turn it into a recursive delete of something
else. Your photographs and video live outside that directory and are never touched.

A reset also discards `calibration.json`, but you don't need to do anything about
it — the run that follows re-fits the scale automatically.

**You usually don't need a full reset.** Cheaper options:

| Situation | Command |
|---|---|
| Changed weights or band cutoffs | `--recompute` (seconds, no models) |
| Bands feel wrong for your library | `--calibrate` (seconds, no models) |
| Changed the subject prompts or `SCORING_SIZE` | `--force` — re-scores, keeps the database |
| One folder needs redoing | `--force --folder "NAME"` |
| Genuinely want a clean slate | `--reset` |

`--force` re-scores and overwrites in place, which is almost always what you want.
A true `--reset` is only necessary when you want stale rows gone too — for example
after moving or deleting a lot of files, whose entries would otherwise linger in
the report.

### `--folder` scopes scoring, not the report

This trips people up. `--folder` controls **which photos get scored on this run**.
The report is always built from the *entire* database — one master report across the
whole library — so it will show folders you scored days ago alongside the one you
just processed. Those aren't new results; they're
history.

To review a single folder, use the **folder dropdown** in the report itself. It
lists every top-level folder with its image count, and picking one folds in that
folder's subfolders too — choosing a shoot folder also shows anything filed
underneath it. The header shows a live count of what's
currently visible, and the lightbox pages only through the filtered set.

Folder, verdict, photo/video and text filters all compose, so "TOP PICK in this one
folder" is two clicks.

### Expected runtime

Roughly **10–20 minutes for 3,500 photographs** on a mid-range NVIDIA GPU, dominated
by RAW preview extraction rather than by the models. On CPU, closer to **1–2 hours**.
Either way it is one-time, free, and resumable.

---

## 5. Output

Everything lands in `_photo_scout/` beside the script (override with `--out`):

- **`report.html`** — the master report. A dark contact sheet of every photo with
  its thumbnail, verdict badge, score and one-sentence feedback. Click a thumbnail
  to open it full-window in the lightbox. Filter buttons for TOP PICK / STRONG /
  MAYBE / PASS, a checkbox to reveal near-duplicates, and a search box for folder
  or filename. Under each thumbnail the folder takes one line and the capture date,
  time and pixel dimensions the next — dimensions because resolution is deliberately
  excluded from the score, so the judgement is yours, and on a separate line because
  a long folder name would otherwise crowd them off. Only the folder is ever
  shortened, with the full name on hover. All client-side — just double-click it.
- **`shortlist.csv`** — only the TOP PICK and STRONG keepers. This is the file to
  actually work from.
- **`report.csv`** — everything, all columns, for Excel or Lightroom import.
- **`scores.sqlite3`** — the resume database. Query it directly if you want.
- **`thumbs/`** — the 400px thumbnails in the report grid.
- **`previews/`** — the 1600px JPEGs the lightbox displays, rendered from the RAWs.
- **`extracted_stills/`** — full-resolution frames pulled out of video clips, one
  subfolder per clip. Only created if the library contains video (see section 8).
- **`calibration.json`** — the score scale fitted to this library. The file explains
  itself when you open it (see section 7).
- **`tags.json`** — your tags. Hand-authored, so `--reset` preserves it.

### On a phone or tablet

Both reports and the published page work the same way on a touchscreen:

- **The `−` and `+` buttons resize the grid.** Pinching zooms the whole page,
  which magnifies one column rather than showing more; these reflow it. Ten
  steps from 100px to 620px columns, remembered per page in the browser. A
  narrow screen starts two-up, and zooms out to three columns on a 390px phone —
  the gutter tightens below 600px, which is what makes the third column fit.
- **Swipe left or right in the lightbox** to move between photographs, or flick
  sideways with two fingers on a trackpad. Only a decisively sideways gesture
  counts — a mostly vertical one is someone scrolling — and one flick moves one
  photograph however much momentum it carries. In the local report both are off
  at 1:1, where dragging pans the photograph.
- **The toolbar stays at the top** as you scroll, so the filters, sort and search
  box stay reachable a thousand photographs down.
- **Closing the lightbox leaves you on the photograph you were looking at**, not
  the one you originally tapped.
- On the published page, the lightbox has **its own heart** showing that
  photograph's tally. Liking from either place updates both, and the sort key
  behind "Most liked" with it.

### The lightbox

Click any thumbnail — or its **view** link — and the photo opens full-window in an
overlay. No server, no ports, nothing running in the background: just double-click
`report.html`.

Because a browser cannot decode a NEF, the overlay displays a 1600px JPEG that the
script renders from the RAW while scoring, stored in the output directory's `previews/`.

| Control | |
|---|---|
| click thumbnail, or **view** | open the lightbox |
| `←` `→` or the side arrows | previous / next photo |
| `F` or **Full screen** | true full screen |
| `Z`, **1:1**, or click the image | toggle fit-to-window and actual pixels |
| `Esc`, **×**, or click the backdrop | close |
| **copy path** | the file's full path to the clipboard |
| **open folder** | the containing folder, as a `file:///` link |

Paging respects your current filter. Show only TOP PICK and the arrows walk just
those, so you can review a shortlist without the rest getting in the way. The next
and previous images preload, so paging is instant.

The counter in the top-left reads `4/57` — position within whatever is currently
filtered.

**On opening the RAW itself:** the **open folder** link is a plain `file:///` link,
which in most browsers shows the folder's contents. Browsers deliberately refuse to
launch a desktop application from a link, so nothing in an HTML file can open a NEF
directly in your RAW editor — that boundary cannot be crossed from a web page. Use
**copy path** and paste it into your file manager when you want the real file.

Previews cost roughly 250 KB each — about 900 MB across 3,500 photographs. Lower
`PREVIEW_SIZE` to 1200 to roughly halve that, or pass `--no-previews` to skip them
(the report still works; the overlay will say there's nothing to show).

Under each thumbnail is one short line — the two axis scores and what the subject
matcher saw:

> Aesthetic 81 · Technical 74 · Golden-hour mountain peaks

If the photograph trips one of the quality checks, that is appended and coloured,
since it is the only part of the line that is not on every card:

> Aesthetic 34 · Technical 12 · A test shot · **looks soft or out of focus**

The verdict, the composite score and a video frame's timestamp are deliberately
absent: they are already on the card, in the badge, the number beside it and the
VIDEO chip. Restating them made every card read like every other one.

---

## 6. Honest limitations

Everything this tool cannot do, gets wrong, or does only approximately lives in
one place: **[LIMITATIONS.md](LIMITATIONS.md)**. Read it before you rely on the
output.

The two that matter most:

- **These are ranking tools, not judges.** LAION-Aesthetic reflects the average
  taste of its raters and under-rates subtle, minimalist and high-key work — in
  fine-art landscape, often the strongest work in the folder. Use the shortlist to cut
  thousands down to a few hundred worth your attention, then trust your own eye.
  Do not delete anything on the strength of a `PASS`.
- **The subject matcher only knows the prompts you give it.** If your own work
  keeps landing in the secondary tier, add prompts describing your actual
  photographs to `PRIMARY_PROMPTS`. This is the highest-leverage knob in the
  script, more so than the weights.

---

## 7. Calibrating to your own library

**This happens by itself.** At the end of any scoring run the script fits the
score scale to your library — you don't have to run anything. It loads no models
and takes seconds.

It re-fits when there's no calibration yet, or when the number of scored photos
has moved by more than 10% since the last fit. Small additions leave it alone;
scoring another 40 folders re-fits it. The log says which happened.

```bash
python photo_scout.py --root /path/to/photos --calibrate      # force a re-fit now
python photo_scout.py --root /path/to/photos --no-calibrate   # leave the defaults alone
```

### Why it's needed

`AESTHETIC_RANGE` assumes LAION-Aesthetic values spread from 4.0 to 7.5. On a real
personal library they don't — a first run here produced a range of **3.88 to 6.03**,
with the median at 5.02. Because the top of the assumed range is never approached,
the best photograph in the folder mapped to about 58/100 instead of near 100, every
score got squashed into the middle, and nothing could reach TOP PICK no matter how
good it was.

`--calibrate` reads the raw model outputs already stored in the database, stretches
the observed 2nd–99th percentile across 0–100, and then sets the band cutoffs by
percentile of the result: **top 5% TOP PICK, next 15% STRONG, next 30% MAYBE,
bottom half PASS**. It writes `calibration.json` and every later run
honours it, so newly scored folders are judged on the same scale.

Change the proportions by editing `BAND_QUANTILES` in the script and re-running
`--calibrate`. Delete `calibration.json` to return to the defaults.

Calibration **rescales, it does not reorder** — your best photograph is the same
photograph before and after. What changes is where the lines fall.

### Retuning without re-scoring

Every raw model output is stored, so the composite, verdict and feedback sentence
can be rebuilt from cache. After editing `WEIGHT_AESTHETIC`, `WEIGHT_TECHNICAL`,
`WEIGHT_SUBJECT`, `VERDICT_BANDS` or the penalties:

```bash
python photo_scout.py --root /path/to/photos --recompute
```

Seconds, no models loaded, no GPU needed. Only changes that affect what the *models
see* — the subject prompt lists, `SCORING_SIZE` — require a real re-run with
`--force`.

### One thing calibration can't fix

`subject_score` saturates. In that first run its median was 96.6 and its 75th
percentile 99.7, because the CLIP softmax uses a sharp temperature that makes the
subject match nearly binary. For anything recognisably a landscape it pins near
100, so that 30% of the composite is close to a constant and contributes little
discrimination *between* your good photographs — though it still does real work
pushing snapshots and test frames down.

This is why `WEIGHT_SUBJECT` ships at 0.15 rather than 0.30, with the difference
moved onto `WEIGHT_AESTHETIC`. On 400 photos shaped like your library that widened
the score spread (sd 13.1 → 14.4) and roughly doubled how closely the final score
tracks aesthetic quality (r 0.72 → 0.90), while still pushing off-subject frames
about 11 points below everything else.

If you want to compare, set them back to 0.45/0.25/0.30 and run `--recompute` —
it's instant and reversible.

---

## 8. Video

Video is built into the script — not a separate command, not a separate pass. The
walker picks up `.mp4 .mov .m4v .avi .mkv .mts .m2ts .wmv .mpg .mpeg` alongside
the stills, and each clip is turned into sampled frames that are scored by exactly the
same three models. One command covers the whole library:

```bash
python photo_scout.py --root /path/to/photos
```

`ffmpeg` is invoked as a subprocess from inside Python — you never type an ffmpeg
command yourself. If ffmpeg isn't on your PATH the script says so and processes
stills only, rather than failing.

### Install ffmpeg

```bash
sudo apt install ffmpeg          # Debian / Ubuntu
brew install ffmpeg              # macOS
winget install Gyan.FFmpeg       # Windows - then reopen your terminal for PATH
```

Check both, since the script uses ffprobe to read clip durations:

```bash
ffmpeg -version
ffprobe -version
```

### How a clip becomes stills

1. **Sample.** One frame every 3 seconds is pulled out in a single decode pass,
   downscaled to 512px. These are throwaway scoring proxies.
2. **Score.** Each frame goes through aesthetic + technical + subject scoring
   exactly like a photograph, minus a flat 6-point handicap (`VIDEO_FRAME_PENALTY`).
   Video frames are 8-bit, chroma-subsampled, inter-frame compressed, and often
   carry motion blur from a shutter angle chosen for motion rather than stills —
   they are genuinely weaker source material, and the handicap stops them crowding
   real photographs out of the shortlist.
3. **Dedup.** Frames go through the same perceptual-hash grouping as photos. A
   locked-off tripod shot collapses to its single best frame instead of forty
   copies of one composition. This is why the sampling interval can be generous.
4. **Extract.** *Surviving* frames scoring 66+ get re-extracted from the source
   clip at **full native resolution** as real PNG files, written to the output
   directory's `extracted_stills/<clip name>/`. Capped at 12 per clip so one
   good sequence can't flood the output; the script logs it when the cap bites.

The seek for step 4 is two-stage — a fast keyframe jump to two seconds before the
target, then an accurate decode forward. Input-only seeking is fast but lands on
the wrong frame; output-only seeking is exact but decodes the clip from zero. The
hybrid is both.

### Video options

| Flag | Effect |
|---|---|
| `--no-video` | Stills only; ignore clips entirely |
| `--video-every 1.5` | Sample more densely (default 3.0 seconds) |
| `--no-extract` | Score frames but skip full-resolution export |

Tuning constants at the top of the script: `VIDEO_FRAME_PENALTY`,
`VIDEO_EXTRACT_MIN_SCORE`, `VIDEO_EXTRACT_MAX_PER_VIDEO`, `VIDEO_EXTRACT_FORMAT`
(`png` for lossless, `jpg` to save disk), `VIDEO_MAX_SAMPLES`.

### In the report

Video frames appear in the same master report as the photographs, with a purple
`VIDEO 1:23` badge and the timestamp. There's a dropdown to show photos only, video
frames only, or both. Each frame card links to *open video*, *open folder*, and —
once extracted — *open extracted still* in green.

`report.csv` gains `source_type`, `timestamp_s`, `source_video`, and
`extracted_path` columns, so you can sort a spreadsheet by score and see exactly
which clip and which second every candidate came from.

### The honest limitation

Resolution. A 1080p frame is 2.1 megapixels — fine for web and licensing, marginal
for a print much beyond 8x10. 4K gives you 8.3 MP, which prints respectably. Against
even a 12 MP RAW file, video frames start at a real disadvantage that no scoring can
fix. Treat extracted stills as a bonus tier rather than as peers of the photographs.

The score will not tell you this, deliberately: pixel count is reported beside every
frame and kept out of every score (see [Resolution is reported, never
scored](#resolution-is-reported-never-scored)). Clips too small to be worth
considering at all are dropped whole by the size floor, and you judge the rest.

Video frames do carry a flat 6-point handicap, but it stands for something else
entirely: 8-bit depth instead of 12 or 14, chroma subsampling, inter-frame
compression, and motion blur from a shutter angle chosen to make movement look
smooth rather than to freeze an instant. Those are real deficits the models cannot
see in a downscaled proxy. It is flat rather than proportional because the deficit
belongs to the medium, not to the individual frame — a great frame and a poor frame
off the same clip share a codec. Set `VIDEO_FRAME_PENALTY` to `0` if you disagree,
and `--recompute` will rebuild every score in seconds.

Resumability works the same as for photos: a clip whose frames are already in the
database is skipped on re-runs, so you can Ctrl+C mid-library and continue later.


---

## 9. Tagging

Every card has a text box under it. Type a word or phrase and press **Enter** or
type a **comma** to turn it into a tag. Multi-word tags like `Lake Photos` work.
Clicking away commits whatever you were typing, so a half-finished tag isn't lost.

Each tag gets its own colour, derived from a hash of its text. The same tag is
therefore the same colour on every card, in the search box, and between sessions.

Only letters, digits, spaces, underscores and hyphens survive; everything else is
stripped as you type. That keeps tags safe to render — with no angle brackets or
quotes there is nothing that can be injected into the page. The same rule is
enforced again in Python when reading `tags.json`, since that file is editable.

### Searching by tag

Click the search box and a dropdown lists every tag in use, with a count of how
many photographs carry it. Typing filters that list live. Click a tag, or use the
arrow keys and Enter, and it becomes a coloured chip in the search box.

Multiple chips are **ORed** — a photo shows if it carries *any* of them. Picking
`Lake` and `Desert` gives you both sets, even when no single photograph has both
tags; each extra chip widens the results rather than narrowing them. Chips sort
alphabetically, each has an × to remove it, and Backspace in an empty search box
removes the last one. Free text still searches folder and filename as before, so
the box does both jobs.

If you ever want the opposite — narrowing to photos that carry *every* selected
tag — it's a one-line change in `apply()`: start `okT` at `true` and flip the
condition to `if (!cardTags.includes(...)) { okT = false; break; }`.

### Keeping your tags

Tags are stored in your browser as you work, so they survive a reload and a report
rebuild. Browser storage can be cleared, though, so when you've done a session of
tagging press **Save tags**. That downloads `tags.json`; move it into the output
directory (`_photo_scout/`) and the script bakes it into every future report. The button shows an asterisk while you have unsaved changes, and the
browser warns you if you try to close the page with tags unsaved.

`--reset` deliberately does **not** delete `tags.json` — tags are your work, not
generated output. The reset prompt tells you it's being preserved.

Tags also appear in a `tags` column in the CSV exports, semicolon-separated.

Both reports share the same `tags.json`, so a photo tagged in the full report is
tagged in the shortlist report too.

---

## 10. Hiding photographs

Some photographs should not be scored, shortlisted, or published — client work,
family pictures, anything that is not yours to license. Rather than moving them
out of the library, put them in a folder named exactly:

```
hide_from_photo_scout
```

Anywhere in the library, at any depth. Everything inside it, including its
subfolders, is left out of:

- the scoring pass — those files are never even opened
- `report.html`, `report.csv` and `shortlist.csv`
- the shortlist report from `photo_scout_strong_top.py`
- anything published to Ghost by `photo_scout_ghost.py`

Two ways to use it:

```
/path/to/photos/2011-06-28 Yellowstone/hide_from_photo_scout/   <- move photos in here
/path/to/photos/2019 Client Work/   ->   renamed to   hide_from_photo_scout/
```

**Nothing is ever deleted or moved.** Your photographs stay exactly where you put
them. Hiding only affects what the reports show.

### It works on photographs you have already scored

You don't need to rescore anything. Hide a folder, run the script again, and those
photographs are gone from the report. Rename it back and they return, just as
quickly. Their scores stay in the database the whole time, so nothing is
recomputed either way.

The mechanism is worth knowing, because it explains one surprise. The database
records each photograph's path as it was when scored. Renaming a folder doesn't
change what's stored, so the script can't hide those rows by matching on the name
alone — instead it checks whether each file is still where it was scored. Anything
that isn't is left out of the report.

That means **photographs you have moved or deleted also drop out of the report**,
which is almost always what you want: a report shouldn't list pictures that aren't
in your library. Their rows stay in the database, so moving a folder back restores
them with no rescore.

### If the drive isn't plugged in

If the library cannot be reached at all — an external drive unplugged, a network
share unmounted — the existence check is skipped entirely and the report is built
from the database alone. You will see:

```
Note:      /path/to/photos is not reachable, so the report is built from the
           database alone - photographs that have moved or been hidden since
           the last scan may still appear.
```

Better to say so than to report that your entire library has vanished.

### What is and isn't matched

The name must be the whole folder name. Case doesn't matter, and stray spaces are
ignored, so `Hide_From_Photo_Scout` works. These do **not** hide anything:

| Name | Hidden? |
|---|---|
| `hide_from_photo_scout` | yes |
| `HIDE_FROM_PHOTO_SCOUT` | yes |
| `hide_from_photo_scout/2019/raw` | yes, at any depth |
| `hide_from_photo_scout_backup` | no |
| `my_hide_from_photo_scout` | no |
| `hide_from_photo_scout.NEF` | no — that's a file, not a folder |

### Already published to Ghost?

Hiding removes photographs from the next publish, but the images already uploaded
to Ghost stay in its media library — Ghost keeps them, unreferenced. Republish
after hiding so the page stops showing them, and delete the stray images from
Ghost admin if that matters to you.

---

## 11. Publishing to the web

Two optional companions, neither required to use the scorer:

| Script | Purpose |
|---|---|
| `photo_scout_strong_top.py` | The same report, limited to the shortlist. Generated from `photo_scout.py`; edit the parent, not this. |
| `photo_scout_ghost.py` | Publishes the shortlist as a page on a [Ghost](https://ghost.org) site via its Admin API. |

The Ghost publisher uploads thumbnails and previews into Ghost's own media library,
keyed by a hash of each photograph's relative path, so re-running it never creates
duplicates and a full rescore re-uploads nothing that has not actually changed. The
published page carries the same searching, sorting and browser-side tagging as the
local report.

### Getting a Ghost Admin API key

In Ghost admin: **Settings → Advanced → Integrations → Add custom integration**.
Name it anything (`Photo Scout` is a reasonable choice). Ghost then shows you three
things — you want the **Admin API key**, the middle one.

Get the right one. The panel also shows a **Content API key**, which is a single
26-character hex string with no colon in it, and it will not work here: it is
read-only and cannot upload images or create pages. The Admin API key is **89
characters with exactly one colon**, in the form `<24-hex id>:<64-hex secret>`.
Copy the whole thing, including the colon and both halves — copying only the part
after the colon is the single most common mistake.

Check what you have before using it:

```bash
python -c "k='PASTE_HERE'; print(len(k), k.count(':'))"    # expect: 89 1
```

**Passing it on the command line**

Either flag or environment variable; the environment variable is the better habit,
since a `--key` on the command line lands in your shell history.

```bash
# bash / zsh - single quotes, so nothing in the key is interpreted
export GHOST_ADMIN_KEY='<id>:<secret>'
python photo_scout_ghost.py --site https://example.com --slug photo-scout

# or inline, for a one-off
python photo_scout_ghost.py --site https://example.com --key '<id>:<secret>'
```

**Windows PowerShell** uses different syntax for both, and this trips people up:

```powershell
# Set it for this terminal session. Note $env:, and DOUBLE quotes.
$env:GHOST_ADMIN_KEY = "<id>:<secret>"
python photo_scout_ghost.py --site https://example.com --slug photo-scout

# or inline
python photo_scout_ghost.py --site https://example.com --key "<id>:<secret>"
```

Three PowerShell specifics worth knowing:

- **`export` does not exist.** `export GHOST_ADMIN_KEY=...` is a bash-ism; PowerShell
  will not recognise it and the variable will simply never be set.
- **The variable lasts only as long as that terminal window.** Open a new one, or
  reboot, and you have to set it again. To make it permanent for your user account:
  `[Environment]::SetEnvironmentVariable("GHOST_ADMIN_KEY", "<id>:<secret>", "User")`
  — then close and reopen the terminal for it to take effect.
- **Line continuation is a backtick (`` ` ``), not a backslash.** Copying a
  multi-line `bash` example straight into PowerShell fails on the `\` at the end of
  each line. Put the whole command on one line, or swap each `\` for `` ` ``.

To confirm it is set in the current terminal, without printing the secret:

```bash
python -c "import os;k=os.environ.get('GHOST_ADMIN_KEY','');print('set' if k else 'NOT SET', len(k))"
```

**Try it without a key first.** `--dry-run` needs no credentials at all — it builds
the whole page locally so you can see exactly what would be published:

```bash
python photo_scout_ghost.py --site https://example.com --dry-run --emit-html preview.html
```

If the key is wrong you will get `Admin API key must look like <id>:<hex secret>`
before anything is uploaded — see [section 12](#12-troubleshooting).

### Fitting the gallery to your theme

The page is one self-contained block dropped into a normal Ghost page, so it
inherits whatever spacing your theme gives its content. Three flags adjust the fit:

| Flag | Effect |
|---|---|
| `--title "Best of 2011"` | Heading above the gallery. **Blank by default**, which also collapses the theme's heading band so the photographs start at the top of the page |
| `--title-size compact` | What to do with your theme's own heading band. `compact` trims its padding and brings the title down to a sensible size; `keep` leaves the theme untouched; `hide` removes the heading entirely. Defaults to `hide` when `--title` is blank, `compact` otherwise |
| `--gap 8` | Space above and below the gallery (default 8). Themes often set a large margin here — Ghost's own default is `max(12vmin, 64px)`, which is a visible hole on a tall screen. This replaces it. Negative values tuck the gallery up closer |
| `--max-width 1800` | How wide the grid may grow, in pixels |
| `--column-width 260` | Minimum column width; smaller means more columns |

A page still needs a name in Ghost's admin list, so when `--title` is blank it is
filed as **Photo Scout Gallery** — an empty title is not reliably accepted by the
API and can land as "(Untitled)". Nothing on the page displays it; the heading is
hidden. The run says which name it used, and `UNTITLED_GHOST_TITLE` at the top of
the script changes it. Pass `--title-size` explicitly if you want the stand-in
shown as a heading after all.

Both spacing flags reach slightly outside the gallery itself, so they are scoped
to pages that actually carry one: the script marks the document with a
`psc-host` class and its rules are written against that. No other page on your
site changes, and a theme whose heading uses class names the script does not
recognise simply keeps its own spacing.

`hearts/` is a small optional service that lets visitors "like" photographs on the
published page, with the tallies stored in SQLite beside your site rather than
anywhere else. It stores no accounts, no IP addresses and no raw visitor
identifiers, and the gallery is fully functional when it is switched off or
unreachable. `DEPLOY_hearts.md` walks through installing it; `SPEC_hearts.md`
documents the design and its trade-offs.

---

## 12. Troubleshooting

Failures whose cause is a long way from where the error appears.

### `RuntimeError: operator torchvision::nms does not exist`

Reported as a CLIP import failure, but it means **torch and torchvision are a
mismatched pair**. torchvision pins one exact torch version. Install both from the
same index in one command and let pip resolve them together:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

The script checks for this on startup and says so before it wastes your time.

### `AssertionError: Torch not compiled with CUDA enabled`

Reads like a hardware fault; it only means the CPU-only wheel is installed. Never
diagnose with `torch.cuda.get_device_name(0)` — it throws on a CPU build. Use this
instead, which always reports the truth:

```bash
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

Section 3 covers the Windows CUDA-wheel trap that usually causes it.

### CLIP imports, but every score looks like nonsense

`transformers` v5 changed CLIP's return type: `get_image_features()` returns a
`BaseModelOutputWithPooling` rather than a tensor. The script handles v4 and v5,
and validates that the embedding really is 768-wide — a wrong width would produce
confident nonsense instead of an error. If you have patched this area, keep that
check.

### Nothing reaches `TOP PICK`, and every score sits in the middle

The scale has not been calibrated. It needs at least **30 scored photographs**
before percentiles mean anything; below that it falls back to the shipped defaults,
which squash everything together. Score a few more folders, or force a fit:

```bash
python photo_scout.py --root /path/to/photos --calibrate
```

Also expect composites to top out around **70–75, not 100** — section 7 explains why.

### Package files being scored as photographs

A Python virtual environment inside the library. The walker detects `pyvenv.cfg`
and prunes the environment's subdirectories, but keeping the venv outside the
library is cleaner.

### `ERROR: Admin API key must look like <id>:<hex secret>`

The key reaching the script is not a Ghost Admin API key. Three causes, in
descending order of likelihood:

- **It's the Content API key.** That one is 26 hex characters with no colon, is
  read-only, and cannot upload or publish. Take the middle key in the integration
  panel, not the first.
- **Only the secret half was copied.** A 64-character string with no colon is the
  right key with its `id:` prefix missing. Copy the whole 89-character value.
- **A stray character came along.** Quotes, a trailing space, or a line break from
  a wrapped terminal. `python -c "k='PASTE'; print(len(k), k.count(':'))"` should
  report `89 1`.

A companion error, `The secret half of the Admin API key is not hex`, means the
colon is there but what follows it is not a 64-character hex string — usually a
truncated paste.

### `ERROR: an Admin API key is required`, but you just set one

The environment variable is not set **in the terminal you are running from**. It
does not travel between windows, and it does not survive a reboot. In PowerShell
note that `export` silently does nothing — the syntax is
`$env:GHOST_ADMIN_KEY = "<id>:<secret>"`. See
[section 11](#getting-a-ghost-admin-api-key) for making it permanent, and for
checking whether it is set without printing the secret.

### Uploads to Ghost fail with `error code: 1010`

That is **Cloudflare's Browser Integrity Check, not Ghost** — the request never
reached your site. The publisher sends a real User-Agent to avoid it. If it still
fires, add a WAF rule in the Cloudflare dashboard to skip Browser Integrity Check
for `/ghost/api/*`. The script prints the exact rule when it hits this, along with
guidance for 401, 403 and 413.

### The Ghost page publishes, but the images are broken

Check that `--site` matches your public domain. Image paths are stored
site-relative, so they resolve against whatever domain serves the page — which is
also why the API can live on a separate admin host.

### Heart buttons appear, but clicking one fails

The allowlist was not registered, because `HEARTS_ADMIN_TOKEN` was not set when you
published. Fix it without republishing:

```bash
HEARTS_ADMIN_TOKEN="<token>" python photo_scout_ghost.py \
  --site https://example.com --hearts-url /api/hearts --hearts-register-only
```

---

## 13. Licence

Copyright (C) 2026 Brian Salisbury and contributors.

Photo Scout is free software: you can redistribute it and/or modify it under the
terms of the **GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version** — `SPDX-License-Identifier: GPL-3.0-or-later`.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the [LICENSE](LICENSE) file for the full text, or
<https://www.gnu.org/licenses/>.

### The models are separately licensed

The GPL covers this project's own code. It does not cover the model weights the
script downloads on first run, which carry their own terms and are not distributed
here:

| Component | Where it comes from |
|---|---|
| CLIP ViT-L/14 | OpenAI, downloaded via Hugging Face |
| LAION-Aesthetic V2 predictor head | LAION |
| NIMA | via `pyiqa`, optional |

Check their licences yourself before using this commercially.

### And a word on the output

Scores are a triage aid, not a verdict on a photograph. They rank pictures against
each other on aesthetic and technical quality; they do not know what a photograph is
worth, and they are not a substitute for your own eye. Section 6 is honest about
what these models can and cannot see.

---

## 14. Contributing

Contributions are welcome — bug reports especially, since most of the interesting
faults in this project were found by someone running it against a real library.

Full detail — ground rules, how to run the tests, style — is in
[CONTRIBUTING.md](CONTRIBUTING.md). The essentials:

### Ground rules

**The photo library is read-only, always.** No feature may write, move or delete
anything inside `--root`. This is the one rule with no exceptions, and
`tests/_selftest_readonly.py` exists to prove it.

**Prove it, don't assert it.** Thirteen suites live in `tests/`; run them with
`for t in tests/_selftest*.py; do python "$t"; done`. Several real bugs here were
caught only because a test drove an actual browser rather than inspecting the
generated HTML. When you fix something, add the test that would have caught it, and
check it fails without your fix.

**`photo_scout_strong_top.py` is generated**, not hand-edited. `_make_variant.py`
derives it from `photo_scout.py`. Change the parent and re-run the generator.

**Comments explain why, not what.** The code is read far more often than it is
written, frequently by someone who is not a professional developer. A comment that
records the reasoning behind a non-obvious choice — or the bug that a line exists to
prevent — earns its place. One that restates the syntax does not.

### Style

Plain Python, no framework. Standard library wherever it is reasonable — the heart
service is Flask because that is genuinely simpler than hand-rolling one, but the
Ghost client mints its own JWTs rather than adding a dependency for thirty lines of
HMAC. Keep it that way unless a dependency really pays for itself.
