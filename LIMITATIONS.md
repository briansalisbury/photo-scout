# Limitations

What Photo Scout cannot do, gets wrong, or does only approximately. Read this
before deciding whether it fits your work.

Most of these are design decisions rather than bugs. Where a workaround exists it
is named. Bugs belong in the issue tracker.

---

## Scoring

**These are ranking tools, not judges.** No model knows which of your photographs
is the good one. The three axes measure *components* of that judgement and combine
them; the result is a triage aid for cutting thousands of photographs down to a few
hundred worth your attention. Use your own eye from there, and don't delete anything
on the strength of a `PASS`.

**LAION-Aesthetic reflects the average taste of its raters**, which skews toward
saturated, high-contrast, conventionally pretty images. It reliably under-rates
subtle, minimalist and high-key work — which in fine-art landscape is often the
strongest work in the folder. This is the single biggest caveat in the project.

**Subject score saturates.** On a focused library CLIP recognises almost
everything as on-subject, so that axis pins near 100 and adds roughly the same
constant to every photograph. It carries only 0.15 for that reason. It still does
real work pushing snapshots and test frames down; it just cannot separate one good
landscape from another. See README §7.

**Composite scores top out around 70–75, not 100.** A consequence of the above —
a photograph scoring 74 is not a near-miss, it is close to the ceiling.

**Scores are not comparable across libraries**, or across recalibrations of the
same one. Calibration fits the scale to whatever has been scored so far, so a
`TOP PICK` means "top 5% of this library", not an absolute standard.

**Fewer than 30 scored photographs and calibration will not run** — percentiles
across a handful of images are meaningless. Scores fall back to the shipped
defaults, which squash everything into the middle. Score a few folders first.

**Resolution is excluded from the score by design**, which cuts both ways. A
gorgeous 2 MP frame and a gorgeous 45 MP frame get the same number, so the report
will happily put a photograph at the top that is too small for the licence you had
in mind. The dimensions are printed on every card, in the lightbox and in the CSV
precisely because the tool is refusing to make that call for you. Sort the CSV by
`megapixels` if you need a hard cut-off.

**Feedback sentences are generated from the numbers**, not written by a model that
looked at the picture. They explain why a score came out as it did, which is what
triage needs, but they are templated rather than perceptive. For real written
critique, run a local vision-language model over the shortlist as a second pass.

---

## Near-duplicates

**Grouping is transitive.** In a long burst where frame 1 resembles frame 2 and
frame 2 resembles frame 3, all three group together even if 1 and 3 differ
noticeably. That is usually right for bracketed sets. If it over-merges, lower
`PHASH_HAMMING_THRESHOLD` from 5 toward 2.

Duplicates are flagged, never deleted — there is a checkbox in the report to
reveal them.

---

## Files and formats

**RAW scoring uses the camera's embedded JPEG preview**, not a neutral demosaic.
That is about 40x faster and irrelevant to ranking, but it means the score
reflects your camera's rendering — its colour science, contrast curve and sharpening.
Two bodies photographing the same scene will not score identically. Where a
preview is missing or too small, it falls back to a half-resolution demosaic.

**Formats are a fixed list** (see `RAW_EXTENSIONS`, `STD_EXTENSIONS` and
`VIDEO_EXTENSIONS` at the top of `photo_scout.py`). Notably `.psd` is **not**
included: a layered composite is finished work, not a candidate for triage.

**Capture dates and times only appear after a rescore.** They are read during
scoring, so an existing database shows blanks until `--force` or `--reset`. Pixel
dimensions behave the same way.

**The size floor is a blunt instrument.** Anything whose shorter side is under
`--min-edge` (500px by default) is not scored at all. It is aimed at icons, emoji,
memes and web thumbnails, and it will also catch legitimate work that happens to be
small: a heavily cropped frame, a scanned strip, a low-resolution archive from an
early digital camera. Nothing warns you which of the skipped files were which — the
count is all you get. Lower the floor, or pass `--min-edge 0`, if your library has
that kind of material in it.

**RAW files bypass the floor entirely.** Reading dimensions out of a NEF costs more
than the check saves on a run that walks the whole library, so RAW is admitted
unchecked and only caught after decoding. In practice no camera writes one near the
floor, but a tiny RAW would still cost a full decode before being dropped.

**A raised floor deletes rows.** Raising `--min-edge` removes the newly-disqualified
photographs from the database, not just from the report, because a skipped file is
never revisited and a stale row would otherwise haunt every future report. Lowering
it again rescores them. Contrast this with hidden folders, whose rows survive.

---

## Video

**Resolution is the ceiling, and the score will not mention it.** A 1080p frame is
2.1 megapixels — fine for web and licensing, marginal for a print beyond 8x10. 4K
gives 8.3 MP. Against even a 12 MP RAW file, video frames start at a disadvantage no
scoring can fix. Pixel count is deliberately kept out of every score, so a video
frame can and will outrank a RAW on the strength of the picture alone; the
dimensions beside it are what tell you whether that matters. Clips under
`--min-edge` are dropped whole.

**`VIDEO_FRAME_PENALTY` is a judgement call, not a measurement.** The flat 6 points
stand for 8-bit depth, chroma subsampling, inter-frame compression and motion blur —
real deficits that a downscaled proxy hides from the models. Nobody has calibrated
that number against licensing outcomes; it is sized to half a verdict band. Set it
to 0 and `--recompute` if you would rather judge frames on the picture alone.

**No ffmpeg, no video.** The script says so and processes stills only rather than
failing.

**Very short clips** — shorter than half the sampling interval — used to yield no
frames at all. There is now a midpoint fallback and a tail frame, but a two-second
clip still gives you very little to choose from.

---

## Reports

**Photographs that have moved or been deleted drop out of the report.** The
database records each file's path at scoring time, so the report checks whether
each file is still there. This is what makes hiding work on already-scored
photographs (README §10), and it means a report never lists pictures that are not
in your library. Their rows stay in the database, so moving a folder back restores
them with no rescore.

**If the library is unreachable** — an unplugged drive, an unmounted share — that
check is skipped entirely and the report is built from the database alone, with a
note saying so. Better than reporting that your entire library has vanished.

---

## Tagging

**Tags live in the browser**, in `localStorage`. They are per browser and per
device: tags added on a laptop do not appear on a phone, and clearing site data
loses anything not exported. Press **Save tags** to download `tags.json` and drop
it in the output directory to bake it into future reports.

This is deliberate — tags are one person's private annotations, and a database
would mean running a server.

---

## Hearts

Applies only if you deploy the optional heart service (`DEPLOY_hearts.md`).

**Identity is deliberately weak.** A visitor is a UUID in `localStorage`. Clearing
site data yields a fresh one and a second heart. That is the price of asking
nothing of visitors, and counts approximate *distinct browsers* rather than people.

**Counting is not tamper-proof.** A determined person with a script and a proxy
pool can inflate the numbers. Defending properly would mean CAPTCHAs or accounts,
which cost more in friction than the data is worth. What is stopped: accidental
double-counting, holding the button down, and casual gaming from one browser.

**Standard Caddy has no rate limiter** (the module needs a custom build), so the
per-IP layer described in `SPEC_hearts.md` §6.3 is absent on a Caddy deployment.
The per-voter cap inside the service — 60 writes an hour — still applies, and it
is the limit that actually catches a script, since it survives a change of address.
A CDN rate-limiting rule on `/api/hearts*` is the easy way to add the missing layer.

---

## Publishing to Ghost

**Ghost has no built-in way to remove uploaded images that are no longer
referenced**, and its Admin API is upload-only — no list, no delete
([API reference](https://docs.ghost.org/admin-api)). This has been an open request
for years ([forum thread](https://forum.ghost.org/t/remove-old-unused-images/1197)).

Republishing never orphans anything: filenames derive from a hash of each
photograph's relative path, so the same photograph always overwrites the same file,
and a full rescore re-uploads nothing whose content has not changed. Orphans appear
only when a photograph leaves the shortlist or is hidden.

Three ways to clear them, on a self-hosted install:

- **Delete from `content/images/` directly.** Every file this tool uploads is named
  `psc-<hash>-t.jpg` or `psc-<hash>-p.jpg`, so they are easy to isolate from
  anything Ghost or you uploaded by hand.
- **[`ghost-purge-images`](https://github.com/growblog/ghost-purge-images)**, a
  third-party CLI that scans posts and pages and purges the rest. Verify it against
  your Ghost version and run it in report mode first.
- **`publish.sqlite3`** records every upload this tool has made. Comparing it
  against the current shortlist gives the orphan set exactly, with no scanning and
  no false positives.

**Ghost(Pro) users have no filesystem access**, so the first option is unavailable
and the third is the practical route.

**The published page is a draft by default.** Pass `--status published`, or publish
it from Ghost admin.

**Near-duplicates are not published.** The Ghost page carries the shortlist with
duplicate groups already collapsed, unlike the local report where you can reveal
them.

---

## Project

**There is no CI.** The suites in `tests/` are run by hand before a release. See
`CONTRIBUTING.md`.

**Model weights are downloaded at first run and carry their own licences** — CLIP
from OpenAI, the LAION-Aesthetic head from LAION, NIMA via `pyiqa`. The GPL covers
this project's code, not those. Check their terms before commercial use.

**First run needs a network connection** for roughly 1.7 GB of model downloads.
Everything after that is fully offline.
