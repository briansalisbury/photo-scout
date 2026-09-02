#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brian Salisbury and contributors.
# Part of Photo Scout. This program comes with ABSOLUTELY NO WARRANTY.
# See the LICENSE file, or <https://www.gnu.org/licenses/>.
"""
photo_scout.py - Local, zero-cost quality scoring for a photo and video library.

Scores every photo on three independent axes, then blends them into one quality
score you can sort and cull by:

  1. AESTHETIC  - LAION-Aesthetic V2 (CLIP ViT-L/14 + linear head trained on
                  human aesthetic ratings). "Would a person call this beautiful?"
  2. TECHNICAL  - NIMA (Neural Image Assessment) via pyiqa, plus a Laplacian
                  sharpness check and highlight/shadow clipping check.
                  "Is this well executed and printable?"
  3. SUBJECT    - CLIP zero-shot match against YOUR subject matter described in
                  plain language (western landscape, desert, mountains, etc.),
                  with a secondary tier for other subjects worth surfacing and a
                  distractor tier that drags down snapshots/test frames.

Video is handled in the SAME pass as stills: clips are sampled with ffmpeg, each
frame is scored exactly like a photograph (minus a handicap for compression and
motion blur), near-identical frames collapse, and the survivors that score well
are re-extracted at full native resolution as real still files.

Resolution is reported, never scored. Pixel dimensions appear on every card and
in the CSV so you can judge whether a frame is big enough for the job, but they
never move a score: the models rank composition, light, focus and exposure, and
how many megapixels a file has is a fact about the file rather than a quality of
the photograph. The one thing resolution does decide is admission - anything
whose shorter side falls below --min-edge (500px by default) is not scored at
all, which keeps icons, emoji, memes and web thumbnails out of the report.

Everything runs on your machine. No API calls, no per-image cost.

THE PHOTO LIBRARY IS READ-ONLY. RAW files and video clips are opened for reading
only. Nothing is created, modified or deleted anywhere under --root: every byte
this script writes goes into its output directory, which defaults to a
'_photo_scout' folder beside the script and is refused outright if you point it
inside the library.

Usage
-----
  # First run against one folder, to sanity-check before committing to the library
  python photo_scout.py --root /path/to/photos --folder "2011-06-28 Yellowstone"

  # All output lands beside this script by default. Override with --out:
  python photo_scout.py --root /path/to/photos --out ./_photo_scout

  # Whole library, photos and video together (resumable - Ctrl+C any time)
  python photo_scout.py --root /path/to/photos

  # Stills only
  python photo_scout.py --root /path/to/photos --no-video

  # Rebuild the master report from what has already been scored, without scoring more
  python photo_scout.py --root /path/to/photos --report-only

  # Windows paths work the same way, quoted:  --root "D:\\Photos"

See README.md for install steps.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import importlib.metadata
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Iterable, Optional

import numpy as np
from PIL import Image, ImageFile

# Some RAW-embedded JPEGs are slightly truncated; don't let that kill a run.
ImageFile.LOAD_TRUNCATED_IMAGES = True
# These are your own large files, not untrusted uploads - lift the decompression guard.
Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------
# CONFIGURATION - edit this block to retune the pipeline for your own work.
# The shipped prompt lists target western landscape work; replacing them with
# descriptions of your own subject matter is the highest-leverage change here.
# ---------------------------------------------------------------------------

RAW_EXTENSIONS = {".nef", ".nrw", ".cr2", ".cr3", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}
STD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
IMAGE_EXTENSIONS = RAW_EXTENSIONS | STD_EXTENSIONS

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# --- Video sampling --------------------------------------------------------
# One frame every N seconds is pulled out and scored exactly like a photograph.
VIDEO_SAMPLE_SECONDS = 3.0
VIDEO_MAX_SAMPLES = 400          # safety cap per clip (400 x 3s = 20 min of footage)

# A flat handicap on every video frame, applied after the three axes combine.
#
# What it stands for: 8-bit depth instead of 12 or 14, chroma subsampling,
# inter-frame compression, and motion blur from a shutter angle chosen to make
# movement look smooth rather than to freeze an instant. None of that is
# reliably visible in a downscaled proxy, so the models cannot see it - the
# penalty is the part of the judgement they structurally cannot make.
#
# What it does NOT stand for, deliberately: resolution. Pixel count is reported
# beside each frame and left out of every score, here as everywhere else, so
# that you decide whether a frame is big enough for the job. Clips too small to
# be worth considering at all are dropped by MIN_IMAGE_EDGE instead.
#
# Flat rather than proportional, because the deficit belongs to the medium, not
# to the individual frame: a great frame and a poor frame off the same clip
# share a bit depth and a codec. A constant leaves the ordering of frames
# against each other untouched and shifts the whole population relative to
# stills, which is exactly the intent.
#
# 6.0 is half a verdict band (the bands sit 12 points apart), and a third of
# BLUR_PENALTY - so being video costs less than being out of focus, which is the
# right ordering.
VIDEO_FRAME_PENALTY = 6.0

# Frames scoring at or above this get re-extracted from the source at FULL
# resolution as a real, usable still. Default matches the STRONG band.
VIDEO_EXTRACT_MIN_SCORE = 66.0
VIDEO_EXTRACT_MAX_PER_VIDEO = 12   # keep the best N per clip, after dedup
VIDEO_EXTRACT_FORMAT = "png"       # png = lossless; use "jpg" to save disk

# Put a folder with this name anywhere in the library and everything inside it,
# at any depth, is left out of the reports. Nothing is deleted or moved: the
# photographs stay exactly where they are, and renaming the folder back brings
# them straight into the next report with no rescore needed.
HIDE_DIR_NAME = "hide_from_photo_scout"


def is_hidden(path: Optional[str]) -> bool:
    """
    True when any folder along this path is the hide folder.

    Checked against the whole path rather than only the parent, so hiding a
    folder hides its subfolders too. Case-insensitive and separator-agnostic:
    Windows says HIDE_FROM_PHOTO_SCOUT and a\\b, other systems say
    hide_from_photo_scout and /a/b, and both must behave identically.
    """
    if not path:
        return False
    parts = str(path).replace("\\", "/").split("/")
    return any(part.strip().lower() == HIDE_DIR_NAME for part in parts)


# Folders to skip entirely (sync scratch dirs, previous script output, etc.)
SKIP_DIR_NAMES = {".tmp.drivedownload", "_photo_scout", "photo_scout", "_to_delete",
                  ".picasaoriginals", "originals",
                  ".thumbnails", "$recycle.bin", "system volume information",
                  "__pycache__", ".git", "node_modules", ".ipynb_checkpoints"}

# Standard subdirectories of a Python virtual environment. These are pruned ONLY
# from a directory that also contains a pyvenv.cfg, so a photo folder that happens
# to be called "Scripts" is never affected. Without this, creating a venv inside
# the library means site-packages ships dozens of icons, test fixtures and sample
# images that get scored as if they were your photographs.
VENV_SUBDIRS = {"lib", "lib64", "include", "scripts", "bin", "share", "etc"}

# How the three axes combine into the headline number. Must sum to 1.0.
#
# Subject weight is deliberately low. CLIP recognises almost any competent
# landscape as on-subject, so on a western-landscape library the subject score
# saturates near 100 (observed median 96.6, p75 99.7) and adds roughly the same
# constant to every photograph. It still earns its 0.15 by pushing snapshots,
# test frames and documents down - but it cannot separate one good landscape
# from another, so the weight belongs on the axis that actually varies.
#
# Raise WEIGHT_SUBJECT toward 0.30 if your library is mixed enough that "is this
# even the right subject?" is a real question. Lower it further if nearly
# everything you shoot is already on-subject.
WEIGHT_AESTHETIC = 0.60
WEIGHT_TECHNICAL = 0.25
WEIGHT_SUBJECT = 0.15

# Observed practical range of each raw model output, used to stretch scores to 0-100.
# LAION-Aesthetic nominally spans 1-10 but real photo libraries cluster in ~4.0-7.5.
AESTHETIC_RANGE = (4.0, 7.5)
NIMA_RANGE = (3.8, 6.2)

# Near-duplicate detection. Two images whose 64-bit perceptual hashes differ by
# <= this many bits are treated as the same shot. 0 = only exact matches.
# 5 is a good default: it collapses bracketed exposures and burst frames while
# keeping genuinely different compositions apart.
PHASH_HAMMING_THRESHOLD = 5

# Sharpness floor. Variance-of-Laplacian below this on a 512px-normalized image
# is almost always motion blur or a missed focus. Applied as a score penalty,
# not a hard reject, so you can still review them.
BLUR_VARIANCE_FLOOR = 60.0
BLUR_PENALTY = 18.0  # points subtracted from the composite

# Faults worth naming on the card, as (wording, test, points off). Kept in one
# place because the score, the note and the report's highlighting all need to
# agree on which photographs trip which flag.
DEFECT_RULES = (
    ("looks soft or out of focus", lambda sharp, hi, lo: sharp < BLUR_VARIANCE_FLOOR,
     BLUR_PENALTY),
    ("highlights are blown", lambda sharp, hi, lo: hi > 0.12, 6.0),
    ("large crushed-black areas", lambda sharp, hi, lo: lo > 0.35, 4.0),
)
DEFECT_TEXTS = tuple(t for t, _, _ in DEFECT_RULES)

# Separates the facts on a card's feedback line. The report splits on it to pick
# the defect out for highlighting, so it must not appear inside a fact.
NOTE_SEP = " \u00b7 "

# Each entry is (clip_prompt, short_label). The prompt is what CLIP compares the
# image against; the short label is what appears in your report sentence.
#
# PRIMARY subjects: what you are trying to surface. Shipped tuned for western
# landscape work - replace these with your own.
PRIMARY_PROMPTS = [
    ("a dramatic fine art landscape photograph of red rock desert canyons", "red rock canyon country"),
    ("a fine art photograph of snow capped mountain peaks at golden hour",  "golden-hour mountain peaks"),
    ("a wide open western american landscape with big sky",                 "wide-open western vista"),
    ("a fine art photograph of desert sand dunes and stark terrain",        "stark desert terrain"),
    ("a sweeping vista of a national park wilderness",                      "national park wilderness"),
    ("an alpine lake reflecting mountains, fine art landscape print",       "alpine lake reflection"),
    ("a dramatic sunset over a vast salt flat or lake",                     "sunset over open water or salt flat"),
    ("a long exposure night photograph of the milky way over desert terrain", "night sky over desert"),
    ("a moody atmospheric landscape with dramatic storm clouds",            "moody storm-light landscape"),
]

# SECONDARY subjects: worth flagging when strong, but not the main target. Scored
# slightly lower than primary so landscapes float to the top, while a genuinely
# strong outlier still surfaces.
SECONDARY_PROMPTS = [
    ("a striking architectural photograph with strong geometry",        "strong architectural geometry"),
    ("an evocative environmental portrait of a person",                 "an environmental portrait"),
    ("a wildlife photograph of an animal in natural habitat",           "wildlife in habitat"),
    ("a botanical macro photograph of a flower with beautiful light",   "a botanical macro"),
    ("an intimate close up of tree bark texture and natural detail",    "an intimate nature detail"),
    ("a fine art black and white photograph with strong tonal contrast", "high-contrast black and white"),
    ("an abstract photograph of natural patterns and textures",         "an abstract natural texture"),
    ("a nostalgic photograph of americana roadside infrastructure",     "roadside americana"),
]
SECONDARY_DISCOUNT = 0.80  # secondary matches score at 80% of a primary match

# DISTRACTORS: things that should NOT surface at all. Their similarity competes
# with the targets, so a snapshot scores low on subject.
DISTRACTOR_PROMPTS = [
    ("a blurry out of focus accidental photograph",                     "an accidental frame"),
    ("a casual snapshot of friends at a party",                         "a casual snapshot"),
    ("a photograph of a parking lot or an ordinary street with nothing of interest", "an unremarkable scene"),
    ("a test shot of a wall, floor, or lens cap",                       "a test shot"),
    ("an underexposed dark frame with no visible subject",              "an underexposed frame"),
    ("a cluttered indoor snapshot with poor lighting",                  "a cluttered indoor snapshot"),
    ("a screenshot or a photograph of a document or sign",              "a document or sign"),
    ("a photograph of food on a table",                                 "a food photo"),
]

# Verdict bands applied to the final composite score.
VERDICT_BANDS = [
    (78.0, "TOP PICK"),
    (66.0, "STRONG"),
    (54.0, "MAYBE"),
    (0.0, "PASS"),
]

# Tunables that --calibrate may override, written to calibration.json in the
# output directory. Delete that file to return to the defaults above.
CALIBRATABLE = ("AESTHETIC_RANGE", "NIMA_RANGE", "VERDICT_BANDS",
                "WEIGHT_AESTHETIC", "WEIGHT_TECHNICAL", "WEIGHT_SUBJECT")
CALIBRATION_FILE = "calibration.json"

# Everything this script produces goes in ONE directory, and by default that
# directory sits next to the script - never inside your photo library. The
# library is treated as strictly read-only input: originals are opened 'rb' and
# nothing is ever created, modified or deleted underneath --root.
OUTPUT_DIRNAME = "_photo_scout"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = SCRIPT_DIR / OUTPUT_DIRNAME

# --- Tagging ---------------------------------------------------------------
# Tags you type into the report are hand-authored data, not derived output, so
# they live in their own file and --reset preserves them.
TAGS_FILE = "tags.json"
TAG_MAX_LEN = 40
# Alphanumerics, space, underscore, hyphen. Nothing else is allowed through, in
# the browser or here. That restriction is also what makes tags safe to render:
# with no angle brackets, quotes or ampersands there is nothing to inject.
TAG_ALLOWED_RE = re.compile(r"[^A-Za-z0-9 _-]")
THUMB_SIZE = 400                  # long edge, px, for the review contact sheet
SCORING_SIZE = 512                # long edge fed to the models (they downsample further)

# --- Practical size floor ---------------------------------------------------
# This tool judges photographs and video stills. A real library always contains
# images that are neither: app icons, emoji, sprite sheets, saved memes, GIF
# reactions, downloaded web thumbnails, screenshots of thumbnails. Scoring them
# costs time and puts junk in the report, and no aesthetic model will reliably
# tell you a meme is not a landscape.
#
# The filter is dimensional rather than by file type, because a JPEG can be
# either. Anything whose SHORTER side falls below MIN_IMAGE_EDGE is skipped
# without being scored at all.
#
# Why the shorter side: it is the dimension that survives cropping. A legitimate
# 3:1 panorama is still comfortably over 500 pixels tall, whereas a 480x360 meme
# is not. Measuring the longer side would let almost every meme through.
#
# Why 500: it sits in the empty gap between the two populations. The smallest
# export any camera or phone produces is around 1024 on the long edge, so no
# real photograph comes near it; icons, emoji and reaction GIFs are almost all
# under 500 on at least one side. It is a floor, not a quality bar - see
# --min-edge to move it, and --min-edge 0 to switch the filter off and score
# everything.
#
# NOTE this is the ONLY place resolution is allowed to influence anything. It
# decides whether a file is a photograph worth looking at; it never adjusts a
# score. See _score_one().
MIN_IMAGE_EDGE = 500

# Browsers cannot display a NEF, so the report's lightbox shows a JPEG rendered
# from the RAW at this size. 1600px is enough to judge a photograph full-screen
# on a normal display and costs roughly 250 KB each - about 900 MB across 3,500
# photos. Drop to 1200 to halve that, or pass --no-previews to skip it entirely.
PREVIEW_SIZE = 1600
PREVIEW_QUALITY = 85

LAION_WEIGHTS_URL = (
    "https://github.com/christophschuhmann/improved-aesthetic-predictor/"
    "raw/main/sac%2Blogos%2Bava1-l14-linearMSE.pth"
)
# Linked from the report footer.
PROJECT_URL = "https://github.com/briansalisbury/photo-scout/"

CLIP_MODEL_ID = "openai/clip-vit-large-patch14"
CLIP_EMBED_DIM = 768   # ViT-L/14 projection width; the LAION head's input size


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rescale(value: float, lo: float, hi: float) -> float:
    """
    Map a raw model score onto 0-100, clamped.

    A zero-width range would divide by zero. That is reachable in practice: if a
    model returns near-identical values across a library, calibration can derive
    lo == hi. Treat it as "no information" and return the midpoint rather than
    crashing - the axis then contributes a constant, which is the truth.
    """
    if value is None:
        return 0.0
    if hi - lo < 1e-9:
        return 50.0
    return float(np.clip((value - lo) / (hi - lo) * 100.0, 0.0, 100.0))


def verdict_for(score: float) -> str:
    for threshold, label in VERDICT_BANDS:
        if score >= threshold:
            return label
    return "PASS"


def hamming64(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------------------
# STAGE 1 - Image loading. RAW files are decoded read-only.
# ---------------------------------------------------------------------------

def _exif_taken(img_or_bytes) -> Optional[str]:
    """
    Capture timestamp as 'YYYY-MM-DD HH:MM:SS', or 'YYYY-MM-DD' when the camera
    recorded no usable clock time, or None when there is no date at all.

    Prefers EXIF DateTimeOriginal (when the shutter fired) over DateTime (when
    the file was last written), because editing software rewrites the latter.
    Stored in this shape because it sorts correctly as plain text - two frames
    from the same morning order by the minute they were taken - and a date-only
    value sorts ahead of any timed one on the same day, which is the only
    sensible place to put it.
    """
    try:
        if isinstance(img_or_bytes, (bytes, bytearray)):
            import io as _io
            probe = Image.open(_io.BytesIO(img_or_bytes))
        else:
            probe = img_or_bytes
        exif = probe.getexif()
        if not exif:
            return None
        # 0x8769 is the Exif sub-IFD, where the interesting timestamps live.
        try:
            sub = exif.get_ifd(0x8769)
        except Exception:
            sub = {}
        raw = (sub.get(36867)          # DateTimeOriginal
               or sub.get(36868)       # DateTimeDigitized
               or exif.get(306))       # DateTime (file modification)
        if not raw or not isinstance(raw, str):
            return None
        # EXIF format is 'YYYY:MM:DD HH:MM:SS'; the time half is optional and
        # some cameras write placeholder zeros into it.
        chunks = raw.strip().split()
        head = chunks[0].replace(":", "-")
        parts = head.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return None
        y, m, d = (int(p) for p in parts)
        if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
            return None
        stamp = f"{y:04d}-{m:02d}-{d:02d}"

        if len(chunks) > 1:
            tparts = chunks[1].split(":")
            if len(tparts) >= 2 and all(p.isdigit() for p in tparts[:3]):
                hh, mm = int(tparts[0]), int(tparts[1])
                ss = int(tparts[2]) if len(tparts) > 2 else 0
                if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                    stamp += f" {hh:02d}:{mm:02d}:{ss:02d}"
        return stamp
    except Exception:
        return None


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def pretty_date(stamp: Optional[str]) -> str:
    """
    '2026-08-24 14:03:22' -> 'August 24, 2026'. Empty string for anything
    unusable. Accepts a bare date too, so rows scored before capture times were
    recorded still render.
    """
    if not stamp or len(stamp) < 10:
        return ""
    try:
        y, m, d = int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10])
        # Range-check before indexing: _MONTHS[m-1] with m=0 silently wraps to
        # December, which would render a malformed row as "December 0, 0".
        if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
            return ""
        return f"{_MONTHS[m - 1]} {d}, {y}"
    except Exception:
        return ""


def pretty_time(stamp: Optional[str]) -> str:
    """
    '2026-08-24 14:03:22' -> '14:03'. Empty string when the camera recorded no
    time. 24-hour, because that is what was asked for and it sorts.
    """
    if not stamp or len(stamp) < 16:
        return ""
    try:
        hh, mm = int(stamp[11:13]), int(stamp[14:16])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return ""
        return f"{hh:02d}:{mm:02d}"
    except Exception:
        return ""


def pretty_taken(stamp: Optional[str]) -> str:
    """'August 24, 2026 · 14:03', dropping whichever half is missing."""
    date, time = pretty_date(stamp), pretty_time(stamp)
    if date and time:
        return f"{date} · {time}"
    return date or ""


def defects(sharpness: float, clip_hi: float, clip_lo: float) -> list[str]:
    """Which quality flags a photograph trips, in order of seriousness."""
    return [t for t, test, _ in DEFECT_RULES if test(sharpness, clip_hi, clip_lo)]


def split_note(note: Optional[str]) -> tuple[str, str]:
    """
    Separate a feedback line into (facts, defect).

    Matched against the known wordings rather than by splitting on the last
    separator, so a subject label containing one cannot be mistaken for a fault.
    """
    note = note or ""
    for text in DEFECT_TEXTS:
        if note.endswith(NOTE_SEP + text):
            return note[: -len(NOTE_SEP + text)], text
    return note, ""


def pretty_resolution(w: Optional[int], h: Optional[int]) -> str:
    """
    '6000 × 4000 · 24.0 MP', or '' when the dimensions were never recorded.

    Shown on every card precisely because resolution is kept out of the score.
    The models rank composition, light, focus and exposure; whether 2.1 MP is
    enough for the licence you have in mind is a judgement about the job, not
    about the photograph, so it is put in front of you rather than guessed at.
    """
    if not w or not h:
        return ""
    return f"{w} × {h} · {(w * h) / 1e6:.1f} MP"


def spec_atoms(stamp: Optional[str], w: Optional[int], h: Optional[int]) -> list[str]:
    """
    The facts shown under a photograph, as pieces that must never be broken
    across a line: ['June 28, 2011', '07:00', '6000 × 4000', '24.0 MP'].

    Returned separately rather than as one string so the report can wrap between
    them and not inside them. A date split over two lines reads as a typo.
    """
    out = [b for b in (pretty_date(stamp), pretty_time(stamp)) if b]
    if w and h:
        out.append(f"{w} × {h}")
        out.append(f"{(w * h) / 1e6:.1f} MP")
    return out


# A date at the start or the end of a folder name: a four-digit year followed by
# a month, optionally a day, in any of the usual separator styles -
# "2011-07-05", "2011 6 28", "2011.07.05", "20110705".
_FOLDER_DATE_BODY = r"((?:19|20)\d{2})[-_.\s/]*(\d{1,2})(?:[-_.\s/]*(\d{1,2}))?(?!\d)"
_FOLDER_DATE_LEAD = re.compile(r"^\s*" + _FOLDER_DATE_BODY)
_FOLDER_DATE_TRAIL = re.compile(r"(?<!\d)" + _FOLDER_DATE_BODY + r"\s*$")
# Separators left stranded once the date is gone: "2011-07-05 - Wyoming".
_STRANDED = " -_.,/\\\t"


def strip_folder_date(name: Optional[str]) -> str:
    """
    Folder name with a leading or trailing date removed, for display.

    '2011-07-05 - Wyoming'  -> 'Wyoming'
    '2011 6 28 Tetons'      -> 'Tetons'
    'Arches 2010.03.12'     -> 'Arches'
    '2011 Wyoming'          -> '2011 Wyoming'   (a bare year is not a date)
    '2011-07-05'            -> '2011-07-05'     (nothing else left, so keep it)

    A month is required, so a year on its own is left alone - plenty of people
    name a folder just '2011' and mean it. Month and day are range-checked, so
    '1998 500 Photos' keeps its 500.
    """
    if not name:
        return ""
    # Nested folders are cleaned a component at a time, so
    # '2011-07-05 - Wyoming\\Publish' keeps its 'Publish'. The original
    # separators are preserved by splitting on them and putting them back.
    parts = re.split(r"([\\/])", name.strip())
    for i in range(0, len(parts), 2):
        parts[i] = _strip_one_component(parts[i])
    out = "".join(parts).strip()
    # If the folder was nothing but a date, there is no name to show, so the
    # original is better than a blank line.
    return out or name.strip()


def _strip_one_component(part: str) -> str:
    out = part.strip()
    for pattern in (_FOLDER_DATE_LEAD, _FOLDER_DATE_TRAIL):
        m = pattern.search(out)
        if not m:
            continue
        month = int(m.group(2))
        day = int(m.group(3)) if m.group(3) else None
        if not (1 <= month <= 12) or (day is not None and not (1 <= day <= 31)):
            continue                      # a number that merely looks like a date
        stripped = (out[:m.start()] + out[m.end():]).strip(_STRANDED).strip()
        if stripped:                      # never blank out a whole component
            out = stripped
    return out


def read_dimensions(path: Path) -> Optional[tuple[int, int]]:
    """
    (width, height) read from the file header, without decoding any pixels.

    Returns None when the size cannot be established cheaply. None means "let it
    through and decide later", never "reject" - a filter that guesses would be
    worse than no filter.

    RAW is the deliberate None case. Pillow cannot open a NEF or a CR2, and
    parsing one with libraw costs far more than the check saves on a run that
    walks the whole library every time. It is also unnecessary: no camera has
    ever written a RAW file anywhere near MIN_IMAGE_EDGE. Should one somehow
    exist, the backstop in _score_one() catches it after the decode.
    """
    if path.suffix.lower() in RAW_EXTENSIONS:
        return None
    try:
        # Image.open parses the header only; .load() is what would decode.
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def below_size_floor(size: Optional[tuple[int, int]], floor: int) -> bool:
    """
    True when an image is too small to be worth scoring.

    An unknown size passes, and a floor of 0 or less disables the check
    entirely. Note that EXIF orientation is irrelevant here: rotating an image
    swaps its two dimensions and min() is unchanged either way.
    """
    if floor <= 0 or not size:
        return False
    return min(size) < floor


def probe_video_size(path: Path) -> Optional[tuple[int, int]]:
    """
    (width, height) of a clip's first video stream, via ffprobe.

    The source clip's own resolution is what matters, not the downscaled proxy
    frames sampled for scoring: an extracted still comes out at native size.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        w, h = out.stdout.strip().split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def load_image(path: Path, max_edge: int = SCORING_SIZE) -> Image.Image:
    """
    Return a downscaled RGB PIL image for `path`.

    For RAW files this prefers the camera's own embedded JPEG preview, which is
    typically 1-2 MP and decodes in ~30ms. Full Bayer demosaicing of a 12MP NEF
    takes 1-2 seconds - about 40x slower - and makes no difference once the
    image is resized to 512px for scoring. If no usable preview exists we fall
    back to a half-resolution demosaic.

    The original file is opened read-only and never written to.
    """
    suffix = path.suffix.lower()

    taken = None
    if suffix in RAW_EXTENSIONS:
        import rawpy  # imported lazily so non-RAW users don't need libraw

        with rawpy.imread(str(path)) as raw:
            img = None
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    import io
                    img = Image.open(io.BytesIO(thumb.data))
                    img.load()
                    # The camera's embedded preview carries the full EXIF block,
                    # so the capture date comes free with the decode we already do.
                    taken = _exif_taken(thumb.data)
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = Image.fromarray(thumb.data)
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError, AttributeError):
                img = None

            # Reject previews too small to score reliably, and re-demosaic instead.
            if img is None or max(img.size) < 512:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=True,
                    no_auto_bright=False,
                    output_bps=8,
                )
                img = Image.fromarray(rgb)
    else:
        img = Image.open(path)
        img.load()
        taken = _exif_taken(img)

    img = img.convert("RGB")

    # Honour the EXIF orientation flag so portrait shots aren't scored sideways.
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Captured BEFORE the downscale below, because after it every image is the
    # same size and the file's real resolution is gone. It is reported beside
    # the photograph so you can judge whether the resolution suits your use; it
    # is never fed into a score.
    native = img.size

    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    # Carried on the image so callers get it without a second decode.
    img.info["psc_taken"] = taken
    img.info["psc_native"] = native
    return img


# ---------------------------------------------------------------------------
# STAGE 1b - Video. ffmpeg is called as a subprocess; nothing is re-encoded and
#            the source clip is only ever read.
# ---------------------------------------------------------------------------

VIRTUAL_SEP = "#t="   # a sampled frame is addressed as  C:\path\clip.mp4#t=12.000


def make_virtual_path(video: Path, ts: float) -> str:
    return f"{video}{VIRTUAL_SEP}{ts:.3f}"


def split_virtual_path(p: str) -> tuple[str, Optional[float]]:
    """Inverse of make_virtual_path. Returns (real_path, timestamp_or_None)."""
    if VIRTUAL_SEP in p:
        base, ts = p.rsplit(VIRTUAL_SEP, 1)
        try:
            return base, float(ts)
        except ValueError:
            return p, None
    return p, None


def hhmmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60:d}:{s % 60:02d}"


def have_ffmpeg() -> bool:
    import shutil as _sh
    return bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))


def probe_duration(path: Path) -> Optional[float]:
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def sample_video_frames(video: Path, work_dir: Path,
                        every: float = VIDEO_SAMPLE_SECONDS,
                        max_samples: int = VIDEO_MAX_SAMPLES) -> list[tuple[float, Path]]:
    """
    Pull one small JPEG every `every` seconds in a single decode pass.

    Uses the fps filter rather than one seek-per-frame: a single sequential
    decode beats hundreds of process spawns, and these frames only exist to be
    scored at 512px. Anything worth keeping is re-extracted at full resolution
    later by extract_still().

    Returns [(timestamp_seconds, jpeg_path), ...].
    """
    import subprocess

    work_dir.mkdir(parents=True, exist_ok=True)
    for stale in work_dir.glob("f_*.jpg"):
        stale.unlink()

    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(video),
        "-vf", f"fps=1/{every},scale='min({PREVIEW_SIZE},iw)':-2",
        "-frames:v", str(max_samples),
        "-q:v", "3",
        str(work_dir / "f_%05d.jpg"),
    ]
    subprocess.run(cmd, capture_output=True, timeout=3600, check=True)

    frames = []
    for jpg in sorted(work_dir.glob("f_*.jpg")):
        idx = int(jpg.stem.split("_")[1])
        # fps=1/every emits output frame i at source time i*every (i is 1-based here)
        frames.append(((idx - 1) * every, jpg))

    duration = probe_duration(video)

    # The fps filter only emits a frame once a full interval has elapsed, so two
    # gaps need closing by hand:
    #
    #  1. A clip shorter than one interval yields NOTHING - a 1-second clip would
    #     silently disappear from the report entirely.
    #  2. The tail after the last emitted frame is never sampled: a 25s clip at
    #     one frame per 3s stops at t=21 and ignores the final 4 seconds.
    if duration and duration > 0:
        if not frames:
            mid = duration / 2.0
            dest = work_dir / "f_00001.jpg"
            if _grab_scoring_frame(video, mid, dest):
                frames = [(mid, dest)]
        else:
            last_ts = frames[-1][0]
            tail = duration - last_ts
            if tail >= every:
                ts = max(0.0, duration - min(0.5, duration * 0.02))
                dest = work_dir / f"f_{len(frames) + 1:05d}_tail.jpg"
                if _grab_scoring_frame(video, ts, dest):
                    frames.append((ts, dest))

    return frames


def _grab_scoring_frame(video: Path, ts: float, dest: Path) -> bool:
    """One downscaled frame at `ts`, for scoring only. Output-seeks for accuracy."""
    import subprocess
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(video),
             "-ss", f"{ts:.3f}", "-frames:v", "1",
             "-vf", f"scale='min({PREVIEW_SIZE},iw)':-2", "-q:v", "3", str(dest)],
            capture_output=True, timeout=600, check=True,
        )
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def extract_still(video: Path, ts: float, dest: Path) -> bool:
    """
    Write the frame at `ts` to `dest` at the clip's native resolution.

    Seeks in two stages - a fast keyframe jump to 2s before the target, then an
    accurate decode forward. Input-only seeking is fast but lands on a keyframe
    (wrong frame); output-only seeking is exact but decodes the whole clip from
    zero. The hybrid is both fast and frame-accurate.
    """
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    pre = max(0.0, ts - 2.0)
    fine = ts - pre
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
           "-ss", f"{pre:.3f}", "-i", str(video), "-ss", f"{fine:.3f}",
           "-frames:v", "1"]
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        cmd += ["-q:v", "1"]
    cmd.append(str(dest))
    try:
        subprocess.run(cmd, capture_output=True, timeout=300, check=True)
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# STAGE 2 - Cheap numeric quality checks (no ML, runs in ~2ms)
# ---------------------------------------------------------------------------

def sharpness_variance(img: Image.Image) -> float:
    """
    Variance of the Laplacian - the standard cheap focus metric. A sharp image
    has lots of high-frequency edge energy and therefore high variance; a blurry
    one is smooth and has low variance.

    The fixed 512x512 resize is what makes the number comparable between images.
    Laplacian variance rises with pixel count, so measuring at native size would
    report a soft 45 MP frame as sharper than a crisp 12 MP one - resolution
    masquerading as focus. Callers already hand this a normalised canvas (see
    _score_one); the resize here squares it up and guarantees the invariant
    even if some future caller forgets.
    """
    g = np.asarray(img.convert("L").resize((512, 512), Image.BILINEAR), dtype=np.float32)
    # 3x3 Laplacian kernel applied by slicing (avoids a scipy/cv2 dependency)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var())


def clipping_fraction(img: Image.Image) -> tuple[float, float]:
    """Fraction of pixels blown to pure white / crushed to pure black."""
    a = np.asarray(img.convert("L"), dtype=np.uint8)
    total = a.size
    return float((a >= 253).sum()) / total, float((a <= 2).sum()) / total


def perceptual_hash(img: Image.Image) -> int:
    """
    64-bit difference hash. Resize to 9x8 greyscale, then emit one bit per
    horizontal neighbour comparison. Robust to exposure shifts and light crops,
    which is exactly what we want for collapsing bracketed and burst frames.
    """
    g = np.asarray(img.convert("L").resize((9, 8), Image.LANCZOS), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out


# ---------------------------------------------------------------------------
# STAGE 3 - The models
# ---------------------------------------------------------------------------

def check_torchvision_compat() -> None:
    """
    torchvision ships compiled C++ operators pinned to one exact torch version.
    When they disagree, the failure surfaces far away - deep inside a transformers
    lazy import - as:

        RuntimeError: operator torchvision::nms does not exist
        ModuleNotFoundError: Could not import module 'CLIPModel'

    which points at CLIP and says nothing about the real cause. Detect it here,
    before any model loading, and say precisely what to do.

    torchvision is optional for this script: transformers falls back to PIL for
    image preprocessing when it is absent. Only pyiqa (NIMA) truly needs it.
    """
    import importlib.util
    if importlib.util.find_spec("torchvision") is None:
        return  # not installed at all - fine, PIL handles preprocessing

    import torch
    try:
        import torchvision
        torch.ops.torchvision.nms  # force the C++ op table to resolve
    except Exception as exc:
        try:
            tv_version = importlib.metadata.version("torchvision")
        except Exception:
            tv_version = "unknown"
        raise SystemExit(
            f"\n"
            f"torchvision {tv_version} is incompatible with torch {torch.__version__}.\n"
            f"  ({type(exc).__name__}: {exc})\n\n"
            f"torchvision bundles compiled operators tied to one exact torch build,\n"
            f"so the two must be installed as a matched pair.\n\n"
            f"This script does NOT need torchvision - transformers uses PIL instead.\n"
            f"Only the optional NIMA model requires it. Quickest fix, which leaves\n"
            f"your torch install completely untouched:\n\n"
            f"    pip uninstall -y torchvision\n"
            f"    python photo_scout.py --root /path/to/photos --no-nima ...\n\n"
            f"To keep NIMA instead, install the torchvision matching your torch\n"
            f"(torchvision 0.X pairs with torch 2.(X-15): 0.26 with 2.11, 0.27 with\n"
            f"2.12, 0.28 with 2.13) using --no-deps so torch is not replaced:\n\n"
            f"    pip install --no-deps --force-reinstall \"torchvision==0.26.*\" \\\n"
            f"        --index-url https://download.pytorch.org/whl/cu128\n"
        )


def clip_features(out, expect_dim: Optional[int] = None):
    """
    Pull the projected embedding tensor out of a CLIP get_text_features() /
    get_image_features() call, across transformers versions.

    transformers v4 returns a plain tensor. transformers v5 returns a
    BaseModelOutputWithPooling instead, and the embedding lives in
    `.pooler_output` - verified post-projection, since its width equals
    config.projection_dim rather than the encoder's hidden_size.

    The width is checked rather than trusted: a wrong-width vector would still
    multiply cleanly against the text embeddings and feed the aesthetic head,
    producing confident-looking scores that mean nothing. Better to stop.
    """
    import torch

    tensor = None
    if torch.is_tensor(out):
        tensor = out
    else:
        for attr in ("text_embeds", "image_embeds", "embeds", "pooler_output"):
            value = getattr(out, attr, None)
            if torch.is_tensor(value):
                tensor = value
                break
        if tensor is None and isinstance(out, (tuple, list)) and out and torch.is_tensor(out[0]):
            tensor = out[0]

    if tensor is None:
        raise TypeError(
            f"Could not find a feature tensor in CLIP output of type "
            f"{type(out).__name__}. This usually means the installed transformers "
            f"version changed its return format again - please report it."
        )
    if tensor.dim() != 2:
        raise ValueError(f"Expected a 2-D CLIP embedding, got shape {tuple(tensor.shape)}")
    if expect_dim is not None and tensor.shape[-1] != expect_dim:
        raise ValueError(
            f"CLIP returned {tensor.shape[-1]}-d embeddings but {expect_dim}-d were "
            f"expected for {CLIP_MODEL_ID}. The LAION aesthetic head requires "
            f"{expect_dim}-d input, so scoring would be meaningless."
        )
    return tensor


def normalise_head_state_dict(state: dict) -> dict:
    """
    Make the published LAION-Aesthetic checkpoint loadable into a bare
    nn.Sequential.

    The upstream weights were saved from a PyTorch Lightning module whose
    Sequential lives in an attribute called `layers`, so every key arrives as
    "layers.0.weight" rather than "0.weight". Some redistributions also wrap the
    whole thing in a {"state_dict": ...} envelope. Handle all three shapes, and
    fail loudly with a useful message if a fourth turns up.
    """
    if isinstance(state, dict) and "state_dict" in state and isinstance(state["state_dict"], dict):
        state = state["state_dict"]

    keys = list(state.keys())
    if not keys:
        raise ValueError("LAION weights file contained no tensors")

    # Strip a single common leading component if every key shares one and the
    # remainder looks like a Sequential index ("0.weight", "7.bias", ...).
    first = keys[0].split(".")[0]
    if not first.isdigit() and all(k.startswith(first + ".") for k in keys):
        state = {k[len(first) + 1:]: v for k, v in state.items()}

    if not all(k.split(".")[0].isdigit() for k in state):
        raise ValueError(
            "Unexpected key layout in the LAION-Aesthetic checkpoint: "
            f"{list(state)[:4]}. Expected Sequential-style keys like '0.weight'."
        )
    return state


class Scorer:
    """
    Holds the three models. Loaded once, reused for every image.

    - CLIP ViT-L/14 produces a 768-d embedding per image. That single embedding
      feeds BOTH the LAION aesthetic head and the zero-shot subject matcher, so
      the expensive part of the pipeline runs once per photo, not twice.
    - The LAION head is a small MLP (768 -> 1024 -> 128 -> 64 -> 16 -> 1)
      trained to regress human aesthetic ratings.
    - NIMA comes from pyiqa and is optional (--no-nima skips it).
    """

    def __init__(self, device: Optional[str] = None, use_nima: bool = True, cache_dir: Optional[Path] = None):
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self.torch = torch
        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        built_with = getattr(torch.version, "cuda", None)
        log(f"Torch {torch.__version__} (built against CUDA {built_with or 'none - CPU-only wheel'})")
        log(f"Torch device: {self.device}")
        if self.device == "cpu":
            if built_with is None:
                log("  This is a CPU-only build of PyTorch, so the GPU cannot be used")
                log("  regardless of what hardware you have. Scoring still works at")
                log("  roughly 1-2s per photo. See README section 3 to switch builds.")
            else:
                log("  PyTorch has CUDA support but no usable GPU was found - check")
                log("  your NVIDIA driver with `nvidia-smi`. Running on CPU for now.")

        log(f"Loading CLIP: {CLIP_MODEL_ID}")
        self.clip = CLIPModel.from_pretrained(CLIP_MODEL_ID).to(self.device).eval()
        self.clip_proc = CLIPProcessor.from_pretrained(CLIP_MODEL_ID)

        self.aesthetic_head = self._load_laion_head(cache_dir)
        self.nima = self._load_nima() if use_nima else None

        # Pre-compute the text embeddings once. They never change during a run.
        self.prompt_labels: list[str] = []
        self.prompt_tier: list[str] = []
        all_prompts: list[str] = []
        for tier, table in (("primary", PRIMARY_PROMPTS),
                            ("secondary", SECONDARY_PROMPTS),
                            ("distractor", DISTRACTOR_PROMPTS)):
            for prompt, label in table:
                all_prompts.append(prompt)
                self.prompt_tier.append(tier)
                self.prompt_labels.append(label)

        with torch.no_grad():
            tok = self.clip_proc(text=all_prompts, return_tensors="pt", padding=True).to(self.device)
            tfeat = clip_features(self.clip.get_text_features(**tok), CLIP_EMBED_DIM)
            self.text_features = tfeat / tfeat.norm(dim=-1, keepdim=True)
        log(f"Encoded {len(all_prompts)} subject prompts "
            f"({tuple(self.text_features.shape)})")

    def _load_laion_head(self, cache_dir: Optional[Path]):
        import torch
        import torch.nn as nn

        cache_dir = cache_dir or (Path.home() / ".cache" / "photo_scout")
        cache_dir.mkdir(parents=True, exist_ok=True)
        weights_path = cache_dir / "laion_aesthetic_l14_linearMSE.pth"

        if not weights_path.exists():
            log("Downloading LAION-Aesthetic weights (~15 MB, one time)")
            import urllib.request
            urllib.request.urlretrieve(LAION_WEIGHTS_URL, weights_path)

        head = nn.Sequential(
            nn.Linear(768, 1024), nn.Dropout(0.2),
            nn.Linear(1024, 128), nn.Dropout(0.2),
            nn.Linear(128, 64), nn.Dropout(0.1),
            nn.Linear(64, 16),
            nn.Linear(16, 1),
        )
        state = torch.load(weights_path, map_location="cpu", weights_only=True)
        head.load_state_dict(normalise_head_state_dict(state))
        return head.to(self.device).eval()

    def _load_nima(self):
        try:
            import pyiqa
        except ImportError:
            log("pyiqa not installed - skipping NIMA. Technical score will use "
                "sharpness/clipping only. Install with: pip install pyiqa")
            return None
        try:
            log("Loading NIMA (pyiqa)")
            return pyiqa.create_metric("nima", device=self.device)
        except Exception as exc:
            log(f"Could not load NIMA ({exc}) - continuing without it.")
            return None

    def score(self, img: Image.Image) -> dict:
        torch = self.torch
        out: dict = {}

        with torch.no_grad():
            inputs = self.clip_proc(images=img, return_tensors="pt").to(self.device)
            feats = clip_features(self.clip.get_image_features(**inputs), CLIP_EMBED_DIM)
            feats_n = feats / feats.norm(dim=-1, keepdim=True)

            # --- Aesthetic -------------------------------------------------
            out["aesthetic_raw"] = float(self.aesthetic_head(feats_n.float()).item())

            # --- Subject match ---------------------------------------------
            # Cosine similarities, temperature-scaled into a distribution.
            sims = (feats_n @ self.text_features.T).squeeze(0)
            probs = (sims * 100.0).softmax(dim=-1).cpu().numpy()

            best_primary = best_secondary = 0.0
            best_label = ""
            best_tier = "distractor"
            distractor_mass = 0.0
            for i, tier in enumerate(self.prompt_tier):
                p = float(probs[i])
                if tier == "primary" and p > best_primary:
                    best_primary = p
                    if p >= best_secondary * SECONDARY_DISCOUNT:
                        best_label, best_tier = self.prompt_labels[i], "primary"
                elif tier == "secondary" and p > best_secondary:
                    best_secondary = p
                    if p * SECONDARY_DISCOUNT > best_primary:
                        best_label, best_tier = self.prompt_labels[i], "secondary"
                elif tier == "distractor":
                    distractor_mass += p

            target_mass = best_primary + best_secondary * SECONDARY_DISCOUNT
            # Relevance = how much of the model's belief lands on target subjects
            # rather than on the distractor set.
            relevance = target_mass / max(target_mass + distractor_mass, 1e-6)
            out["subject_score"] = float(np.clip(relevance * 100.0, 0.0, 100.0))
            out["subject_label"] = best_label
            out["subject_tier"] = best_tier

            # --- NIMA technical ---------------------------------------------
            if self.nima is not None:
                arr = np.asarray(img, dtype=np.float32) / 255.0
                t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(self.device)
                out["nima_raw"] = float(self.nima(t).item())
            else:
                out["nima_raw"] = None

        return out


# ---------------------------------------------------------------------------
# STAGE 4 - Result assembly
# ---------------------------------------------------------------------------

@dataclass
class PhotoResult:
    path: str
    rel_path: str
    folder: str
    filename: str
    mtime: float
    size: int
    phash: int = 0
    aesthetic_raw: Optional[float] = None
    nima_raw: Optional[float] = None
    subject_score: float = 0.0
    subject_label: str = ""
    subject_tier: str = ""
    sharpness: float = 0.0
    clip_hi: float = 0.0
    clip_lo: float = 0.0
    composite: float = 0.0
    verdict: str = ""
    note: str = ""
    dup_of: Optional[str] = None
    error: Optional[str] = None
    # Native pixel dimensions of the source file (of the source CLIP, for a
    # video frame). Reported, never scored - see _score_one().
    width: Optional[int] = None
    height: Optional[int] = None
    # Video-frame fields. source_type is "photo" or "video_frame".
    taken_at: Optional[str] = None      # 'YYYY-MM-DD' from EXIF, or None
    source_type: str = "photo"
    source_video: Optional[str] = None
    timestamp_s: Optional[float] = None
    extracted_path: Optional[str] = None


def compose(res: PhotoResult) -> None:
    """Blend the axes into the headline score, apply penalties, write the sentence."""
    aes = rescale(res.aesthetic_raw, *AESTHETIC_RANGE)

    if res.nima_raw is not None:
        tech = rescale(res.nima_raw, *NIMA_RANGE)
    else:
        # No NIMA available: derive a technical proxy from sharpness alone.
        tech = float(np.clip(np.log1p(res.sharpness) / np.log1p(800.0) * 100.0, 0, 100))

    score = (WEIGHT_AESTHETIC * aes
             + WEIGHT_TECHNICAL * tech
             + WEIGHT_SUBJECT * res.subject_score)

    problems = defects(res.sharpness, res.clip_hi, res.clip_lo)
    for text, _, penalty in DEFECT_RULES:
        if text in problems:
            score -= penalty
    if res.source_type == "video_frame":
        score -= VIDEO_FRAME_PENALTY

    res.composite = float(np.clip(score, 0.0, 100.0))
    res.verdict = verdict_for(res.composite)

    # --- the line under the thumbnail -----------------------------------
    # Two axis scores, what the subject matcher saw, and any defect. Nothing
    # else: the verdict is already in the badge, the composite in the number
    # beside it, and a frame's timestamp in the VIDEO badge, so restating them
    # here just made every card read the same as every other one.
    label = res.subject_label or "no clear subject"
    bits = [f"Aesthetic {aes:.0f}", f"Technical {tech:.0f}",
            label[0].upper() + label[1:]]
    if problems:
        bits.append(problems[0])
    res.note = NOTE_SEP.join(bits)


# ---------------------------------------------------------------------------
# STAGE 4b - Calibration.
#
# The raw model outputs are stored in the database, so the composite score, the
# verdict and the feedback sentence can all be rebuilt WITHOUT re-running any
# models. That makes retuning a seconds-long operation instead of an hours-long
# one, which matters: the default AESTHETIC_RANGE and band cutoffs are guesses
# made before seeing anybody's photographs, and they will usually be wrong.
# ---------------------------------------------------------------------------

def sanitize_tag(raw: str) -> str:
    """
    Reduce a typed string to a safe tag, or "" if nothing survives.

    Allows letters, digits, spaces, underscores and hyphens. Everything else is
    dropped, runs of whitespace collapse to one, and the result is trimmed and
    length-capped. The browser applies exactly the same rule; this repeats it
    because tags.json is an editable file on disk and must never be trusted.
    """
    if not isinstance(raw, str):
        return ""
    # Whitespace becomes a single space BEFORE disallowed characters are
    # dropped. Doing it the other way round deletes newlines and tabs outright,
    # welding "Lake\nPhotos" into "LakePhotos".
    cleaned = re.sub(r"\s+", " ", raw)
    cleaned = TAG_ALLOWED_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_").strip()
    return cleaned[:TAG_MAX_LEN].strip(" -_").strip()


def load_tags(out_dir: Path) -> dict:
    """
    Read tags.json: {"<absolute photo path>": ["Lake Photos", "Sunset"], ...}

    Unreadable or malformed content is reported and ignored rather than raised -
    losing a report build over a stray comma in a hand-edited file would be a
    poor trade. Tags are de-duplicated case-insensitively, keeping the first
    spelling seen.
    """
    path = out_dir / TAGS_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"Could not read {TAGS_FILE} ({exc}) - continuing without tags. "
            f"The file has been left alone.")
        return {}
    if not isinstance(raw, dict):
        log(f"{TAGS_FILE} is not a JSON object - ignoring it.")
        return {}

    out: dict = {}
    dropped = 0
    for key, values in raw.items():
        if not isinstance(key, str) or not isinstance(values, (list, tuple)):
            dropped += 1
            continue
        seen, keep = set(), []
        for v in values:
            tag = sanitize_tag(v)
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                keep.append(tag)
            elif not tag:
                dropped += 1
        if keep:
            out[key] = keep
    if dropped:
        log(f"{TAGS_FILE}: ignored {dropped} malformed or empty tag entries")
    if out:
        total = sum(len(v) for v in out.values())
        log(f"Loaded {total} tags across {len(out)} images from {TAGS_FILE}")
    return out


def percentile(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, idx))]


def apply_calibration(cal: dict) -> None:
    """Overwrite the module-level tunables with calibrated values."""
    g = globals()
    for key, value in cal.items():
        if key not in CALIBRATABLE:
            continue
        if key == "VERDICT_BANDS":
            g[key] = [(float(t), str(l)) for t, l in value]
        elif key.endswith("_RANGE"):
            g[key] = (float(value[0]), float(value[1]))
        else:
            g[key] = float(value)


def load_calibration(out_dir: Path) -> Optional[dict]:
    path = out_dir / CALIBRATION_FILE
    if not path.exists():
        return None
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"Ignoring unreadable {CALIBRATION_FILE}: {exc}")
        return None
    apply_calibration(cal)
    ar, nr = AESTHETIC_RANGE, NIMA_RANGE
    log(f"Using calibration from {path.name}: aesthetic {ar[0]:.2f}-{ar[1]:.2f}, "
        f"nima {nr[0]:.2f}-{nr[1]:.2f}, "
        f"bands {[f'{t:.0f}' for t, _ in VERDICT_BANDS[:-1]]}")
    return cal


def result_from_row(row) -> PhotoResult:
    """Rebuild a PhotoResult from a cached database row (no image decoding)."""
    res = PhotoResult(
        path=row["path"], rel_path=row["rel_path"] or "", folder=row["folder"] or "",
        filename=row["filename"] or "", mtime=row["mtime"] or 0.0, size=row["size"] or 0,
        aesthetic_raw=row["aesthetic_raw"], nima_raw=row["nima_raw"],
        subject_score=row["subject_score"] or 0.0,
        subject_label=row["subject_label"] or "", subject_tier=row["subject_tier"] or "",
        sharpness=row["sharpness"] or 0.0,
        clip_hi=row["clip_hi"] or 0.0, clip_lo=row["clip_lo"] or 0.0,
        taken_at=row["taken_at"] if "taken_at" in row.keys() else None,
        source_type=row["source_type"] or "photo",
        source_video=row["source_video"], timestamp_s=row["timestamp_s"],
        extracted_path=row["extracted_path"], dup_of=row["dup_of"],
    )
    res.phash = int(row["phash"], 16) if row["phash"] else 0
    return res


def run_recompute(cache: "Cache") -> int:
    """Rebuild composite / verdict / note for every cached row. Seconds, not hours."""
    rows = [r for r in cache.all_rows() if not r["error"] and r["aesthetic_raw"] is not None]
    for row in rows:
        res = result_from_row(row)
        compose(res)
        cache.update_scores(res.path, res.composite, res.verdict, res.note)
    cache.commit()
    log(f"Recomputed scores for {len(rows)} cached rows (no models loaded)")
    return len(rows)


# What share of your keepers should land in each band. Top 5% become TOP PICK,
# the next 15% STRONG, the next 30% MAYBE, the bottom half PASS.
BAND_QUANTILES = [(0.95, "TOP PICK"), (0.80, "STRONG"), (0.50, "MAYBE")]

# Smallest range --calibrate will ever produce, on the models' ~1-10 scale.
MIN_RANGE_SPAN = 0.25

# Calibration happens by itself at the end of a scoring run. The shipped score
# ranges are guesses made before seeing any real library, so leaving them in
# place produces squashed scores and an empty top band - which is a bad default
# to hand someone. Re-fitting is cheap (no models load), so it just happens.
AUTO_CALIBRATE_MIN_KEEPERS = 30   # too few and the percentiles are meaningless
AUTO_CALIBRATE_GROWTH = 0.10      # re-fit once the scored pool moves by 10%


def run_calibrate(cache: "Cache", out_dir: Path) -> Optional[dict]:
    """
    Derive normalisation ranges and band cutoffs from what this library actually
    scored, then write them to calibration.json and apply them.

    The shipped defaults assume model outputs spread across a range they rarely
    reach in practice - real LAION-Aesthetic values on a personal library cluster
    far tighter than its nominal 1-10. Stretching the observed spread across
    0-100 restores the discrimination that assumption throws away.
    """
    rows = [r for r in cache.all_rows()
            if not r["error"] and not r["dup_of"] and r["aesthetic_raw"] is not None]
    if len(rows) < 30:
        log(f"Only {len(rows)} scored keepers - need at least 30 to calibrate. "
            f"Score more folders first.")
        return None

    aes = sorted(r["aesthetic_raw"] for r in rows)
    nima = sorted(r["nima_raw"] for r in rows if r["nima_raw"] is not None)

    def spread(values: list) -> list:
        """
        2nd-99th percentile, widened if degenerate.

        A library where a model returns nearly the same value everywhere would
        otherwise yield lo == hi, which makes the axis meaningless and, before
        the guard in rescale(), crashed every later run from the saved
        calibration file.
        """
        lo, hi = percentile(values, 0.02), percentile(values, 0.99)
        if hi - lo < MIN_RANGE_SPAN:
            mid = (hi + lo) / 2.0
            lo, hi = mid - MIN_RANGE_SPAN / 2.0, mid + MIN_RANGE_SPAN / 2.0
            log(f"  note: observed values span almost nothing; widening the range "
                f"to {lo:.2f}-{hi:.2f} so scores stay meaningful")
        return [lo, hi]

    cal: dict = {"AESTHETIC_RANGE": spread(aes)}
    if len(nima) >= 30:
        cal["NIMA_RANGE"] = spread(nima)

    log(f"Calibrating on {len(rows)} keepers")
    log(f"  aesthetic: was {AESTHETIC_RANGE[0]:.2f}-{AESTHETIC_RANGE[1]:.2f}, "
        f"observed {aes[0]:.2f}-{aes[-1]:.2f}, "
        f"now {cal['AESTHETIC_RANGE'][0]:.2f}-{cal['AESTHETIC_RANGE'][1]:.2f}")
    if "NIMA_RANGE" in cal:
        log(f"  technical: was {NIMA_RANGE[0]:.2f}-{NIMA_RANGE[1]:.2f}, "
            f"observed {nima[0]:.2f}-{nima[-1]:.2f}, "
            f"now {cal['NIMA_RANGE'][0]:.2f}-{cal['NIMA_RANGE'][1]:.2f}")

    # Apply the new ranges, then read off band cutoffs from the resulting spread.
    apply_calibration(cal)
    composites = sorted(_composite_only(result_from_row(r)) for r in rows)
    bands = [[float(percentile(composites, q)), label] for q, label in BAND_QUANTILES]
    bands.append([0.0, "PASS"])
    # Guard against ties collapsing two bands into one.
    for i in range(len(bands) - 2, -1, -1):
        if bands[i][0] <= bands[i + 1][0]:
            bands[i][0] = bands[i + 1][0] + 0.1
    cal["VERDICT_BANDS"] = bands
    apply_calibration(cal)

    log(f"  bands: TOP PICK >= {bands[0][0]:.1f}, STRONG >= {bands[1][0]:.1f}, "
        f"MAYBE >= {bands[2][0]:.1f}")

    # Remember how big the pool was, so later runs can tell when it has moved
    # enough to be worth re-fitting.
    cal["_keepers_at_calibration"] = len(rows)

    # JSON has no comments, so the explanation goes in as data. Keys the script
    # does not recognise are ignored by apply_calibration(), so this is inert -
    # it exists purely so the file explains itself when you open it in a year.
    described = {
        "_readme": [
            "Photo Scout's scoring scale, fitted to THIS library. Written",
            "automatically - you should not need to edit it by hand.",
            "",
            "AESTHETIC_RANGE / NIMA_RANGE: the low and high raw values the two",
            "  models actually produce on your photographs. The shipped defaults",
            "  are guesses; on a real library they are usually too wide, which",
            "  squashes every score into the middle and can yield no TOP PICKs",
            "  at all. These map the real spread onto 0-100.",
            "",
            "VERDICT_BANDS: the composite score at which each verdict starts,",
            "  set by proportion - roughly the top 5% TOP PICK, next 15% STRONG,",
            "  next 30% MAYBE, the rest PASS.",
            "",
            "_keepers_at_calibration: how many photographs were scored when this",
            "  was fitted. A later run re-fits once the pool has moved by more",
            "  than 10%, and leaves it alone otherwise.",
            "",
            "Delete this file to go back to the shipped defaults; the next run",
            "will fit a fresh one. Deleting it changes nothing about your",
            "photographs or their raw scores - only how those scores are banded.",
        ],
    }
    described.update(cal)
    (out_dir / CALIBRATION_FILE).write_text(
        json.dumps(described, indent=2), encoding="utf-8")
    log(f"  written to {out_dir / CALIBRATION_FILE} (delete it to revert to defaults)")
    return cal


def _composite_only(res: PhotoResult) -> float:
    compose(res)
    return res.composite


def auto_calibrate(cache: "Cache", out_dir: Path, previous: Optional[dict]) -> bool:
    """
    Re-fit the scale automatically at the end of a scoring run, when it matters.

    Runs if there is no calibration yet, or if the number of scored keepers has
    moved by more than AUTO_CALIBRATE_GROWTH since the last fit - which is the
    case that actually bites: bands fitted to 200 photos are wrong once 3,500
    are in the database.
    """
    keepers = cache.keeper_count()
    if keepers < AUTO_CALIBRATE_MIN_KEEPERS:
        log(f"Skipping calibration: only {keepers} scored keepers "
            f"(need {AUTO_CALIBRATE_MIN_KEEPERS}). Scores use the shipped defaults, "
            f"which tend to squash everything into the middle - score more folders.")
        return False

    was = (previous or {}).get("_keepers_at_calibration")
    if was:
        growth = abs(keepers - was) / max(was, 1)
        if growth < AUTO_CALIBRATE_GROWTH:
            log(f"Calibration still fits ({keepers} keepers vs {was} when last "
                f"fitted). Leaving it alone.")
            return False
        log(f"Re-calibrating: {keepers} keepers now, {was} when last fitted "
            f"({growth * 100:.0f}% change)")
    else:
        log("No calibration yet - fitting the score scale to this library")

    if run_calibrate(cache, out_dir) is None:
        return False
    run_recompute(cache)
    return True


# ---------------------------------------------------------------------------
# STAGE 5 - Resume cache. Re-running skips anything already scored.
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    path          TEXT PRIMARY KEY,
    rel_path      TEXT,
    folder        TEXT,
    filename      TEXT,
    mtime         REAL,
    size          INTEGER,
    phash         TEXT,          -- 16-char hex; SQLite INTEGER is signed 64-bit
                                 -- and a dhash can exceed its positive range
    aesthetic_raw REAL,
    nima_raw      REAL,
    subject_score REAL,
    subject_label TEXT,
    subject_tier  TEXT,
    sharpness     REAL,
    clip_hi       REAL,
    clip_lo       REAL,
    composite     REAL,
    verdict       TEXT,
    note          TEXT,
    dup_of        TEXT,
    error         TEXT,
    scored_at     REAL,
    source_type    TEXT DEFAULT 'photo',
    source_video   TEXT,
    timestamp_s    REAL,
    extracted_path TEXT,
    taken_at       TEXT,
    width          INTEGER,   -- native pixel dimensions of the source file.
    height         INTEGER    -- Reported to you; never used in a score.
);
"""

# Indexes are created only AFTER migrations run. An index can reference a column
# added by a migration, and on a pre-existing table CREATE TABLE IF NOT EXISTS is
# a no-op - so building indexes first would fail on any older database.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_folder    ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_composite ON photos(composite DESC);
CREATE INDEX IF NOT EXISTS idx_source    ON photos(source_type);
"""

# Columns added after the first release. Existing databases are upgraded in
# place so a library already scored with v1 keeps every result.
MIGRATIONS = [
    ("source_type", "TEXT DEFAULT 'photo'"),
    ("source_video", "TEXT"),
    ("timestamp_s", "REAL"),
    ("extracted_path", "TEXT"),
    ("taken_at", "TEXT"),
    # NULL on every row scored before this column existed, which reads as
    # "resolution unknown". Nothing rejects an unknown, so an old database keeps
    # showing everything it already had until a --force rescore fills them in.
    ("width", "INTEGER"),
    ("height", "INTEGER"),
]


class Cache:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(SCHEMA_INDEXES)
        self.conn.commit()

    def _migrate(self) -> None:
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(photos)")}
        for col, decl in MIGRATIONS:
            if col not in existing:
                log(f"Upgrading database: adding column '{col}'")
                self.conn.execute(f"ALTER TABLE photos ADD COLUMN {col} {decl}")
        self.conn.execute(
            "UPDATE photos SET source_type = 'photo' WHERE source_type IS NULL")

    def already_done(self, path: str, mtime: float, size: int) -> bool:
        row = self.conn.execute(
            "SELECT mtime, size, error FROM photos WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return False
        # Re-score if the file changed on disk since last time.
        return abs(row["mtime"] - mtime) < 1.0 and row["size"] == size and row["error"] is None

    def upsert(self, r: PhotoResult) -> None:
        self.conn.execute(
            """INSERT INTO photos
               (path, rel_path, folder, filename, mtime, size, phash, aesthetic_raw,
                nima_raw, subject_score, subject_label, subject_tier, sharpness,
                clip_hi, clip_lo, composite, verdict, note, dup_of, error, scored_at,
                source_type, source_video, timestamp_s, extracted_path, taken_at,
                width, height)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 mtime=excluded.mtime, size=excluded.size, phash=excluded.phash,
                 aesthetic_raw=excluded.aesthetic_raw, nima_raw=excluded.nima_raw,
                 subject_score=excluded.subject_score, subject_label=excluded.subject_label,
                 subject_tier=excluded.subject_tier, sharpness=excluded.sharpness,
                 clip_hi=excluded.clip_hi, clip_lo=excluded.clip_lo,
                 composite=excluded.composite, verdict=excluded.verdict, note=excluded.note,
                 dup_of=excluded.dup_of, error=excluded.error, scored_at=excluded.scored_at,
                 source_type=excluded.source_type, source_video=excluded.source_video,
                 timestamp_s=excluded.timestamp_s, taken_at=excluded.taken_at,
                 width=excluded.width, height=excluded.height
            """,
            (r.path, r.rel_path, r.folder, r.filename, r.mtime, r.size, f"{r.phash:016x}",
             r.aesthetic_raw, r.nima_raw, r.subject_score, r.subject_label, r.subject_tier,
             r.sharpness, r.clip_hi, r.clip_lo, r.composite, r.verdict, r.note,
             r.dup_of, r.error, time.time(),
             r.source_type, r.source_video, r.timestamp_s, r.extracted_path,
             r.taken_at, r.width, r.height),
        )

    def forget(self, path: str) -> int:
        """
        Drop one row. Used when a file that was scored by an earlier run no
        longer qualifies - it fell below the size floor after --min-edge was
        raised, say. Without this its stale row would keep appearing in reports
        forever, since a skipped file is never re-scored and so never updated.
        """
        cur = self.conn.execute("DELETE FROM photos WHERE path = ?", (path,))
        return cur.rowcount

    def forget_video(self, video_path: str) -> int:
        """Drop every sampled frame belonging to one clip. Same reason as forget()."""
        cur = self.conn.execute(
            "DELETE FROM photos WHERE source_video = ? OR path = ?",
            (video_path, video_path))
        return cur.rowcount

    def commit(self):
        self.conn.commit()

    def all_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM photos ORDER BY composite DESC"
        ).fetchall()

    def hashes_for_dedup(self) -> list[tuple[str, int, float]]:
        return [
            (r["path"], int(r["phash"], 16), r["composite"] or 0.0)
            for r in self.conn.execute(
                "SELECT path, phash, composite FROM photos WHERE error IS NULL AND phash IS NOT NULL"
            )
        ]

    def set_dup(self, path: str, dup_of: Optional[str]) -> None:
        self.conn.execute("UPDATE photos SET dup_of = ? WHERE path = ?", (dup_of, path))

    def update_scores(self, path: str, composite: float, verdict: str, note: str) -> None:
        self.conn.execute(
            "UPDATE photos SET composite = ?, verdict = ?, note = ? WHERE path = ?",
            (composite, verdict, note, path),
        )

    def set_extracted(self, path: str, dest: Optional[str]) -> None:
        self.conn.execute("UPDATE photos SET extracted_path = ? WHERE path = ?", (dest, path))

    def keeper_count(self) -> int:
        """Scored, non-duplicate, error-free rows - the pool calibration fits to."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM photos "
            "WHERE error IS NULL AND dup_of IS NULL AND aesthetic_raw IS NOT NULL"
        ).fetchone()[0]

    def frames_already_scored(self, video_path: str) -> int:
        """How many sampled frames of this clip are already in the database."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM photos WHERE source_video = ? AND error IS NULL",
            (video_path,),
        ).fetchone()[0]

    def extraction_candidates(self) -> list[sqlite3.Row]:
        """Surviving (non-duplicate) video frames that scored well enough to export."""
        return self.conn.execute(
            """SELECT * FROM photos
               WHERE source_type = 'video_frame'
                 AND dup_of IS NULL AND error IS NULL
                 AND composite >= ?
               ORDER BY source_video, composite DESC""",
            (VIDEO_EXTRACT_MIN_SCORE,),
        ).fetchall()


# ---------------------------------------------------------------------------
# STAGE 6 - Walk, score, dedupe
# ---------------------------------------------------------------------------

def iter_media(root: Path, only_folder: Optional[str], include_video: bool = True) -> Iterable[Path]:
    """Walk for stills and video together - one pass over the library."""
    wanted = MEDIA_EXTENSIONS if include_video else IMAGE_EXTENSIONS
    base = root / only_folder if only_folder else root
    warned_venv = False
    for dirpath, dirnames, filenames in os.walk(base):
        # A directory holding pyvenv.cfg is the root of a Python virtual
        # environment. Prune its machinery but keep walking everything else -
        # people do sometimes create the venv inside the library itself.
        if "pyvenv.cfg" in filenames:
            venv_dirs = [d for d in dirnames if d.lower() in VENV_SUBDIRS]
            if venv_dirs:
                if not warned_venv:
                    log(f"Note: a Python virtual environment lives in {dirpath} - "
                        f"skipping {', '.join(sorted(venv_dirs))} so package assets "
                        f"aren't scored as photographs.")
                    warned_venv = True
                dirnames[:] = [d for d in dirnames if d.lower() not in VENV_SUBDIRS]

        # Hidden folders are pruned here so nothing inside is ever opened, let
        # alone scored. The report also filters on the path (see build_reports),
        # which is what makes hiding work on photographs scored earlier.
        hidden = [d for d in dirnames if d.strip().lower() == HIDE_DIR_NAME]
        if hidden:
            for d in hidden:
                log(f"Hidden:    skipping {Path(dirpath) / d} and everything in it")
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIR_NAMES
                       and d.strip().lower() != HIDE_DIR_NAME]
        for name in sorted(filenames):
            if Path(name).suffix.lower() in wanted:
                yield Path(dirpath) / name


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def make_thumb(img: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    t = img.copy()
    t.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    t.save(dest, "JPEG", quality=82, optimize=True)


def make_preview(img: Image.Image, dest: Path) -> None:
    """The image the lightbox displays. A browser cannot render a NEF."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = img.copy()
    if max(p.size) > PREVIEW_SIZE:
        p.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.LANCZOS)
    p.save(dest, "JPEG", quality=PREVIEW_QUALITY, optimize=True)


def thumb_name(path) -> str:
    """Stable thumbnail filename. Accepts a real path or a virtual frame path."""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:20] + ".jpg"


def _score_one(img: Image.Image, res: PhotoResult, scorer, out_dir: Path,
               want_thumb: bool, want_preview: bool) -> None:
    """
    Run every measurement on one decoded image and write its derivatives.

    `img` arrives at PREVIEW_SIZE so the lightbox has something worth looking at.
    Scoring happens on a SCORING_SIZE copy, which keeps every measurement
    identical to what it would be without previews enabled.
    """
    if want_preview:
        make_preview(img, out_dir / "previews" / thumb_name(res.path))
    if want_thumb:
        make_thumb(img, out_dir / "thumbs" / thumb_name(res.path))

    # --- Resolution is deliberately excluded from every measurement ---------
    # Each image below is normalised to the SAME canvas - longest side exactly
    # SCORING_SIZE - so that how many pixels a file happens to contain cannot
    # move its score. That is a policy, not an accident:
    #
    #   * NIMA is handed these pixels directly and its output shifts with input
    #     size, so a 45 MP body would quietly out-score a 12 MP one on identical
    #     scenes.
    #   * Laplacian variance is scale-sensitive by nature: measure the same
    #     photograph at two sizes and you get two "sharpness" figures.
    #   * CLIP resizes to 224 internally and was already immune.
    #
    # Resolution is a hard fact about a file, not a component of how good the
    # photograph is. It is recorded and shown beside the image so the
    # photographer decides whether it is enough for the intended use; the models
    # keep judging what they are actually good at - composition, light, blur,
    # exposure, subject.
    #
    # The downscale path is byte-for-byte what earlier versions did, so scores
    # in an existing database do not shift. The upscale branch is new and only
    # ever runs on images smaller than the canvas; anything reaching here has
    # already cleared MIN_IMAGE_EDGE, so it is at most a couple of per cent.
    scoring = img
    if max(img.size) > SCORING_SIZE:
        scoring = img.copy()
        scoring.thumbnail((SCORING_SIZE, SCORING_SIZE), Image.LANCZOS)
    elif max(img.size) < SCORING_SIZE:
        f = SCORING_SIZE / float(max(img.size))
        scoring = img.resize((max(1, round(img.width * f)),
                              max(1, round(img.height * f))), Image.LANCZOS)

    # img.info survives the copy/thumbnail above, but read it from the
    # original to be explicit about where the metadata came from.
    res.taken_at = img.info.get("psc_taken")
    native = img.info.get("psc_native")
    if native and not res.width:
        res.width, res.height = int(native[0]), int(native[1])
    res.phash = perceptual_hash(scoring)
    res.sharpness = sharpness_variance(scoring)
    res.clip_hi, res.clip_lo = clipping_fraction(scoring)
    res.__dict__.update(scorer.score(scoring))
    compose(res)


def run_scoring(root: Path, out_dir: Path, cache: Cache, args) -> None:
    include_video = not args.no_video
    paths = list(iter_media(root, args.folder, include_video=include_video))
    stills = [p for p in paths if not is_video(p)]
    videos = [p for p in paths if is_video(p)]
    log(f"Found {len(stills)} images and {len(videos)} videos under {args.folder or root}")

    if videos and not have_ffmpeg():
        log("ffmpeg/ffprobe not found on PATH - skipping video. "
            "Install from https://ffmpeg.org/download.html to enable it.")
        videos = []

    min_edge = getattr(args, "min_edge", MIN_IMAGE_EDGE)

    todo = []
    too_small = 0
    for p in paths:
        if p in videos or not is_video(p):
            try:
                st = p.stat()
            except OSError as exc:
                log(f"  cannot stat {p}: {exc}")
                continue
            # The size floor is checked BEFORE the resume cache, and on every
            # run, so raising or lowering --min-edge takes effect immediately
            # instead of only for files that happen to be new. A header read is
            # a fraction of a millisecond, which is why it can afford to be
            # unconditional. Videos are probed later, in the scoring loop, where
            # the ffprobe call is already being paid for.
            if not is_video(p) and below_size_floor(read_dimensions(p), min_edge):
                too_small += 1
                # A previous run with a lower floor may have scored it. Drop
                # that row, or it would haunt the report indefinitely.
                if cache.forget(str(p)):
                    cache.commit()
                continue
            if not args.force and cache.already_done(str(p), st.st_mtime, st.st_size):
                continue
            # A video is "done" if we already have frames for it and it hasn't changed.
            if is_video(p) and not args.force and cache.frames_already_scored(str(p)) > 0:
                continue
            todo.append((p, st))

    if too_small:
        log(f"Skipped {too_small} images under the {min_edge}px size floor "
            f"(icons, emoji, memes, web thumbnails). Pass --min-edge 0 to score them.")

    if args.limit:
        todo = todo[: args.limit]

    n_todo_videos = sum(1 for p, _ in todo if is_video(p))
    log(f"{len(paths) - len(todo) - too_small} already scored, {len(todo)} to do "
        f"({n_todo_videos} of them videos)")
    if not todo:
        return

    # Fail fast and legibly on a torch/torchvision mismatch, rather than after
    # a minute of model loading with an error that blames CLIP.
    check_torchvision_compat()

    scorer = Scorer(device=args.device, use_nima=not args.no_nima)

    frame_work = out_dir / "_frame_tmp"
    started = time.time()
    units_done = 0  # counts scored images, so video clips advance it by their frame count

    for i, (p, st) in enumerate(todo, 1):
        rel = p.relative_to(root)
        folder = str(rel.parent) if str(rel.parent) != "." else "(root)"

        # ---------------- video: expand into sampled frames -----------------
        if is_video(p):
            # The clip's own resolution, not the downscaled proxy frames': an
            # extracted still comes out at native size, so that is the number
            # the floor should judge and the number worth reporting.
            vsize = probe_video_size(p)
            if below_size_floor(vsize, min_edge):
                too_small += 1
                if cache.forget_video(str(p)):
                    cache.commit()
                log(f"  [{i}/{len(todo)}] {p.name}: skipped, "
                    f"{vsize[0]}x{vsize[1]} is under the {min_edge}px floor")
                continue
            try:
                duration = probe_duration(p)
                frames = sample_video_frames(p, frame_work, every=args.video_every)
            except Exception as exc:
                bad = PhotoResult(path=str(p), rel_path=str(rel), folder=folder,
                                  filename=p.name, mtime=st.st_mtime, size=st.st_size,
                                  source_type="video_frame", source_video=str(p),
                                  error=f"{type(exc).__name__}: {exc}")
                cache.upsert(bad); cache.commit()
                log(f"  [{i}/{len(todo)}] {p.name}: FAILED to sample - {exc}")
                if args.verbose:
                    traceback.print_exc()
                continue

            log(f"  [{i}/{len(todo)}] {p.name}: "
                f"{hhmmss(duration) if duration else '?'} -> {len(frames)} frames "
                f"every {args.video_every:g}s")

            kept = 0
            for ts, jpg in frames:
                vpath = make_virtual_path(p, ts)
                res = PhotoResult(
                    path=vpath,
                    rel_path=f"{rel}{VIRTUAL_SEP}{ts:.3f}",
                    folder=folder,
                    filename=f"{p.name} @ {hhmmss(ts)}",
                    mtime=st.st_mtime,
                    size=st.st_size,
                    source_type="video_frame",
                    source_video=str(p),
                    timestamp_s=ts,
                    # The clip's dimensions, because that is what an extracted
                    # still will be. The proxy JPEG being scored is smaller.
                    width=vsize[0] if vsize else None,
                    height=vsize[1] if vsize else None,
                )
                try:
                    img = Image.open(jpg); img.load(); img = img.convert("RGB")
                    _score_one(img, res, scorer, out_dir,
                               not args.no_thumbs, not args.no_previews)
                    kept += 1
                except Exception as exc:
                    res.error = f"{type(exc).__name__}: {exc}"
                    if args.verbose:
                        traceback.print_exc()
                cache.upsert(res)
                units_done += 1

            cache.commit()
            log(f"      scored {kept}/{len(frames)} frames")
            continue

        # ---------------- still photograph ----------------------------------
        res = PhotoResult(
            path=str(p),
            rel_path=str(rel),
            folder=folder,
            filename=p.name,
            mtime=st.st_mtime,
            size=st.st_size,
        )
        try:
            img = load_image(p, PREVIEW_SIZE if not args.no_previews else SCORING_SIZE)
            # Backstop for the formats read_dimensions() cannot measure without
            # decoding - RAW, in practice. Reaching this is close to unheard of,
            # but a decoded size is authoritative and costs nothing to check.
            if below_size_floor(img.info.get("psc_native"), min_edge):
                w, h = img.info["psc_native"]
                log(f"  [{i}/{len(todo)}] {p.name}: skipped, "
                    f"{w}x{h} is under the {min_edge}px floor")
                cache.forget(str(p))
                cache.commit()
                continue
            _score_one(img, res, scorer, out_dir,
                       not args.no_thumbs, not args.no_previews)
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"
            if args.verbose:
                traceback.print_exc()

        cache.upsert(res)
        units_done += 1

        if i % 25 == 0 or i == len(todo):
            cache.commit()
            elapsed = time.time() - started
            rate = units_done / max(elapsed, 1e-6)
            eta = (len(todo) - i) / max(i / max(elapsed, 1e-6), 1e-6)
            log(f"  {i}/{len(todo)}  {rate:.1f} img/s  ETA {eta/60:.1f} min  "
                f"last: {p.name} -> {res.verdict or 'ERROR'}")

    cache.commit()

    # Sampled frames were only ever scoring proxies; the real stills come later.
    if frame_work.exists():
        import shutil as _sh
        _sh.rmtree(frame_work, ignore_errors=True)


def run_extraction(cache: Cache, out_dir: Path, fmt: str = VIDEO_EXTRACT_FORMAT) -> int:
    """
    Re-extract the surviving high-scoring video frames from their source clips at
    full native resolution, as real still files you can actually work with.

    Runs after dedup, so a locked-off tripod shot contributes its single best
    frame rather than forty copies of the same composition.
    """
    candidates = cache.extraction_candidates()
    if not candidates:
        return 0

    dest_root = out_dir / "extracted_stills"
    per_video: dict[str, int] = {}
    written = 0
    skipped_cap = 0

    for row in candidates:
        video = row["source_video"]
        n = per_video.get(video, 0)
        if n >= VIDEO_EXTRACT_MAX_PER_VIDEO:
            skipped_cap += 1
            continue

        ts = row["timestamp_s"] or 0.0
        vpath = Path(video)
        dest = dest_root / vpath.stem / f"{vpath.stem}_{ts:08.2f}s_{row['composite']:.0f}.{fmt}"

        if dest.exists() and dest.stat().st_size > 0:
            cache.set_extracted(row["path"], str(dest))
            per_video[video] = n + 1
            continue

        if extract_still(vpath, ts, dest):
            cache.set_extracted(row["path"], str(dest))
            per_video[video] = n + 1
            written += 1
        else:
            log(f"  could not extract {vpath.name} @ {hhmmss(ts)}")

    cache.commit()
    log(f"Extracted {written} full-resolution stills to {dest_root}")
    if skipped_cap:
        log(f"  ({skipped_cap} further frames scored high enough but hit the "
            f"per-clip cap of {VIDEO_EXTRACT_MAX_PER_VIDEO}; raise "
            f"VIDEO_EXTRACT_MAX_PER_VIDEO to keep more)")
    return written


def run_dedup(cache: Cache) -> int:
    """
    Group near-identical frames and keep only the highest-scoring one as the
    representative. Duplicates stay in the database (flagged) so nothing is
    hidden from you permanently - they're just filtered out of the main report.
    """
    rows = cache.hashes_for_dedup()
    log(f"Deduplicating {len(rows)} scored images (hamming <= {PHASH_HAMMING_THRESHOLD})")

    # Banded LSH so we don't do N^2 comparisons on 3500+ images.
    #
    # Split each 64-bit hash into BANDS equal slices. If two hashes differ by at
    # most T bits, then at most T bands can contain a differing bit, so with
    # BANDS > T at least one band must match exactly. Indexing on exact band
    # equality therefore cannot miss a true pair (pigeonhole principle) - which
    # a single high-bits bucket very much can.
    BANDS = max(PHASH_HAMMING_THRESHOLD + 3, 8)
    band_bits = 64 // BANDS
    band_mask = (1 << band_bits) - 1

    buckets: dict[tuple[int, int], list[tuple[str, int, float]]] = {}
    for path, ph, comp in rows:
        for b in range(BANDS):
            key = (b, (ph >> (b * band_bits)) & band_mask)
            buckets.setdefault(key, []).append((path, ph, comp))

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    compared: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        if len(bucket) > 400:
            # Pathological bucket (e.g. thousands of near-black frames). Comparing
            # it fully would be quadratic; say so rather than silently skipping.
            log(f"  note: skipping an oversized similarity bucket of {len(bucket)} "
                f"images - some near-duplicates there may not be grouped")
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                pa, pb = bucket[i][0], bucket[j][0]
                pair = (pa, pb) if pa < pb else (pb, pa)
                if pair in compared:
                    continue
                compared.add(pair)
                if hamming64(bucket[i][1], bucket[j][1]) <= PHASH_HAMMING_THRESHOLD:
                    parent.setdefault(pa, pa)
                    parent.setdefault(pb, pb)
                    union(pa, pb)

    groups: dict[str, list[tuple[str, float]]] = {}
    score_by_path = {p: c for p, _, c in rows}
    for path in parent:
        groups.setdefault(find(path), []).append((path, score_by_path.get(path, 0.0)))

    dup_count = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[1], reverse=True)
        keeper = members[0][0]
        cache.set_dup(keeper, None)
        for path, _ in members[1:]:
            cache.set_dup(path, keeper)
            dup_count += 1
    cache.commit()
    log(f"Flagged {dup_count} near-duplicate frames")
    return dup_count


# ---------------------------------------------------------------------------
# STAGE 7 - Reports
# ---------------------------------------------------------------------------

def _to_url_path(p: str) -> str:
    """
    Normalise a filesystem path into the path portion of a file:/// URL.

    Done with plain string ops rather than pathlib on purpose: the report may be
    generated on one OS and the paths may use the other's separator, and
    PurePath silently picks the wrong flavour when they disagree.
    """
    s = p.replace("\\", "/").lstrip("/")
    # Percent-encode the characters that actually break href parsing.
    for ch, enc in ((" ", "%20"), ("#", "%23"), ("?", "%3F"), ("%", "%25")):
        if ch == "%":
            continue  # skip; encoding % first would double-encode the others
        s = s.replace(ch, enc)
    return s


def win_file_url(path: str) -> str:
    """file:/// URL that opens the file itself."""
    return "file:///" + _to_url_path(path)


def win_folder_url(path: str) -> str:
    """file:/// URL for the containing folder - opens it in Explorer."""
    s = path.replace("\\", "/")
    parent = s.rsplit("/", 1)[0] if "/" in s else s
    return "file:///" + _to_url_path(parent)


def write_csv(rows, dest: Path, tags: Optional[dict] = None) -> None:
    cols = ["verdict", "composite", "folder", "filename", "taken_at", "note", "source_type",
            "width", "height",
            "timestamp_s", "extracted_path", "subject_tier", "subject_label",
            "aesthetic_raw", "nima_raw", "sharpness", "dup_of", "path", "source_video", "error"]
    tags = tags or {}

    def cell(row, col):
        # Tolerates rows from a database that predates a column: a stale CSV
        # beats a crash halfway through writing the report.
        try:
            return row[col]
        except (IndexError, KeyError):
            return None

    with open(dest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols + ["megapixels", "taken_date", "taken_time", "tags"])
        for r in rows:
            real = split_virtual_path(r["path"])[0]
            wpx, hpx = cell(r, "width"), cell(r, "height")
            w.writerow([cell(r, c) for c in cols]
                       + [f"{(wpx * hpx) / 1e6:.1f}" if wpx and hpx else "",
                          pretty_date(cell(r, "taken_at")),
                          pretty_time(cell(r, "taken_at")),
                          "; ".join(tags.get(r["path"], tags.get(real, [])))])


# Raw string: the embedded JavaScript contains regexes like /\s+/g, and in a
# normal string Python reads \s as an invalid escape (a SyntaxWarning today,
# a SyntaxError in a future version).
HTML_TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<!-- Without this a phone lays the page out at 980px and then shrinks it, so
     the grid never reflows and every control is too small to hit. -->
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Scout - photograph quality report</title>
<style>
 :root { color-scheme: dark; }
 body { background:#111; color:#e8e8e8; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; margin:0; }
 header { position:sticky; top:0; background:#181818; border-bottom:1px solid #333; padding:14px 20px; z-index:10; }
 h1 { margin:0 0 8px; font-size:18px; font-weight:600; }
 .stats { color:#9a9a9a; font-size:13px; margin-bottom:10px; }
 .stats b { color:#e8e8e8; }
 .controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
 button, select, input { background:#242424; color:#e8e8e8; border:1px solid #3a3a3a;
   border-radius:6px; padding:6px 11px; font-size:13px; cursor:pointer; }
 button.on { background:#2f6f4f; border-color:#3f8f68; }
 :root { --colw: 300px; }
 main { display:grid; grid-template-columns:repeat(auto-fill,minmax(var(--colw),1fr)); gap:14px; padding:18px; }
 /* On a phone the gutter, not the column width, is what keeps a third column
    off the screen: three 115px columns need 361px at 8px, 373px at 14px. */
 @media (max-width:600px) { main { gap:8px; padding:10px; } }
 /* Thumbnail size. Ctrl+scroll zooms the whole page; these reflow the grid,
    and give a touchscreen a way to do it at all. */
 #zoom { display:inline-flex; gap:4px; }
 #zoom button { min-width:34px; padding:6px 9px; font-size:15px; line-height:1; }
 #zoom button[disabled] { opacity:.35; cursor:default; }
 .card { background:#1b1b1b; border:1px solid #2c2c2c; border-radius:10px; overflow:hidden;
   display:flex; flex-direction:column; }
 .card img { width:100%; aspect-ratio:3/2; object-fit:cover; background:#000; display:block; }
 .body { padding:10px 12px 12px; }
 .top { display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom:5px; }
 .name { font-weight:600; font-size:13px; word-break:break-all; }
 .score { font-variant-numeric:tabular-nums; font-weight:700; }
 .badge { display:inline-block; font-size:10px; letter-spacing:.06em; padding:2px 7px;
   border-radius:99px; font-weight:700; }
 .TOP-PICK { background:#1e5f3f; color:#8ff0bd; }
 .STRONG   { background:#1f4a63; color:#9fd8f5; }
 .MAYBE    { background:#5a4a1c; color:#f0d79a; }
 .PASS     { background:#3a3a3a; color:#a5a5a5; }
 .VIDEO    { background:#4a2a5c; color:#dcb0f5; margin-right:5px; }
 .note { color:#b6b6b6; font-size:12.5px; margin:6px 0 8px; }
 /* A named fault is the one part of the line that is not on every card, so it
    is coloured to be scannable rather than read. */
 .note .flag { color:#d9a441; }
 /* Two muted lines, split by how long each field can get. The folder is
    unbounded free text, so it takes a line to itself and truncates. The facts
    below it wrap rather than clip, so none of them can be cut off. */
 .folder { color:#7d7d7d; font-size:11.5px; margin-bottom:2px;
   white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
 /* tabular-nums lines the figures up down a column of cards. */
 .specs { color:#6a6a6a; font-size:11px; margin-bottom:6px; line-height:1.45;
   font-variant-numeric:tabular-nums; }
 /* Each fact is atomic - a break may fall between them, never inside one. */
 .specs span { white-space:nowrap; }
 .card .folder:empty, .card .specs:empty { display:none; }
 .links a { color:#7fc4ff; text-decoration:none; font-size:12px; margin-right:12px; }
 .links a:hover { text-decoration:underline; }

 /* ---- tags ---------------------------------------------------------------
    Colour comes from a hash of the tag text, so a tag looks the same on every
    card and in the search box, and stays the same between sessions. */
 .tagwrap { margin:8px 0 2px; padding-top:8px; border-top:1px solid #2a2a2a; }
 .taglist { display:flex; flex-wrap:wrap; gap:5px; align-items:center; }
 .tag { display:inline-flex; align-items:center; gap:5px; font-size:11.5px;
   line-height:1; padding:4px 7px; border-radius:99px; white-space:nowrap;
   background:var(--tagbg); color:var(--tagfg); border:1px solid var(--tagbd); }
 .tag button { all:unset; cursor:pointer; font-size:13px; line-height:1;
   opacity:.65; padding:0 1px; }
 .tag button:hover { opacity:1; }
 .taginput { flex:1; min-width:96px; background:#141414; border:1px dashed #3a3a3a;
   color:#e8e8e8; border-radius:6px; padding:4px 7px; font-size:11.5px; }
 .taginput:focus { outline:none; border-color:#5a5a5a; border-style:solid; }
 .taginput::placeholder { color:#6a6a6a; }

 /* ---- tag search box ---------------------------------------------------- */
 #searchbox { position:relative; flex:1; min-width:260px; display:flex;
   flex-wrap:wrap; gap:5px; align-items:center; background:#242424;
   border:1px solid #3a3a3a; border-radius:6px; padding:4px 6px; }
 #searchbox.focus { border-color:#5a7f9a; }
 #chips { display:contents; }
 #q { flex:1; min-width:120px; background:transparent; border:none; padding:3px 2px;
   color:#e8e8e8; font-size:13px; }
 #q:focus { outline:none; }
 #tagmenu { position:absolute; top:calc(100% + 4px); left:0; right:0; z-index:30;
   background:#1d1d1d; border:1px solid #3a3a3a; border-radius:8px;
   max-height:260px; overflow:auto; display:none;
   box-shadow:0 10px 30px rgba(0,0,0,.6); }
 #tagmenu.open { display:block; }
 #tagmenu div { padding:7px 10px; cursor:pointer; font-size:13px;
   display:flex; align-items:center; gap:8px; }
 #tagmenu div.sel, #tagmenu div:hover { background:#2c2c2c; }
 #tagmenu .count { margin-left:auto; color:#8a8a8a; font-size:11.5px; }
 #tagmenu .delall { background:none; border:1px solid #4a3a3a; color:#c98a8a;
   border-radius:6px; font-size:11px; line-height:1; padding:4px 7px; cursor:pointer;
   margin-left:6px; flex:0 0 auto; }
 #tagmenu .delall:hover { background:#3a2626; border-color:#7a4a4a; color:#f0b4b4; }
 #tagmenu .none { color:#8a8a8a; cursor:default; }
 #tagmenu .none:hover { background:transparent; }
 .dup { opacity:.45; }
 .hidden { display:none !important; }
 footer { padding:24px; color:#777; font-size:12px; text-align:center; }
 footer a { color:#9a9a9a; text-decoration:none; border-bottom:1px solid #3a3a3a; }
 footer a:hover { color:#e8e8e8; border-bottom-color:#6a6a6a; }
 #toast { position:fixed; left:50%; bottom:26px; transform:translateX(-50%);
   background:#252525; border:1px solid #3d3d3d; color:#eee; padding:10px 16px;
   border-radius:8px; font-size:13px; opacity:0; pointer-events:none;
   transition:opacity .18s; max-width:78vw; z-index:120; }
 #toast.show { opacity:1; }
 /* The bar itself stays click-through so it never swallows a click meant for
    the page beneath it; only an action button inside it is clickable. */
 #toast button { pointer-events:auto; }
 .card img { cursor:zoom-in; }

 /* ---- lightbox ---- */
 #lb { position:fixed; inset:0; background:#0b0b0b; display:none;
   flex-direction:column; z-index:100; }
 #lb.open { display:flex; }
 #lb-bar { display:flex; align-items:center; gap:12px; padding:10px 16px;
   background:#141414; border-bottom:1px solid #2a2a2a; flex:0 0 auto; }
 #lb-bar .grow { flex:1; min-width:0; }
 #lb-name { font-weight:600; font-size:14px; white-space:nowrap; overflow:hidden;
   text-overflow:ellipsis; }
 #lb-sub { color:#9a9a9a; font-size:12px; white-space:nowrap; overflow:hidden;
   text-overflow:ellipsis; }
 /* Outside .grow and flex:none, so a long folder name in #lb-sub cannot
    squeeze the resolution out of the bar. */
 #lb-dims { flex:none; color:#8a8a8a; font-size:12px; white-space:nowrap;
   font-variant-numeric:tabular-nums; }
 #lb-bar button, #lb-bar a { background:#242424; color:#e8e8e8; border:1px solid #3a3a3a;
   border-radius:6px; padding:6px 11px; font-size:13px; cursor:pointer;
   text-decoration:none; white-space:nowrap; }
 #lb-bar button:hover, #lb-bar a:hover { background:#2f2f2f; }
 #lb-stage { flex:1; position:relative; overflow:auto; display:flex;
   align-items:center; justify-content:center; background:#0b0b0b; }
 #lb-img { display:block; max-width:100%; max-height:100%; object-fit:contain;
   cursor:zoom-in; }
 #lb-stage.actual { display:block; }
 #lb-stage.actual #lb-img { max-width:none; max-height:none; cursor:zoom-out; }
 #lb-note { padding:10px 16px; background:#141414; border-top:1px solid #2a2a2a;
   color:#c0c0c0; font-size:13px; flex:0 0 auto; }
 .lb-nav { position:absolute; top:50%; transform:translateY(-50%);
   background:rgba(20,20,20,.72); border:1px solid #3a3a3a; color:#eee;
   font-size:26px; line-height:1; padding:14px 18px; border-radius:10px;
   cursor:pointer; user-select:none; z-index:2; }
 .lb-nav:hover { background:rgba(45,45,45,.92); }
 #lb-prev { left:14px; } #lb-next { right:14px; }
 #lb-missing { color:#f0b0b0; font-size:14px; padding:40px; text-align:center;
   display:none; }
 #lb.no-image #lb-img { display:none; }
 #lb.no-image #lb-missing { display:block; }
</style>
<header>
  <h1>Photo Scout &mdash; photograph quality</h1>
  <div class="stats">__STATS__ &middot; <b id="shown"></b></div>
  <div class="controls">
    <button data-f="all" class="on">All</button>
    <button data-f="TOP PICK">Top picks</button>
    <button data-f="STRONG">Strong</button>
    <button data-f="MAYBE">Maybe</button>
    <button data-f="PASS">Pass</button>
    <select id="folder" style="margin-left:10px;max-width:340px">__FOLDER_OPTIONS__</select>
    <select id="sort" title="Sort order">
      <option value="score-desc">Score, highest first</option>
      <option value="score-asc">Score, lowest first</option>
      <option value="date-desc">Date, newest first</option>
      <option value="date-asc">Date, oldest first</option>
      <option value="folder-asc">Folder A-Z</option>
      <option value="folder-desc">Folder Z-A</option>
      <option value="name-asc">File name A-Z</option>
      <option value="name-desc">File name Z-A</option>
    </select>
    <select id="kind">
      <option value="all">Photos + video frames</option>
      <option value="photo">Photos only</option>
      <option value="video">Video frames only</option>
    </select>
    <label><input type="checkbox" id="dups"> show near-duplicates</label>
    <span id="zoom">
      <button id="smaller" type="button" aria-label="Smaller thumbnails"
              title="Smaller thumbnails, more per row">&minus;</button>
      <button id="bigger" type="button" aria-label="Larger thumbnails"
              title="Larger thumbnails, fewer per row">+</button>
    </span>
    <div id="searchbox">
      <span id="chips"></span>
      <input type="search" id="q" autocomplete="off"
             placeholder="filter by folder, filename or tag">
      <div id="tagmenu"></div>
    </div>
    <button id="savetags" title="Download tags.json so your tags survive a rebuild">
      Save tags</button>
  </div>
</header>
<div id="toast"></div>

<div id="lb">
  <div id="lb-bar">
    <button id="lb-close" title="Esc">&times;</button>
    <span id="lb-count" style="color:#8a8a8a;font-size:12px"></span>
    <span class="grow">
      <div id="lb-name"></div>
      <div id="lb-sub"></div>
    </span>
    <span id="lb-dims"></span>
    <button id="lb-zoom" title="Toggle 1:1 (Z)">1:1</button>
    <button id="lb-full" title="Full screen (F)">Full screen</button>
    <a id="lb-folder" href="#">open folder</a>
    <button id="lb-copy" title="Copy the full path">copy path</button>
  </div>
  <div id="lb-stage">
    <div id="lb-prev" class="lb-nav">&#8249;</div>
    <img id="lb-img" alt="">
    <div id="lb-missing">
      No preview was generated for this image.<br>
      Re-run without <code>--no-previews</code> to create one.
    </div>
    <div id="lb-next" class="lb-nav">&#8250;</div>
  </div>
  <div id="lb-note"></div>
</div>
<main id="grid">
__CARDS__
</main>
<footer>Generated by <a href="__PROJECT_URL__" target="_blank" rel="noopener">Photo Scout</a> &middot; scores are model estimates, not verdicts &mdash; trust your eye.&trade;</footer>
<script>
 const grid = document.getElementById('grid');
 const qEl = document.getElementById('q');
 const cards = [...grid.children];

 // ==== thumbnail size =======================================================
 // Ctrl+scroll zooms the page, which magnifies one column rather than showing
 // more. These reflow the grid, and are the only way to do it on a touchscreen.
 (function zoom(){
   const STEPS = [100, 115, 140, 160, 200, 240, 300, 380, 480, 620];
   const smaller = document.getElementById('smaller');
   const bigger = document.getElementById('bigger');
   const KEY = 'psc-colw:' + location.pathname;
   // One 300px column fills a phone, which is not a contact sheet. A narrow
   // screen therefore starts two-up, and any saved choice still wins.
   let at = STEPS.indexOf(innerWidth < 600 ? 160 : 300);
   if (at < 0) at = STEPS.indexOf(300);
   try {
     const saved = STEPS.indexOf(parseInt(localStorage.getItem(KEY), 10));
     if (saved >= 0) at = saved;
   } catch (e) {}
   function apply(){
     document.documentElement.style.setProperty('--colw', STEPS[at] + 'px');
     smaller.disabled = at === 0;
     bigger.disabled = at === STEPS.length - 1;
     try { localStorage.setItem(KEY, STEPS[at]); } catch (e) {}
   }
   smaller.addEventListener('click', () => { if (at > 0) { at--; apply(); } });
   bigger.addEventListener('click', () => {
     if (at < STEPS.length - 1) { at++; apply(); } });
   apply();
 })();

 // ==== tags =================================================================
 // State is { "<card key>": ["Lake Photos", ...] }, seeded from tags.json by the
 // Python side and mirrored into localStorage on every edit so tags survive a
 // reload and a report rebuild. localStorage can be cleared by the browser, so
 // "Save tags" downloads tags.json for the script to bake in permanently.
 const TAGS = __TAGS_JSON__;
 const STORE_KEY = '__STORAGE_KEY__';
 const TAG_MAX = 40;

 try {
   const saved = localStorage.getItem(STORE_KEY);
   if (saved) {
     const parsed = JSON.parse(saved);
     if (parsed && typeof parsed === 'object') {
       // localStorage is the newer copy: the file only changes when exported.
       for (const k in parsed) if (Array.isArray(parsed[k])) TAGS[k] = parsed[k];
     }
   }
 } catch (e) { /* private mode or file:// storage disabled - stay in memory */ }

 let storageWorks = true, dirty = false;
 const saveBtn = document.getElementById('savetags');
 function markDirty() {
   dirty = true;
   if (saveBtn) saveBtn.textContent = 'Save tags *';
 }
 function persist() {
   // tagsFor() creates an empty array for any card it is asked about, which is
   // harmless in memory but would otherwise litter localStorage and a saved
   // tags.json with one empty entry per photograph in the library.
   for (const k of Object.keys(TAGS)) if (!TAGS[k] || !TAGS[k].length) delete TAGS[k];
   try { localStorage.setItem(STORE_KEY, JSON.stringify(TAGS)); }
   catch (e) {
     if (storageWorks) {
       storageWorks = false;
       toast('This browser will not store tags - use "Save tags" before closing');
     }
   }
   markDirty();
 }
 window.addEventListener('beforeunload', e => {
   if (!dirty) return;
   e.preventDefault(); e.returnValue = '';
 });

 // Same rule as the Python side: letters, digits, space, underscore, hyphen.
 function cleanTag(raw) {
   // Order matters and must match sanitize_tag() in the Python side exactly:
   // whitespace collapses to a space first, so newlines and tabs separate words
   // instead of being deleted and welding them together.
   return (raw || '').replace(/\s+/g, ' ')
                     .replace(/[^A-Za-z0-9 _-]/g, '')
                     .replace(/\s+/g, ' ')
                     .replace(/^[\s\-_]+|[\s\-_]+$/g, '')
                     .slice(0, TAG_MAX)
                     .replace(/^[\s\-_]+|[\s\-_]+$/g, '').trim();
 }

 // Deterministic colour: the same text always gives the same hue, on every card
 // and in the search box, across sessions. Multiplying by the golden angle
 // spreads similar strings far apart on the colour wheel.
 function tagHue(name) {
   let h = 0;
   const t = name.toLowerCase();
   for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) >>> 0;
   return Math.round((h * 137.508) % 360);
 }
 function paintTag(el, name) {
   const hue = tagHue(name);
   el.style.setProperty('--tagbg', 'hsl(' + hue + ' 58% 24%)');
   el.style.setProperty('--tagfg', 'hsl(' + hue + ' 85% 82%)');
   el.style.setProperty('--tagbd', 'hsl(' + hue + ' 50% 38%)');
 }

 function tagsFor(key) { return TAGS[key] || (TAGS[key] = []); }
 function allTags() {
   const seen = new Map();            // lowercase -> display spelling
   for (const k in TAGS) for (const t of TAGS[k])
     if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
   return [...seen.values()].sort((a, b) => a.localeCompare(b));
 }
 function tagCount(name) {
   const n = name.toLowerCase();
   let c = 0;
   for (const k in TAGS) if (TAGS[k].some(t => t.toLowerCase() === n)) c++;
   return c;
 }
 function makeChip(name, title, onRemove) {
   const el = document.createElement('span');
   el.className = 'tag';
   paintTag(el, name);
   el.appendChild(document.createTextNode(name));   // textContent, never innerHTML
   if (onRemove) {
     const x = document.createElement('button');
     x.textContent = '×';
     x.title = title;
     x.onclick = ev => { ev.stopPropagation(); onRemove(); };
     el.appendChild(x);
   }
   return el;
 }

 function renderCardTags(card) {
   const key = card.dataset.tagkey;
   const list = card.querySelector('.taglist');
   const input = list.querySelector('.taginput');
   list.querySelectorAll('.tag').forEach(n => n.remove());
   for (const name of tagsFor(key)) {
     const chip = makeChip(name, 'remove tag', () => {
       TAGS[key] = tagsFor(key).filter(t => t !== name);
       if (!TAGS[key].length) delete TAGS[key];
       persist(); renderCardTags(card); refreshTagUI();
     });
     list.insertBefore(chip, input);
   }
   // Pipe-delimited so an exact chip match cannot hit a substring: the chip
   // "Lake" must not match the tag "Lake Photos". A pipe can never appear in a
   // tag, since tags are restricted to letters, digits, space, _ and -.
   card.dataset.tags = tagsFor(key).length
     ? '|' + tagsFor(key).join('|').toLowerCase() + '|' : '';
 }

 function addTagToCard(card, raw) {
   const name = cleanTag(raw);
   if (!name) return false;
   const cur = tagsFor(card.dataset.tagkey);
   if (cur.some(t => t.toLowerCase() === name.toLowerCase())) return false;
   cur.push(name);
   cur.sort((a, b) => a.localeCompare(b));
   persist(); renderCardTags(card); refreshTagUI();
   return true;
 }

 for (const card of cards) {
   renderCardTags(card);
   const input = card.querySelector('.taginput');
   // Comma and Enter both commit. Blur catches the half-typed tag people leave
   // behind when they click away, which is the most common way to lose one.
   input.addEventListener('keydown', e => {
     if (e.key === 'Enter' || e.key === ',') {
       e.preventDefault();
       addTagToCard(card, input.value);
       input.value = '';
     } else if (e.key === 'Backspace' && !input.value) {
       const cur = tagsFor(card.dataset.tagkey);
       if (cur.length) {
         cur.pop();
         if (!cur.length) delete TAGS[card.dataset.tagkey];
         persist(); renderCardTags(card); refreshTagUI();
       }
     } else if (e.key === 'Escape') { input.value = ''; input.blur(); }
   });
   input.addEventListener('input', () => {
     // Handles pasting "a, b, c" as well as typing a comma.
     if (input.value.includes(',')) {
       const parts = input.value.split(',');
       const tail = parts.pop();
       parts.forEach(p => addTagToCard(card, p));
       input.value = cleanTag(tail);
     }
   });
   input.addEventListener('blur', () => {
     if (input.value.trim()) { addTagToCard(card, input.value); input.value = ''; }
   });
 }

 // ---- tag chips inside the search box -------------------------------------
 const selected = [];                    // display spellings, kept alphabetical
 const chipBox = document.getElementById('chips');
 const menu = document.getElementById('tagmenu');
 const searchBox = document.getElementById('searchbox');

 function renderChips() {
   chipBox.textContent = '';
   selected.sort((a, b) => a.localeCompare(b));
   for (const name of selected) {
     chipBox.appendChild(makeChip(name, 'remove from search', () => {
       selected.splice(selected.indexOf(name), 1);
       renderChips(); apply();
     }));
   }
 }
 function selectTag(name) {
   if (!selected.some(t => t.toLowerCase() === name.toLowerCase())) selected.push(name);
   qEl.value = ''; query = '';
   closeMenu(); renderChips(); apply();
 }

 let menuItems = [], menuIdx = -1;
 function closeMenu() { menu.classList.remove('open'); menuItems = []; menuIdx = -1; }
 function openMenu(term) {
   const t = (term || '').trim().toLowerCase();
   const chosen = new Set(selected.map(x => x.toLowerCase()));
   const matches = allTags().filter(n => !chosen.has(n.toLowerCase()) &&
                                         (!t || n.toLowerCase().includes(t)));
   menu.textContent = '';
   menuItems = matches.slice(0, 40);
   menuIdx = menuItems.length ? 0 : -1;
   if (!menuItems.length) {
     if (!t) { closeMenu(); return; }
     const d = document.createElement('div');
     d.className = 'none';
     d.textContent = 'No tag matches - still filtering by name and folder';
     menu.appendChild(d);
     menu.classList.add('open');
     return;
   }
   menuItems.forEach((name, i) => {
     const d = document.createElement('div');
     if (i === menuIdx) d.className = 'sel';
     d.appendChild(makeChip(name, null, null));
     const c = document.createElement('span');
     c.className = 'count';
     const n = tagCount(name);
     c.textContent = n + (n === 1 ? ' photo' : ' photos');
     d.appendChild(c);
     const del = document.createElement('button');
     del.className = 'delall';
     del.type = 'button';
     del.textContent = 'remove';
     del.title = 'Remove "' + name + '" from ' + (n === 1 ? 'the 1 photograph' :
       'all ' + n + ' photographs') + ' that carry it, including any hidden by ' +
       'the current filters';
     // mousedown, not click: the row selects on mousedown, so the button has to
     // intercept the same event or the tag would be picked before it is deleted.
     del.onmousedown = e => { e.preventDefault(); e.stopPropagation();
                              removeTagEverywhere(name); };
     d.appendChild(del);
     d.onmousedown = e => { e.preventDefault(); selectTag(name); };
     menu.appendChild(d);
   });
   menu.classList.add('open');
 }
 function moveMenu(delta) {
   if (!menuItems.length) return;
   menuIdx = (menuIdx + delta + menuItems.length) % menuItems.length;
   [...menu.children].forEach((c, i) => c.classList.toggle('sel', i === menuIdx));
   const el = menu.children[menuIdx];
   if (el && el.scrollIntoView) el.scrollIntoView({block: 'nearest'});
 }
 // A tag survives as long as ONE photograph still carries it - and that
 // photograph may be hidden by the band buttons, the folder picker or the
 // near-duplicate toggle, so deleting every chip you can see is not always
 // enough to retire a tag. This does it in one action, from the search box
 // where you are when you notice the tag is still there.
 function removeTagEverywhere(name) {
   const needle = name.toLowerCase();
   const n = tagCount(name);
   if (!n) { refreshTagUI(); return; }
   const scope = n === 1 ? 'the 1 photo that uses it'
                         : 'all ' + n + ' photos that use it';
   if (!confirm('Delete the tag "' + name + '" from ' + scope + '?\n\n' +
                'This includes photos hidden by the current filters.')) return;
   const before = JSON.stringify(TAGS);
   let hit = 0;
   for (const k of Object.keys(TAGS)) {
     const kept = TAGS[k].filter(t => t.toLowerCase() !== needle);
     if (kept.length !== TAGS[k].length) hit++;
     if (kept.length) TAGS[k] = kept; else delete TAGS[k];
   }
   if (!hit) { refreshTagUI(); return; }
   persist();
   cards.forEach(c => renderCardTags(c));
   refreshTagUI();
   toast('Removed "' + name + '" from ' + hit + (hit === 1 ? ' photo' : ' photos'),
         'Undo', () => {
     const restored = JSON.parse(before);
     for (const k of Object.keys(TAGS)) delete TAGS[k];
     for (const k in restored) TAGS[k] = restored[k];
     persist();
     cards.forEach(c => renderCardTags(c));
     refreshTagUI();
     toast('Put "' + name + '" back');
   });
 }

 function refreshTagUI() {
   // A tag can disappear from the library entirely; drop it from the search too.
   const live = new Set(allTags().map(t => t.toLowerCase()));
   for (let i = selected.length - 1; i >= 0; i--)
     if (!live.has(selected[i].toLowerCase())) selected.splice(i, 1);
   renderChips();
   if (menu.classList.contains('open')) openMenu(qEl.value);
   apply();
 }

 if (saveBtn) saveBtn.onclick = () => {
   const blob = new Blob([JSON.stringify(TAGS, null, 2)], {type: 'application/json'});
   const a = document.createElement('a');
   a.href = URL.createObjectURL(blob);
   a.download = 'tags.json';
   document.body.appendChild(a); a.click(); a.remove();
   setTimeout(() => URL.revokeObjectURL(a.href), 2000);
   dirty = false;
   saveBtn.textContent = 'Save tags';
   toast('tags.json downloaded - move it into the report folder to keep it');
 };

 let verdictFilter = 'all', showDups = false, query = '', kindFilter = 'all', folderFilter = 'all';
 const shown = document.getElementById('shown');
 function apply() {
   let n = 0;
   for (const c of cards) {
     const okV = verdictFilter === 'all' || c.dataset.verdict === verdictFilter;
     const okD = showDups || c.dataset.dup === '0';
     // Chips are ORed: a card qualifies if it carries ANY selected tag, so
     // picking "Lake" and "Desert" shows both sets rather than only photographs
     // that happen to be tagged with both. Each extra chip widens the results.
     // Free text still matches folder and filename, and a tag name as well.
     const cardTags = (c.dataset.tags || '');
     let okT = selected.length === 0;
     for (const t of selected) {
       if (cardTags.includes('|' + t.toLowerCase() + '|')) { okT = true; break; }
     }
     const okQ = !query || c.dataset.search.includes(query) || cardTags.includes(query);
     const okK = kindFilter === 'all' || c.dataset.kind === kindFilter;
     // data-foldertop is the top-level folder, computed in Python, so selecting
     // a folder brings in its subfolders (Old Faithful, Publish) with a plain
     // equality test. Deliberately no path-separator logic here: a lone Windows
     // backslash inside this template would escape the quote and break the whole
     // script block.
     const okF = folderFilter === 'all' || c.dataset.foldertop === folderFilter;
     const vis = okV && okD && okQ && okK && okF && okT;
     c.classList.toggle('hidden', !vis);
     if (vis) n++;
   }
   if (shown) shown.textContent = n + ' shown';
 }
 // ---- sorting --------------------------------------------------------------
 // Cards are reordered in the DOM rather than re-rendered, so tags, hearts and
 // every other bit of per-card state survive a sort untouched. The lightbox
 // walks visible cards in DOM order, so it follows the sort automatically.
 const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'});
 function sortCards(mode) {
   const [key, dir] = mode.split('-');
   const sign = dir === 'desc' ? -1 : 1;
   const value = c => {
     if (key === 'score') return parseFloat(c.dataset.sortscore || '0');
     if (key === 'date') return c.dataset.date || '';
     if (key === 'folder') return c.dataset.folder || '';
     return c.dataset.name || '';
   };
   const ordered = cards.slice().sort((a, b) => {
     const va = value(a), vb = value(b);
     let r;
     if (key === 'score') r = va - vb;
     else if (!va && !vb) r = 0;
     // Undated or unfoldered items always sink to the bottom rather than
     // clumping at whichever end the sort direction happens to favour.
     else if (!va) return 1;
     else if (!vb) return -1;
     // Timestamps are 'YYYY-MM-DD HH:MM:SS', so a plain string comparison is
     // already chronological to the second. The locale collator is deliberately
     // NOT used here: its numeric mode reads digit runs as numbers, which is the
     // right thing for file names and the wrong thing for a fixed-width date.
     else if (key === 'date') r = va < vb ? -1 : va > vb ? 1 : 0;
     else r = collator.compare(va, vb);
     // Ties fall back to score, so equal folders or dates stay meaningfully ordered.
     if (r === 0) return parseFloat(b.dataset.sortscore || '0') - parseFloat(a.dataset.sortscore || '0');
     return r * sign;
   });
   const frag = document.createDocumentFragment();
   ordered.forEach(c => frag.appendChild(c));
   grid.appendChild(frag);
 }
 document.getElementById('sort').onchange = e => { sortCards(e.target.value); apply(); };

 document.getElementById('kind').onchange = e => { kindFilter = e.target.value; apply(); };
 document.getElementById('folder').onchange = e => { folderFilter = e.target.value; apply(); };
 document.querySelectorAll('button[data-f]').forEach(b => b.onclick = () => {
   document.querySelectorAll('button[data-f]').forEach(x => x.classList.remove('on'));
   b.classList.add('on'); verdictFilter = b.dataset.f; apply();
 });
 document.getElementById('dups').onchange = e => { showDups = e.target.checked; apply(); };
 qEl.addEventListener('input', e => {
   query = e.target.value.toLowerCase();
   openMenu(e.target.value);
   apply();
 });
 qEl.addEventListener('focus', () => {
   searchBox.classList.add('focus');
   openMenu(qEl.value);
 });
 qEl.addEventListener('blur', () => {
   searchBox.classList.remove('focus');
   setTimeout(closeMenu, 120);          // let a click on an option land first
 });
 qEl.addEventListener('keydown', e => {
   if (e.key === 'ArrowDown') { e.preventDefault(); moveMenu(1); }
   else if (e.key === 'ArrowUp') { e.preventDefault(); moveMenu(-1); }
   else if (e.key === 'Enter') {
     if (menuIdx >= 0 && menuItems[menuIdx]) { e.preventDefault(); selectTag(menuItems[menuIdx]); }
   } else if (e.key === 'Escape') {
     if (menu.classList.contains('open')) { e.stopPropagation(); closeMenu(); }
     else { qEl.value = ''; query = ''; apply(); }
   } else if (e.key === 'Backspace' && !qEl.value && selected.length) {
     selected.pop(); renderChips(); apply();
   }
 });
 searchBox.addEventListener('mousedown', e => {
   if (e.target === searchBox || e.target === chipBox) { e.preventDefault(); qEl.focus(); }
 });
 apply();

 // ---- lightbox -------------------------------------------------------------
 // A browser cannot decode a NEF, so the overlay shows the preview JPEG the
 // script rendered from the RAW. Everything here is local to the page: no
 // server, no network, just the files already sitting next to this report.
 const toastEl = document.getElementById('toast');
 let toastTimer;
 // Keep the tag dropdown from being closed by the lightbox's global Escape.
 document.addEventListener('keydown', e => {
   if (e.key === 'Escape' && menu.classList.contains('open')) {
     closeMenu();
     e.stopPropagation();
   }
 }, true);

 function toast(msg, actionLabel, onAction) {
   toastEl.textContent = msg;
   if (actionLabel && onAction) {
     const b = document.createElement('button');
     b.textContent = actionLabel;
     b.style.cssText = 'margin-left:12px;background:#333;border:1px solid #555;' +
       'color:#eee;border-radius:6px;padding:3px 9px;font-size:12px;cursor:pointer';
     b.onclick = () => { toastEl.classList.remove('show'); onAction(); };
     toastEl.appendChild(b);
   }
   toastEl.classList.add('show');
   clearTimeout(toastTimer);
   // An undoable message lingers: an Undo that vanishes in two seconds is not
   // an Undo. Everything else keeps the short, unobtrusive timing.
   toastTimer = setTimeout(() => toastEl.classList.remove('show'),
                           (actionLabel && onAction) ? 9000 : 2400);
 }

 const lb = document.getElementById('lb'), stage = document.getElementById('lb-stage');
 const lbImg = document.getElementById('lb-img');
 let idx = -1;

 // Queried from the grid, NOT filtered out of the `cards` snapshot: that array
 // keeps the order the cards were created in, so after a sort it no longer
 // matches what is on screen and the arrows would walk to the wrong photograph.
 // querySelectorAll returns document order, which is what the eye sees.
 const visible = () => [...grid.querySelectorAll('.card:not(.hidden)')];

 function show(i) {
   const list = visible();
   if (!list.length) return;
   idx = (i + list.length) % list.length;
   const c = list[idx];
   const prev = c.dataset.preview;
   lb.classList.toggle('no-image', !prev);
   lbImg.src = prev || '';
   document.getElementById('lb-name').textContent = c.dataset.name;
   document.getElementById('lb-sub').textContent =
     [c.dataset.folder, c.dataset.verdicttext + '  ' + c.dataset.score]
       .filter(Boolean).join('  ·  ');
   // Its own slot: at the end of the subtitle a long folder name would push
   // it past the ellipsis.
   document.getElementById('lb-dims').textContent = c.dataset.res || '';
   document.getElementById('lb-note').textContent = c.dataset.note;
   document.getElementById('lb-count').textContent = (idx + 1) + '/' + list.length;
   document.getElementById('lb-folder').href = c.dataset.folderurl;
   stage.classList.remove('actual');
   stage.scrollTop = stage.scrollLeft = 0;
   lb.classList.add('open');
   // Warm the neighbours so arrow-key paging feels instant.
   [list[(idx + 1) % list.length], list[(idx - 1 + list.length) % list.length]]
     .forEach(n => { if (n && n.dataset.preview) new Image().src = n.dataset.preview; });
 }
 function close() {
   // Leave the page on the photograph being looked at, not the one that was
   // clicked several arrow presses ago.
   const list = visible();
   const c = idx >= 0 ? list[idx] : null;
   lb.classList.remove('open');
   if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
   if (c) c.scrollIntoView({ block: 'center' });
 }

 // A two-finger sideways flick on a trackpad arrives as wheel events carrying
 // deltaX. Momentum fires dozens of them, so the accumulator locks after one
 // step and only rearms once the flick has died down.
 let wacc = 0, wlock = false, wtimer = null;
 lb.addEventListener('wheel', e => {
   if (!lb.classList.contains('open')) return;
   if (stage.classList.contains('actual')) return;   // at 1:1 the stage pans
   if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;
   clearTimeout(wtimer);
   wtimer = setTimeout(() => { wacc = 0; wlock = false; }, 240);
   if (wlock) return;
   wacc += e.deltaX;
   if (Math.abs(wacc) < 60) return;
   const dir = wacc < 0 ? -1 : 1;
   wacc = 0; wlock = true;
   show(idx + dir);
 }, { passive: true });

 // Swipe between photographs. Only a decisively sideways drag counts: a mostly
 // vertical one is someone scrolling, and a short one is a tap that wandered.
 // Skipped at 1:1, where dragging is how you pan around the photograph.
 let tx = 0, ty = 0, tracking = false;
 lb.addEventListener('touchstart', e => {
   tracking = e.touches.length === 1 && !stage.classList.contains('actual');
   if (tracking) { tx = e.touches[0].clientX; ty = e.touches[0].clientY; }
 }, { passive: true });
 lb.addEventListener('touchend', e => {
   if (!tracking) return;
   tracking = false;
   const t = e.changedTouches[0];
   const dx = t.clientX - tx, dy = t.clientY - ty;
   if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
   show(idx + (dx < 0 ? 1 : -1));
 }, { passive: true });
 function toggleZoom() {
   stage.classList.toggle('actual');
   document.getElementById('lb-zoom').textContent =
     stage.classList.contains('actual') ? 'Fit' : '1:1';
 }
 function toggleFull() {
   if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
   else lb.requestFullscreen().catch(() => toast('Full screen was refused by the browser'));
 }

 grid.addEventListener('click', ev => {
   const img = ev.target.closest('.card img');
   if (!img) return;
   const card = img.closest('.card');
   const list = visible();
   const at = list.indexOf(card);
   if (at >= 0) show(at);
 });
 document.querySelectorAll('a[data-view]').forEach(a => a.onclick = ev => {
   ev.preventDefault();
   const at = visible().indexOf(a.closest('.card'));
   if (at >= 0) show(at);
 });

 document.getElementById('lb-close').onclick = close;
 document.getElementById('lb-prev').onclick = () => show(idx - 1);
 document.getElementById('lb-next').onclick = () => show(idx + 1);
 document.getElementById('lb-zoom').onclick = toggleZoom;
 document.getElementById('lb-full').onclick = toggleFull;
 lbImg.onclick = toggleZoom;
 document.getElementById('lb-copy').onclick = async () => {
   const p = visible()[idx].dataset.path;
   try { await navigator.clipboard.writeText(p); toast('Path copied to clipboard'); }
   catch { window.prompt('Copy this path:', p); }
 };
 stage.addEventListener('click', ev => { if (ev.target === stage) close(); });

 document.addEventListener('keydown', ev => {
   if (!lb.classList.contains('open')) return;
   if (ev.key === 'Escape' && !document.fullscreenElement) { close(); }
   else if (ev.key === 'ArrowRight' || ev.key === ' ') { ev.preventDefault(); show(idx + 1); }
   else if (ev.key === 'ArrowLeft') { ev.preventDefault(); show(idx - 1); }
   else if (ev.key === 'f' || ev.key === 'F') { toggleFull(); }
   else if (ev.key === 'z' || ev.key === 'Z') { toggleZoom(); }
 });
</script>
"""


def top_folder(folder: Optional[str]) -> str:
    """First path component of a folder, e.g. 'A/Publish' -> 'A'. Used to group
    subfolders under their parent in the report's folder picker."""
    return re.split(r"[\\/]", folder or "(root)")[0] or "(root)"


def write_html(rows, dest: Path, root: Path, stats: dict,
               tags: Optional[dict] = None) -> None:
    tags = tags or {}
    previews_dir = dest.parent / "previews"
    cards = []
    for r in rows:
        if r["error"]:
            continue
        verdict = r["verdict"] or "PASS"
        is_dup = 1 if r["dup_of"] else 0
        thumb = f"thumbs/{thumb_name(r['path'])}"
        is_vid = (r["source_type"] == "video_frame")
        # Capture timestamp. Held as 'YYYY-MM-DD HH:MM:SS' so it sorts as plain
        # text down to the minute; rendered as "June 28, 2011 - 09:14".
        iso = r["taken_at"] if "taken_at" in r.keys() else None
        shot_on = pretty_taken(iso)   # escaped where it is written, in meta_line
        # A date baked into the folder name is dropped from the display: the
        # capture date beside it already says when, so "2011-07-05 - Wyoming"
        # would just print the same information twice.
        folder_shown = strip_folder_date(r["folder"])
        # Pixel dimensions, when the row has them. A database scored before the
        # columns existed simply shows nothing here rather than failing.
        try:
            wpx, hpx = r["width"], r["height"]
        except (IndexError, KeyError):
            wpx = hpx = None
        res_txt = pretty_resolution(wpx, hpx)
        # The folder takes a line to itself, being the one field of unbounded
        # length. Its title attribute carries the full name when it is elided.
        folder_html = (f'<div class="folder" title="{html.escape(folder_shown, quote=True)}">'
                       f'{html.escape(folder_shown)}</div>') if folder_shown else ""
        # Date, time and resolution below it, each wrapped so a break can fall
        # between facts but never inside one.
        atoms = spec_atoms(iso, wpx, hpx)
        note_main, note_flag = split_note(r["note"])
        note_html = html.escape(note_main) + (
            f'{NOTE_SEP}<span class="flag">{html.escape(note_flag)}</span>'
            if note_flag else "")
        specs_html = ('<div class="specs">'
                      + " &middot; ".join(f"<span>{html.escape(a)}</span>" for a in atoms)
                      + "</div>") if atoms else ""
        # Everything searchable in one blob: filename, folder (as shown AND as
        # named on disk), every date form, the verdict and the written feedback,
        # so "august 2026", "wyoming", "2011-07-05", "top pick" and "moody
        # landscape" all work. Lowercased here and the query is lowercased in the
        # browser, so every search is case-insensitive in both directions.
        search_blob = html.escape(
            f"{r['folder']} {folder_shown} {r['filename']} {iso or ''} "
            f"{pretty_date(iso)} {pretty_time(iso)} {verdict} "
            f"{res_txt} {r['note'] or ''}".lower(), quote=True)

        # The real file on disk, for the folder link and the copy-path button.
        real = (r["source_video"] or split_virtual_path(r["path"])[0]) if is_vid else r["path"]
        preview = f"previews/{thumb_name(r['path'])}"
        if not (previews_dir / thumb_name(r["path"])).exists():
            preview = ""

        links = (f'<a href="#" data-view="1">view</a>'
                 f'<a href="{win_folder_url(real)}">open folder</a>')
        if is_vid:
            vid_badge = ('<span class="badge VIDEO">VIDEO '
                         f'{html.escape(hhmmss(r["timestamp_s"] or 0))}</span>')
            if r["extracted_path"]:
                links += (f'<a href="{win_folder_url(r["extracted_path"])}"'
                          f' style="color:#8ff0bd">extracted still</a>')
        else:
            vid_badge = ""

        cards.append(f"""<div class="card{' dup' if is_dup else ''}"
   data-verdict="{html.escape(verdict)}" data-dup="{is_dup}"
   data-kind="{'video' if is_vid else 'photo'}" data-search="{search_blob}"
   data-date="{html.escape(iso or '', quote=True)}"
   data-sortscore="{(r['composite'] or 0):.4f}"
   data-preview="{html.escape(preview, quote=True)}"
   data-path="{html.escape(real, quote=True)}"
   data-tagkey="{html.escape(r['path'], quote=True)}"
   data-folderurl="{win_folder_url(real)}"
   data-name="{html.escape(r['filename'], quote=True)}"
   data-folder="{html.escape(folder_shown, quote=True)}"
   data-foldertop="{html.escape(top_folder(r['folder']), quote=True)}"
   data-verdicttext="{html.escape(verdict, quote=True)}"
   data-res="{html.escape(res_txt, quote=True)}"
   data-score="{(r['composite'] or 0):.0f}"
   data-note="{html.escape(r['note'] or '', quote=True)}">
  <img loading="lazy" src="{html.escape(thumb)}" alt="">
  <div class="body">
    <div class="top">
      <span>{vid_badge}<span class="badge {verdict.replace(' ', '-')}">{html.escape(verdict)}</span></span>
      <span class="score">{(r['composite'] or 0):.0f}</span>
    </div>
    <div class="name">{html.escape(r['filename'])}{' &middot; near-dup' if is_dup else ''}</div>
    {folder_html}{specs_html}
    <div class="note">{note_html}</div>
    <div class="links">{links}</div>
    <div class="tagwrap"><div class="taglist">
      <input class="taginput" type="text" autocomplete="off" spellcheck="false"
             placeholder="add tag, comma or Enter">
    </div></div>
  </div>
</div>""")

    stats_html = (
        f"<b>{stats['total']}</b> scored "
        f"({stats['photos']} photos, {stats['video_frames']} frames from "
        f"{stats['videos']} videos) &middot; "
        f"<b>{stats['top']}</b> top picks &middot; "
        f"<b>{stats['strong']}</b> strong &middot; "
        f"<b>{stats['maybe']}</b> maybe &middot; "
        f"<b>{stats['extracted']}</b> stills extracted from video &middot; "
        f"<b>{stats['dups']}</b> near-duplicates hidden &middot; "
        f"<b>{stats['folders']}</b> folders &middot; "
        f"<b>{stats['errors']}</b> errors"
    )

    # Folder picker. Subfolders are folded into their parent, so choosing
    # "2011-06-28 - Wyoming..." also shows its Old Faithful and Publish frames.
    tops: dict[str, int] = {}
    for r in rows:
        if r["error"]:
            continue
        top = top_folder(r["folder"])
        tops[top] = tops.get(top, 0) + 1
    options = ['<option value="all">All folders '
               f'({len(tops)} folders, {sum(tops.values())} images)</option>']
    # The value stays the real directory name so filtering is exact; only the
    # label loses its date, matching what the cards show.
    for name in sorted(tops, key=lambda n: (strip_folder_date(n).lower(), n)):
        options.append(f'<option value="{html.escape(name, quote=True)}">'
                       f'{html.escape(strip_folder_date(name))} ({tops[name]})</option>')

    # Only tags for images actually in this report, so the shortlist variant
    # doesn't ship the whole library's tags. json.dumps escapes for JS, and the
    # allowed character set already rules out anything that could break out of
    # the script block.
    keys = {r["path"] for r in rows}
    payload = {k: v for k, v in tags.items() if k in keys}

    # Scope browser storage to this exact report file. Chrome treats every
    # file:// page as one origin, so without this the full report and the
    # shortlist report would fight over the same localStorage entry.
    store_key = "photo_scout_tags_" + hashlib.sha1(
        str(dest.resolve()).encode("utf-8")).hexdigest()[:16]

    out = (HTML_TEMPLATE
           .replace("__CARDS__", "\n".join(cards))
           .replace("__STATS__", stats_html)
           .replace("__FOLDER_OPTIONS__", "\n".join(options))
           .replace("__TAGS_JSON__", json.dumps(payload, ensure_ascii=True))
           .replace("__STORAGE_KEY__", store_key)
           .replace("__PROJECT_URL__", PROJECT_URL)
           )
    dest.write_text(out, encoding="utf-8")


def filter_hidden(rows, root: Path, min_edge: int = MIN_IMAGE_EDGE):
    """
    Drop rows that should not appear in a report: photographs under a hidden
    folder, photographs that are no longer on disk at all, and images below the
    size floor.

    The size check reads the dimensions recorded at scoring time rather than
    reopening every file, so it is free. A row from a database written before
    those columns existed has no dimensions, which counts as unknown and is
    always kept - lowering the floor can only ever add photographs back, never
    silently remove ones you have already seen. Rescore with --force to fill in
    the missing sizes.

    Two mechanisms for the hidden half, because one is not enough:

    * The recorded path is checked against HIDE_DIR_NAME. That catches a folder
      that was already hidden when its contents were scored.
    * The file's existence is checked. This is what makes RENAMING a folder to
      hide_from_photo_scout work. The database stores the path as it was at
      scoring time, so after a rename those rows point at a location that no
      longer exists - there is no text in the row that mentions the new name.
      A row whose file has gone is stale either way, and a report that lists
      photographs which are not in the library is simply wrong.

    If the library root is unreachable - an external drive not plugged in, a
    network share not mounted - the existence half is skipped entirely rather
    than reporting that every photograph has vanished.
    """
    rows = list(rows)
    by_path = [r for r in rows if not is_hidden(r["rel_path"] or r["path"])]
    hidden_n = len(rows) - len(by_path)

    def _too_small(r) -> bool:
        try:
            w, h = r["width"], r["height"]
        except (IndexError, KeyError):
            return False        # a database written before the columns existed
        if not w or not h:
            return False        # unknown resolution is never grounds for removal
        return below_size_floor((w, h), min_edge)

    sized = [r for r in by_path if not _too_small(r)]
    small_n = len(by_path) - len(sized)
    by_path = sized

    root_ok = True
    try:
        root_ok = root.exists()
    except OSError:
        root_ok = False

    missing_n = 0
    if root_ok:
        kept = []
        for r in by_path:
            # Video frames are virtual paths into a clip; judge them by the
            # clip, which is the thing that actually exists on disk.
            real = r["source_video"] or split_virtual_path(r["path"])[0]
            try:
                if Path(real).exists():
                    kept.append(r)
                else:
                    missing_n += 1
            except OSError:
                kept.append(r)          # unreadable is not the same as absent
        by_path = kept
    else:
        log(f"Note:      {root} is not reachable, so the report is built from "
            f"the database alone - photographs that have moved or been hidden "
            f"since the last scan may still appear.")

    if hidden_n:
        log(f"Hidden:    {hidden_n} photographs under {HIDE_DIR_NAME} left out")
    if small_n:
        log(f"Hidden:    {small_n} images under the {min_edge}px size floor left out")
    if missing_n:
        log(f"Hidden:    {missing_n} photographs are no longer where they were "
            f"scored (moved, renamed or deleted) and were left out")
    return by_path


def build_reports(cache: Cache, out_dir: Path, root: Path,
                  min_edge: int = MIN_IMAGE_EDGE) -> None:
    rows = filter_hidden(cache.all_rows(), root, min_edge)
    tags = load_tags(out_dir)
    stats = {
        "total": sum(1 for r in rows if not r["error"]),
        "top": sum(1 for r in rows if r["verdict"] == "TOP PICK" and not r["dup_of"]),
        "strong": sum(1 for r in rows if r["verdict"] == "STRONG" and not r["dup_of"]),
        "maybe": sum(1 for r in rows if r["verdict"] == "MAYBE" and not r["dup_of"]),
        "dups": sum(1 for r in rows if r["dup_of"]),
        "folders": len({r["folder"] for r in rows}),
        "errors": sum(1 for r in rows if r["error"]),
        "photos": sum(1 for r in rows if r["source_type"] != "video_frame" and not r["error"]),
        "video_frames": sum(1 for r in rows if r["source_type"] == "video_frame" and not r["error"]),
        "videos": len({r["source_video"] for r in rows if r["source_video"]}),
        "extracted": sum(1 for r in rows if r["extracted_path"]),
    }
    write_html(rows, out_dir / "report.html", root, stats, tags)
    write_csv(rows, out_dir / "report.csv", tags)

    shortlist = [r for r in rows if r["verdict"] in ("TOP PICK", "STRONG") and not r["dup_of"]]
    write_csv(shortlist, out_dir / "shortlist.csv", tags)

    log(f"Report:    {out_dir / 'report.html'}")
    log(f"  NB the report is library-wide: it covers all {stats['folders']} folders "
        f"scored so far, not only the one just processed. Use the folder dropdown "
        f"in the report to narrow it.")
    log(f"CSV:       {out_dir / 'report.csv'}")
    log(f"Shortlist: {out_dir / 'shortlist.csv'}  ({len(shortlist)} images)")
    log(f"Stats: {stats}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def run_reset(root: Path, out_dir: Path, assume_yes: bool) -> bool:
    """
    Delete everything this script has produced, so the next run starts clean.

    Deliberately narrow: it will only ever remove a directory literally named
    OUTPUT_DIRNAME sitting directly inside --root. Your photographs live outside
    that directory and are never touched. The guards below exist so that a
    mistyped --root can't turn this into a recursive delete of something else.
    """
    import shutil as _sh

    resolved = out_dir.resolve()
    if resolved == root.resolve() or root.resolve() in resolved.parents:
        log(f"Refusing to reset: {out_dir} is inside the photo library {root}, "
            f"which is read-only.")
        return False
    if resolved.parent == resolved or resolved == Path.home().resolve():
        log(f"Refusing to reset: {out_dir} is a filesystem or home root.")
        return False

    if not out_dir.exists():
        log(f"Nothing to reset - {out_dir} does not exist yet.")
        return True

    # Identify the directory by what's in it rather than by its name, so a custom
    # --out still works. Deleting a directory full of someone else's files because
    # they mistyped --out would be unforgivable, so anything unrecognised is
    # refused outright.
    contents = list(out_dir.iterdir())
    ours = (out_dir / "scores.sqlite3").exists()
    if contents and not ours:
        log(f"Refusing to reset: {out_dir} exists but has no scores.sqlite3, so it "
            f"does not look like a photo_scout output directory.")
        log(f"  It contains: {', '.join(sorted(p.name for p in contents)[:6])}"
            f"{' ...' if len(contents) > 6 else ''}")
        log(f"  Delete it yourself if you're sure, or point --out somewhere else.")
        return False

    scored = thumbs = previews = stills = 0
    total_bytes = 0
    db = out_dir / "scores.sqlite3"
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            scored = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            conn.close()
        except Exception:
            pass
    for sub, name in ((out_dir / "thumbs", "thumbs"), (out_dir / "previews", "previews"),
                      (out_dir / "extracted_stills", "stills")):
        if sub.exists():
            n = sum(1 for _ in sub.rglob("*") if _.is_file())
            if name == "thumbs":
                thumbs = n
            elif name == "previews":
                previews = n
            else:
                stills = n
    for f in out_dir.rglob("*"):
        if f.is_file():
            try:
                total_bytes += f.stat().st_size
            except OSError:
                pass

    log(f"About to delete {out_dir}")
    log(f"  {scored} scored results, {thumbs} thumbnails, {previews} previews, "
        f"{stills} extracted stills")
    log(f"  {total_bytes / 1e6:.0f} MB total, plus any saved calibration")
    log("  Your photographs and video are NOT in this directory and are not affected.")

    tags_path = out_dir / TAGS_FILE
    tags_blob = tags_path.read_bytes() if tags_path.exists() else None
    if tags_blob is not None:
        log(f"  {TAGS_FILE} will be PRESERVED - tags are your work, not output.")

    if not assume_yes:
        try:
            answer = input("Type 'yes' to delete and start over: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            log("Cancelled - nothing was deleted.")
            return False

    _sh.rmtree(out_dir)
    if tags_blob is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        tags_path.write_bytes(tags_blob)
        log(f"Deleted. {TAGS_FILE} restored; starting from scratch otherwise.")
    else:
        log("Deleted. Starting from scratch.")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True,
                    help=r'Photo library to score, e.g. /path/to/photos or "D:\Photos". '
                         r'READ-ONLY: nothing is ever written, changed or deleted '
                         r'inside it.')
    ap.add_argument("--out", metavar="DIR",
                    help="Where to keep scores, thumbnails, previews and reports. "
                         "Defaults to a '_photo_scout' folder beside this script. "
                         "Must not be inside --root.")
    ap.add_argument("--folder", help="Score only this subfolder of --root")
    ap.add_argument("--limit", type=int, help="Stop after N new images (for testing)")
    ap.add_argument("--force", action="store_true", help="Re-score even if cached")
    ap.add_argument("--reset", action="store_true",
                    help="Delete all previous results (scores, thumbnails, previews, "
                         "calibration) and start over. Only ever removes the script's "
                         "own output directory; your photographs are untouched.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip the --reset confirmation prompt")
    ap.add_argument("--report-only", action="store_true", help="Rebuild reports, score nothing")
    ap.add_argument("--calibrate", action="store_true",
                    help="Force a re-fit of the score ranges and verdict bands right "
                         "now, then rebuild the reports. Normally unnecessary - this "
                         "happens automatically after scoring. Loads no models.")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="Do not calibrate automatically after scoring. Scores will "
                         "use the shipped defaults, which squash most libraries.")
    ap.add_argument("--recompute", action="store_true",
                    help="Rebuild scores and verdicts from cached model outputs after "
                         "editing the weights or bands. Loads no models.")
    ap.add_argument("--no-nima", action="store_true", help="Skip the NIMA technical model")
    ap.add_argument("--no-thumbs", action="store_true", help="Skip contact-sheet thumbnails")
    ap.add_argument("--no-previews", action="store_true",
                    help="Skip the larger JPEGs the lightbox displays (saves disk, "
                         "but clicking a photo will have nothing to show)")
    ap.add_argument("--min-edge", type=int, default=MIN_IMAGE_EDGE, metavar="PX",
                    help=f"Ignore any image or clip whose shorter side is under PX "
                         f"pixels - icons, emoji, memes, web thumbnails and the like "
                         f"(default {MIN_IMAGE_EDGE}). Use 0 to score everything. "
                         f"This is a filter, not a scoring factor: resolution never "
                         f"changes a photograph's score.")
    ap.add_argument("--no-dedup", action="store_true", help="Skip near-duplicate grouping")
    ap.add_argument("--no-video", action="store_true", help="Stills only; ignore video files")
    ap.add_argument("--video-every", type=float, default=VIDEO_SAMPLE_SECONDS,
                    metavar="SEC", help=f"Sample one video frame every SEC seconds "
                                        f"(default {VIDEO_SAMPLE_SECONDS:g})")
    ap.add_argument("--no-extract", action="store_true",
                    help="Score video frames but don't export full-resolution stills")
    ap.add_argument("--device", help="Force torch device: cuda / cpu")
    ap.add_argument("--verbose", action="store_true", help="Print full tracebacks on error")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        log(f"ERROR: root not found: {root}")
        return 2

    out_dir = Path(args.out).expanduser().resolve() if args.out else DEFAULT_OUT_DIR

    # The photo library is read-only input. Refuse to put working files in it,
    # so scores, thumbnails and previews can never end up among the originals.
    if out_dir == root or root in out_dir.parents:
        log(f"ERROR: the output directory {out_dir} is inside the photo library {root}.")
        log("       The library is treated as read-only. Pass --out with a location")
        log("       outside it, or omit --out to use the default:")
        log(f"       {DEFAULT_OUT_DIR}")
        return 2

    if args.reset:
        if not run_reset(root, out_dir, args.yes):
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Library (read-only): {root}")
    log(f"Output:              {out_dir}")
    cache = Cache(out_dir / "scores.sqlite3")

    # A saved calibration applies to every mode, so newly scored photos are
    # judged on the same scale as everything already in the database.
    existing_cal = None if args.calibrate else load_calibration(out_dir)

    if args.calibrate:
        if run_calibrate(cache, out_dir) is None:
            return 1
        run_recompute(cache)
        if not args.no_extract and not args.no_video and have_ffmpeg():
            run_extraction(cache, out_dir)
        build_reports(cache, out_dir, root, args.min_edge)
        return 0

    if args.recompute:
        run_recompute(cache)
        build_reports(cache, out_dir, root, args.min_edge)
        return 0

    if not args.report_only:
        run_scoring(root, out_dir, cache, args)
        if not args.no_dedup:
            run_dedup(cache)
        # Calibrate before extraction, not after: which video frames are worth
        # pulling out at full resolution depends on the scores, so they need to
        # be on the final scale first.
        if not args.no_calibrate:
            auto_calibrate(cache, out_dir, existing_cal)
        if not args.no_extract and not args.no_video and have_ffmpeg():
            run_extraction(cache, out_dir)

    build_reports(cache, out_dir, root, args.min_edge)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted - progress is saved. Re-run the same command to continue.")
        sys.exit(130)
