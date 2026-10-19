"""
The slideshow, in both the local report and the published gallery.

It is the lightbox on a timer rather than a second viewer, so most of what
could go wrong is interaction between the clock and everything the overlay
already did: closing, stepping by hand, the keyboard, a backgrounded tab. That
is what is checked here, in a real browser, on both pages - the two have
separate implementations and have drifted apart before.

The pages are built with a shortened interval. Each suite patches the two
constants by name before writing its fixture, so a rename shows up here as a
failure rather than as a suite that quietly waits five seconds a frame.

Also checked: that there is no Play button anywhere but inside the overlay,
which is the design - a slideshow starts from the photograph you opened; and
the speed button beside it, on an unhurried copy of each page so its labels
read the real presets, and on the hurried one for what a change does mid-play.
"""
import re
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_scout as ps                       # noqa: E402
import photo_scout_ghost as pg                 # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


STEP_MS, IDLE_MS = 700, 500          # what the fixtures run at
FAST_PRESETS = [300, STEP_MS, 1500, 3000]   # the speed button's steps, wound down
REAL_LABELS = ["8s", "15s", "30s", "4s", "8s"]  # the real ones, from the default
OUT = Path("/tmp/ps_slide"); LIB = Path("/tmp/ps_slide_lib")
for d in (OUT, LIB):
    shutil.rmtree(d, ignore_errors=True)
OUT.mkdir(parents=True)

SPEC = [("2011-06-28 - Wyoming and Tetons", 4), ("2010-03-12 - Arches National Park", 2)]
N_WY = 4


def hurry(html):
    """The same page, with the clock wound down so a suite can watch it run."""
    out, hits = html, 0
    for name, want in (("SLIDE_MS", STEP_MS), ("IDLE_MS", IDLE_MS)):
        out, n = re.subn(rf"{name} = \d+", f"{name} = {want}", out)
        hits += n
    out, n = re.subn(r"SLIDE_PRESETS = \[[\d, ]+\]",
                     f"SLIDE_PRESETS = {FAST_PRESETS}", out)
    hits += n
    assert hits == 3, f"the slideshow constants were not found to patch ({hits})"
    return out


# ---------------------------------------------------------------------------
# The published gallery
# ---------------------------------------------------------------------------
items, n = [], 0
for folder, count in SPEC:
    for _ in range(count):
        n += 1
        items.append({
            "photo_id": f"{n:016x}", "verdict": "STRONG", "score": 60 + n,
            "filename": f"DSC_{n:04d}.NEF", "folder": folder,
            "taken_at": f"2011-06-2{n % 9} 1{n % 6}:00:00",
            "resolution": "6000 x 4000 - 24.0 MP", "note": "Aesthetic 70",
            "thumb_url": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
            "preview_url": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
        })
GALLERY = OUT / "gallery.html"
# The viewport tag Ghost's theme supplies; without it a phone lays the page
# out at 980px and every phone measurement here would be of a desktop.
_shell = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
          "<meta name='viewport' content='width=device-width, initial-scale=1'>"
          "<title>Photo Scout Gallery</title></head><body style='margin:0;background:#111'>"
          "{}</body></html>")
GALLERY.write_text(_shell.format(hurry(pg.build_gallery_html(items, {}))), encoding="utf-8")
GALLERY_REAL = OUT / "gallery_real.html"
GALLERY_REAL.write_text(_shell.format(pg.build_gallery_html(items, {})), encoding="utf-8")
# With a heart service configured, so the foot of the overlay carries the hearts
# pill too - the most crowded that row ever gets. The service is unreachable;
# the pill keeps its space while it waits, which is what the layout check needs.
GALLERY_HEARTS = OUT / "gallery_hearts.html"
GALLERY_HEARTS.write_text(_shell.format(pg.build_gallery_html(
    items, {}, hearts_url="https://hearts.example.invalid")), encoding="utf-8")

# ---------------------------------------------------------------------------
# The local report, from a real scoring run
# ---------------------------------------------------------------------------
rng = np.random.default_rng(11)
n = 0
for folder, count in SPEC:
    d = LIB / folder
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(count):
        n += 1
        im = Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8))
        im = im.resize((900, 620), Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"2011:06:{1 + n:02d} 1{n % 6}:00:00"
        im.save(d / f"DSC_{n:04d}.JPG", "JPEG", quality=90, exif=ex)


class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": float(np.clip(5.0 + np.sin(self.n) * .6, 3.9, 6.1)),
                "nima_raw": float(np.clip(4.9 + np.cos(self.n) * .5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}


ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT / "report_out"
ps.main(["--root", str(LIB)])
REPORT = OUT / "report_out" / "report.html"
# Beside the original, so its thumbnails and previews still resolve.
REPORT_REAL = OUT / "report_out" / "report_real.html"
REPORT_REAL.write_text(REPORT.read_text(encoding="utf-8"), encoding="utf-8")
REPORT.write_text(hurry(REPORT.read_text(encoding="utf-8")), encoding="utf-8")


def frame(page, count_sel):
    """Which photograph is on screen, as the counter states it."""
    m = re.search(r"(\d+)", page.inner_text(count_sel))
    return int(m.group(1)) if m else -1


def run(page, name, tile, shot, bar, lb, play, count, closer, chrome, idle):
    print(f"\n=== {name} ===")
    settle = STEP_MS + 450

    print("-- the way in --")
    check("no Play button outside the overlay",
          page.eval_on_selector_all(
              f"{bar} button, .psc-crumb button, #crumb button",
              "els => els.filter(e => /play/i.test(e.textContent + "
              "(e.getAttribute('aria-label') || ''))).length") == 0)
    page.click(tile); page.wait_for_timeout(350)
    page.click(shot); page.wait_for_timeout(400)
    check("the overlay opens on the photograph that was clicked",
          page.eval_on_selector(lb, "e => e.classList.contains('open')"))
    check("and carries a named Play button",
          page.eval_on_selector(play, "e => e.getAttribute('aria-label')") == "Play slideshow")
    box = page.eval_on_selector(play, "e => [e.getBoundingClientRect().width,"
                                      " e.getBoundingClientRect().height]")
    check("big enough to hit (WCAG 2.5.8)", box[0] >= 24 and box[1] >= 24, str(box))
    check("which says it is not playing yet",
          page.eval_on_selector(play, "e => e.getAttribute('aria-pressed')") == "false")

    print("-- it plays, and it loops --")
    start = frame(page, count)
    page.click(play); page.wait_for_timeout(200)
    check("pressing it says so",
          page.eval_on_selector(play, "e => e.getAttribute('aria-pressed')") == "true")
    page.wait_for_timeout(settle)
    check("the next photograph arrives on its own", frame(page, count) == start + 1,
          f"{start} -> {frame(page, count)}")
    for _ in range(N_WY):
        page.wait_for_timeout(settle)
    check("and the end of the folder comes round to the beginning",
          1 <= frame(page, count) <= N_WY, str(frame(page, count)))

    print("-- the controls get out of the way --")
    page.wait_for_timeout(IDLE_MS + 400)
    check("everything but the photograph fades",
          page.eval_on_selector(lb, f"e => e.classList.contains('{idle}')")
          and page.eval_on_selector(chrome, "e => +getComputedStyle(e).opacity") < 0.05,
          page.eval_on_selector(chrome, "e => getComputedStyle(e).opacity"))
    page.mouse.move(500, 380); page.wait_for_timeout(250)
    check("and the first movement brings it back",
          not page.eval_on_selector(lb, f"e => e.classList.contains('{idle}')")
          and page.eval_on_selector(chrome, "e => +getComputedStyle(e).opacity") > 0.5)

    print("-- the keyboard --")
    page.keyboard.press("p"); page.wait_for_timeout(200)
    check("P pauses",
          page.eval_on_selector(play, "e => e.getAttribute('aria-pressed')") == "false")
    held = frame(page, count)
    page.wait_for_timeout(settle * 2)
    check("and a paused slideshow stays where it was", frame(page, count) == held,
          f"{held} -> {frame(page, count)}")
    check("the controls stay up while it is paused",
          not page.eval_on_selector(lb, f"e => e.classList.contains('{idle}')"))
    page.keyboard.press("p"); page.wait_for_timeout(200)
    check("and P starts it again",
          page.eval_on_selector(play, "e => e.getAttribute('aria-pressed')") == "true")

    print("-- stepping by hand --")
    # The photograph you reach with the arrow keys gets a full turn, not the
    # remainder of the one before it.
    page.wait_for_timeout(int(STEP_MS * 0.6))
    page.keyboard.press("ArrowRight")
    stepped = frame(page, count)
    page.wait_for_timeout(int(STEP_MS * 0.6))
    check("a photograph stepped to is not cut short", frame(page, count) == stepped,
          f"{stepped} -> {frame(page, count)}")
    page.wait_for_timeout(settle)
    check("and the clock carries on from there", frame(page, count) != stepped)

    print("-- closing --")
    page.keyboard.press("Escape"); page.wait_for_timeout(350)
    check("Escape closes the overlay",
          not page.eval_on_selector(lb, "e => e.classList.contains('open')"))
    page.click(shot); page.wait_for_timeout(400)
    check("and the slideshow does not resume when it is reopened",
          page.eval_on_selector(play, "e => e.getAttribute('aria-pressed')") == "false")
    held = frame(page, count)
    page.wait_for_timeout(settle * 2)
    check("nor does it advance on its own", frame(page, count) == held)

    page.keyboard.press("Escape"); page.wait_for_timeout(300)


def run_speed(real, fast, name, tile, shot, lb, play, speed, count, chrome, idle):
    """The speed button: its presets and labels, its memory, and its effect."""
    print(f"\n=== {name}: the speed button ===")
    real.click(tile); real.wait_for_timeout(350)
    real.click(shot); real.wait_for_timeout(400)
    label = lambda: real.eval_on_selector(speed, "e => e.textContent.trim()")
    named = lambda: real.eval_on_selector(speed, "e => e.getAttribute('aria-label')")
    check("it starts at eight seconds", label() == "8s", label())
    check("and says so to a screen reader",
          named() == "Slideshow speed: 8 seconds per photo", named())
    box = real.eval_on_selector(speed, "e => [e.getBoundingClientRect().width,"
                                       " e.getBoundingClientRect().height]")
    check("big enough to hit (WCAG 2.5.8)", box[0] >= 24 and box[1] >= 24, str(box))
    seen = [label()]
    for _ in range(4):
        real.click(speed); real.wait_for_timeout(120)
        seen.append(label())
    check("each press steps to the next preset, then back to the fastest",
          seen == REAL_LABELS, " -> ".join(seen))
    check("the spoken name follows it",
          named() == "Slideshow speed: 8 seconds per photo", named())
    check("and pressing it leaves the overlay open",
          real.eval_on_selector(lb, "e => e.classList.contains('open')"))
    real.click(speed); real.wait_for_timeout(120)        # 15s
    # A fresh visit to the bare address: reloading would follow the ?photo= the
    # overlay put there and reopen the photograph, which is a different test.
    real.goto(real.url.split("?")[0].split("#")[0]); real.wait_for_timeout(500)
    real.click(tile); real.wait_for_timeout(350)
    real.click(shot); real.wait_for_timeout(400)
    check("the choice is remembered across a reload", label() == "15s", label())

    # On the hurried page: change speed when the next photograph is nearly due.
    # The old interval must not fire; the new one runs from the press.
    fast.click(tile); fast.wait_for_timeout(350)
    fast.click(shot); fast.wait_for_timeout(400)
    fast.click(play)
    fast.wait_for_timeout(int(STEP_MS * 0.7))
    at = frame(fast, count)
    # Moving toward the button wakes the controls, as a hand would; the
    # hurried page fades them in half a second.
    fast.mouse.move(600, 400); fast.mouse.move(610, 410)
    fast.click(speed)                                    # 700 -> 1500
    fast.wait_for_timeout(int(STEP_MS * 0.7))
    check("a change mid-slideshow cancels the old interval",
          frame(fast, count) == at, f"{at} -> {frame(fast, count)}")
    fast.wait_for_timeout(1500)
    check("and the new one runs from the moment it was pressed",
          frame(fast, count) != at, f"{at} -> {frame(fast, count)}")
    fast.wait_for_timeout(IDLE_MS + 300)
    check("it fades with the other controls while the slideshow plays",
          fast.eval_on_selector(lb, f"e => e.classList.contains('{idle}')")
          and fast.eval_on_selector(chrome, "e => +getComputedStyle(e).opacity") < 0.05)
    fast.keyboard.press("Escape"); fast.wait_for_timeout(300)


def run_touch(page, name, tile, shot, lb, play, idle):
    """The first tap on a hidden-chrome slideshow, which only a finger can make.

    A mouse wakes the controls on its way to the click, so by the time the
    click lands a backdrop press means what it always meant - close. A finger
    cannot hover, so its first tap has to reveal rather than dismiss, or the
    slideshow is one stray touch away from ending.
    """
    print(f"\n=== {name}, on a touchscreen ===")
    page.tap(tile); page.wait_for_timeout(350)
    page.tap(shot); page.wait_for_timeout(400)
    page.tap(play); page.wait_for_timeout(IDLE_MS + 400)
    check("the controls are away",
          page.eval_on_selector(lb, f"e => e.classList.contains('{idle}')"))
    page.touchscreen.tap(12, 12)                  # the backdrop, clear of everything
    page.wait_for_timeout(300)
    check("a tap brings them back rather than closing the overlay",
          page.eval_on_selector(lb, "e => e.classList.contains('open')")
          and not page.eval_on_selector(lb, f"e => e.classList.contains('{idle}')"),
          "open=" + str(page.eval_on_selector(lb, "e => e.classList.contains('open')")))
    page.touchscreen.tap(12, 12)                  # and the second one does close it
    page.wait_for_timeout(300)
    check("a second tap, with them showing, closes as it always did",
          not page.eval_on_selector(lb, "e => e.classList.contains('open')"))


with sync_playwright() as pw:
    br = pw.chromium.launch()
    errors = []

    G = br.new_context(viewport={"width": 1200, "height": 850}).new_page()
    G.on("pageerror", lambda e: errors.append("gallery: " + str(e)))
    G.goto(GALLERY.resolve().as_uri()); G.wait_for_timeout(500)
    run(G, "the published gallery", '.psc-fold[data-group="1"]', ".psc-shot", ".psc-bar",
        ".psc-lb", ".psc-play", ".psc-count-lb", ".psc-lb .x", ".psc-lb .x", "psc-idle")

    R = br.new_context(viewport={"width": 1200, "height": 850}).new_page()
    R.on("pageerror", lambda e: errors.append("report: " + str(e)))
    R.goto(REPORT.resolve().as_uri()); R.wait_for_timeout(500)
    run(R, "the local report", '.fold[data-group="1"]', ".shot", "header",
        "#lb", "#lb-play", "#lb-count", "#lb-close", "#lb-bar", "idle")

    for label, real, fast, tile, shot, lb, play, speed, count, chrome, idle in (
            ("the published gallery", GALLERY_REAL, GALLERY, '.psc-fold[data-group="1"]',
             ".psc-shot", ".psc-lb", ".psc-play", ".psc-speed", ".psc-count-lb",
             ".psc-lbfoot", "psc-idle"),
            ("the local report", REPORT_REAL, REPORT, '.fold[data-group="1"]',
             ".shot", "#lb", "#lb-play", "#lb-speed", "#lb-count", "#lb-bar", "idle")):
        ctx = br.new_context(viewport={"width": 1200, "height": 850})
        A, B = ctx.new_page(), br.new_context(viewport={"width": 1200, "height": 850}).new_page()
        for pgx in (A, B):
            pgx.on("pageerror", lambda e: errors.append("speed: " + str(e)))
        A.goto(real.resolve().as_uri()); B.goto(fast.resolve().as_uri())
        A.wait_for_timeout(500); B.wait_for_timeout(500)
        run_speed(A, B, label, tile, shot, lb, play, speed, count, chrome, idle)
        ctx.close()

    print("\n=== the speed button on a phone ===")
    # The foot of the published overlay carries the hearts, Play, the speed, the
    # counter and Share on one row; at 390px they must still not collide.
    for label, url, tile, shot, foot, share in (
            ("the published gallery", GALLERY_HEARTS, '.psc-fold[data-group="1"]',
             ".psc-shot", ".psc-lbfoot", ".psc-link-lb"),
            ("the local report", REPORT_REAL, '.fold[data-group="1"]',
             ".shot", "#lb-bar", None)):
        Q = br.new_context(has_touch=True, is_mobile=True,
                           viewport={"width": 390, "height": 844}).new_page()
        Q.on("pageerror", lambda e: errors.append("phone: " + str(e)))
        Q.goto(url.resolve().as_uri()); Q.wait_for_timeout(500)
        Q.tap(tile); Q.wait_for_timeout(350)
        Q.tap(shot); Q.wait_for_timeout(400)
        check(f"{label}: nothing pushes the overlay sideways",
              Q.evaluate("() => document.documentElement.scrollWidth "
                         "<= document.documentElement.clientWidth + 1"))
        if share:
            # Boxes that do not overlap, in either direction: on a phone the
            # counter group rides a row above hearts and share rather than
            # squeezing between them.
            hits = Q.evaluate("""(sels) => {
                const box = s => document.querySelector(s).getBoundingClientRect();
                const meet = (a, b) => a.left < b.right && b.left < a.right
                                    && a.top < b.bottom && b.top < a.bottom;
                const f = box(sels[0]);
                return sels.slice(1).filter(s => meet(f, box(s)));
            }""", [foot, share, ".psc-hearts-lb"])
            check(f"{label}: the counter group touches neither hearts nor share",
                  not hits, str(hits))
            check(f"{label}: measured at phone width, not a desktop layout",
                  Q.evaluate("() => document.documentElement.clientWidth") == 390)
        Q.close()

    for label, url, tile, shot, lb, play, idle in (
            ("the published gallery", GALLERY, '.psc-fold[data-group="1"]',
             ".psc-shot", ".psc-lb", ".psc-play", "psc-idle"),
            ("the local report", REPORT, '.fold[data-group="1"]',
             ".shot", "#lb", "#lb-play", "idle")):
        T = br.new_context(has_touch=True, is_mobile=True,
                           viewport={"width": 390, "height": 844}).new_page()
        T.on("pageerror", lambda e: errors.append("touch: " + str(e)))
        T.goto(url.resolve().as_uri()); T.wait_for_timeout(500)
        run_touch(T, label, tile, shot, lb, play, idle)
        T.close()

    print("\n=== less motion, where the system asks for it ===")
    # The cross-fade is the only animation here, and it is the one thing
    # somebody with vestibular trouble would want gone.
    M = br.new_context(viewport={"width": 1200, "height": 850},
                       reduced_motion="reduce").new_page()
    M.on("pageerror", lambda e: errors.append("reduced: " + str(e)))
    M.goto(GALLERY.resolve().as_uri()); M.wait_for_timeout(400)
    M.click('.psc-fold[data-group="1"]'); M.wait_for_timeout(300)
    M.click(".psc-shot"); M.wait_for_timeout(300)
    check("the fade layer does not animate",
          M.eval_on_selector(".psc-fade", "e => getComputedStyle(e).transitionDuration")
          in ("0s", "0ms"),
          M.eval_on_selector(".psc-fade", "e => getComputedStyle(e).transitionDuration"))
    M.click(".psc-play"); M.wait_for_timeout(STEP_MS + 450)
    check("but the photographs still change",
          frame(M, ".psc-count-lb") == 2, str(frame(M, ".psc-count-lb")))

    check("no page errors anywhere", not errors, "; ".join(errors[:3]))
    br.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
