"""
WCAG 2.2 Level AA, on both pages this project produces.

An accessibility fix is the kind that rots quietly: nothing looks wrong when it
is undone. So this drives the real pages in a browser and measures what a
person using a keyboard or a screen reader would actually get - names, roles,
focus, contrast, target size - rather than grepping the markup for attributes.

The criteria checked here, and why each is checked this way:

  1.1.1  every image is either named or explicitly decorative
  1.3.1  the overlay says it is a dialog; toggles say whether they are on
  1.4.3  text contrast, computed from what the browser painted
  1.4.10 no sideways scroll at 320px, the narrowest width the standard names
  2.1.1  everything clickable can be reached and operated from the keyboard,
         which is where a click handler on an <img> fails
  2.1.2  the overlay does not trap the keyboard, and Tab stays inside it
  2.4.3  focus goes somewhere sensible when a folder or a photograph opens or
         closes, instead of being dropped at the top of the document
  2.4.7  a focus indicator that can be seen on a dark page
  2.4.11 a focused card is not hidden under the pinned toolbar
  2.5.8  pointer targets are at least 24px, bar inline links in a sentence
  4.1.2  every control has a name
  4.1.3  a message that appears without a page change is announced

Both pages are built from the same fixtures the folder suites use, so a change
that breaks one of them breaks this too.
"""
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


# ---------------------------------------------------------------------------
# The two pages
# ---------------------------------------------------------------------------
OUT = Path("/tmp/ps_a11y"); LIB = Path("/tmp/ps_a11y_lib")
for d in (OUT, LIB):
    shutil.rmtree(d, ignore_errors=True)
OUT.mkdir(parents=True)

SPEC = [("2010-03-12 - Arches National Park", 3),
        ("2011-06-28 - Wyoming and Tetons", 4),
        ("", 1)]

# The published gallery, wrapped as Ghost serves it.
items, n = [], 0
for folder, count in SPEC:
    for _ in range(count):
        n += 1
        items.append({
            "photo_id": f"{n:016x}", "verdict": "TOP PICK" if n % 3 == 0 else "STRONG",
            "score": 60 + n, "filename": f"DSC_{n:04d}.NEF", "folder": folder,
            "taken_at": f"2011-06-2{n % 9} 1{n % 6}:00:00",
            "resolution": "6000 x 4000 - 24.0 MP", "note": "Aesthetic 70 · Technical 60",
            "thumb_url": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
            "preview_url": "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
        })
GALLERY = OUT / "gallery.html"
GALLERY.write_text(
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Photo Scout Gallery</title></head>"
    "<body style='margin:0;background:#111'>"
    + pg.build_gallery_html(items, {}) + "</body></html>", encoding="utf-8")

# The local report, from a real scoring run over a small library.
rng = np.random.default_rng(3)
n = 0
for folder, count in SPEC:
    d = LIB / folder if folder else LIB
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(count):
        n += 1
        im = Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8))
        im = im.resize((900, 620), Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"201{n % 3}:06:{1 + n % 27:02d} 1{n % 6}:00:00"
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


def contrast(a, b):
    """WCAG ratio between two 'rgb(r, g, b)' strings the browser computed."""
    import re
    return ps.contrast(*[tuple(int(v) / 255 for v in re.findall(r"\d+", c)[:3])
                         for c in (a, b)])


# Interactive things, as a pointer sees them. Links inside a run of text are
# exempt from 2.5.8, and the footer credit is the only one on either page.
TARGETS = "button, a, input, select, [tabindex]:not([tabindex='-1'])"
UNDERSIZED = """els => els.filter(e => e.offsetParent !== null)
    .filter(e => !e.closest('footer, .psc-foot'))
    // A control wrapped in a label is activated by clicking anywhere in that
    // label, so the label is the target, not the 13px box drawn inside it.
    .map(e => e.closest('label') || e)
    .map(e => { const r = e.getBoundingClientRect();
      return {what: e.tagName + '.' + (e.className || ''),
              name: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 20),
              w: Math.round(r.width), h: Math.round(r.height)}; })
    .filter(t => t.w < 24 || t.h < 24)"""

# Every control needs a name from somewhere: its text, an aria-label, or a
# label element. A title alone is not one a screen reader can be relied on for.
UNNAMED = """els => els.filter(e => e.offsetParent !== null)
    .filter(e => !(e.textContent || '').trim()
              && !e.getAttribute('aria-label')
              && !e.getAttribute('aria-labelledby')
              && !e.closest('label')
              && !(e.id && document.querySelector('label[for="' + e.id + '"]')))
    .map(e => e.tagName + '#' + (e.id || '') + '.' + (e.className || ''))"""

# An <img> either says something, or says it says nothing. A missing alt is the
# one state a screen reader cannot work with: it falls back to the file name.
NO_ALT = """els => els.filter(e => !e.hasAttribute('alt'))
    .map(e => (e.className || e.id || 'img') + ' ' + (e.src || '').slice(0, 40))"""


def audit(page, name, tile, shot, bar, dialog, closer):
    print(f"\n=== {name} ===")

    print("-- names and roles (4.1.2, 1.1.1) --")
    bad = page.eval_on_selector_all(TARGETS, UNNAMED)
    check("every control on the index has a name", not bad, str(bad))
    check("every image says what it is, or that it is decorative",
          not page.eval_on_selector_all("img", NO_ALT),
          str(page.eval_on_selector_all("img", NO_ALT)))
    check("the overlay is announced as a modal dialog",
          page.eval_on_selector(dialog, "e => e.getAttribute('role')") == "dialog"
          and page.eval_on_selector(dialog, "e => e.getAttribute('aria-modal')") == "true")

    print("-- a message that appears is announced (4.1.3) --")
    check("the toast is a status region",
          page.eval_on_selector(".psc-toast, #toast",
                                "e => e.getAttribute('role')") == "status")

    print("-- pointer targets are 24px (2.5.8) --")
    small = page.eval_on_selector_all(TARGETS, UNDERSIZED)
    check("nothing on the index is too small to hit", not small, str(small))

    print("-- the keyboard can do everything the mouse can (2.1.1, 2.4.3) --")
    page.focus(tile)
    page.keyboard.press("Enter"); page.wait_for_timeout(400)
    check("Enter on a folder tile opens the folder",
          page.eval_on_selector_all(shot, "e => e.length") > 0)
    check("and focus moves to the way out, not to the top of the page",
          page.evaluate("() => (document.activeElement.textContent || '').trim()")
          .endswith("All folders"),
          page.evaluate("() => document.activeElement.className"))
    small = page.eval_on_selector_all(TARGETS, UNDERSIZED)
    check("nothing inside a folder is too small either", not small, str(small))
    bad = page.eval_on_selector_all(TARGETS, UNNAMED)
    check("and every control in a card is named", not bad, str(bad))

    print("-- the focus indicator can be seen (2.4.7) --")
    page.keyboard.press("Tab"); page.wait_for_timeout(120)
    ring = page.evaluate("""() => { const s = getComputedStyle(document.activeElement);
        return [s.outlineStyle, parseFloat(s.outlineWidth), s.outlineColor,
                getComputedStyle(document.body).backgroundColor]; }""")
    check("the focused control draws a real outline",
          ring[0] != "none" and ring[1] >= 2, str(ring[:2]))
    check("bright enough to see against the page",
          contrast(ring[2], ring[3]) >= 3, f"{contrast(ring[2], ring[3]):.2f}:1")

    print("-- a focused card is not hidden by the pinned toolbar (2.4.11) --")
    page.eval_on_selector(shot, "e => e.focus()")
    page.wait_for_timeout(250)
    pos = page.evaluate("""(barSel) => {
        const a = document.activeElement.getBoundingClientRect();
        const b = document.querySelector(barSel).getBoundingClientRect();
        return [Math.round(a.top), Math.round(b.bottom)]; }""", bar)
    check("the focused thumbnail sits below it", pos[0] >= pos[1], str(pos))

    print("-- the overlay keeps the keyboard, and gives it back (2.1.2, 2.4.3) --")
    came_from = page.evaluate("() => document.activeElement.getAttribute('aria-label')")
    page.keyboard.press("Enter"); page.wait_for_timeout(500)
    check("Enter on a thumbnail opens the overlay",
          page.eval_on_selector(dialog, "e => e.classList.contains('open')"))
    check("and focus goes into it",
          page.eval_on_selector(closer, "e => e === document.activeElement"),
          page.evaluate("() => document.activeElement.className"))
    for _ in range(12):
        page.keyboard.press("Tab"); page.wait_for_timeout(40)
    check("Tab cannot walk out of it",
          page.evaluate("(d) => !!document.activeElement.closest(d)", dialog),
          page.evaluate("() => document.activeElement.className"))
    for _ in range(12):
        page.keyboard.press("Shift+Tab"); page.wait_for_timeout(40)
    check("nor can Shift+Tab",
          page.evaluate("(d) => !!document.activeElement.closest(d)", dialog))
    page.keyboard.press("Escape"); page.wait_for_timeout(400)
    check("Escape closes it", not page.eval_on_selector(
        dialog, "e => e.classList.contains('open')"))
    check("and hands the keyboard back where it was",
          page.evaluate("() => document.activeElement.getAttribute('aria-label')")
          == came_from,
          str(page.evaluate("() => document.activeElement.getAttribute('aria-label')")))

    print("-- text contrast, as painted (1.4.3) --")
    worst = page.evaluate("""() => {
        const lum = (c) => { const [r,g,b] = c.match(/\\d+/g).slice(0,3)
            .map(v => { v = v/255; return v <= .03928 ? v/12.92
                                                      : Math.pow((v+.055)/1.055, 2.4); });
            return .2126*r + .7152*g + .0722*b; };
        const ratio = (a,b) => { const [x,y] = [lum(a), lum(b)].sort((p,q) => q-p);
            return (x+.05)/(y+.05); };
        const bg = (el) => { let e = el;
            while (e) { const c = getComputedStyle(e).backgroundColor;
                if (c && !c.startsWith('rgba(0, 0, 0, 0)')) return c;
                e = e.parentElement; }
            return 'rgb(255, 255, 255)'; };
        let out = null;
        document.querySelectorAll('*').forEach(el => {
            if (el.offsetParent === null) return;
            const t = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
            if (!t) return;
            const s = getComputedStyle(el);
            const size = parseFloat(s.fontSize);
            const large = size >= 24 || (size >= 18.66 && +s.fontWeight >= 700);
            const r = ratio(s.color, bg(el));
            const need = large ? 3 : 4.5;
            if (r < need && (!out || r < out.ratio))
                out = {ratio: +r.toFixed(2), need, size,
                       what: el.tagName + '.' + (el.className || ''),
                       text: el.textContent.trim().slice(0, 30)};
        });
        return out; }""")
    check("every run of text clears its threshold", worst is None, str(worst))

    print("-- 320px wide, which the standard names (1.4.10) --")
    page.set_viewport_size({"width": 320, "height": 700}); page.wait_for_timeout(500)
    check("the page does not scroll sideways",
          page.evaluate("() => document.documentElement.scrollWidth "
                        "<= document.documentElement.clientWidth"),
          str(page.evaluate("() => [document.documentElement.scrollWidth, "
                            "document.documentElement.clientWidth]")))
    page.set_viewport_size({"width": 1400, "height": 950}); page.wait_for_timeout(300)


with sync_playwright() as pw:
    br = pw.chromium.launch()
    errors = []

    P = br.new_context(viewport={"width": 1400, "height": 950}).new_page()
    P.on("pageerror", lambda e: errors.append(str(e)))
    P.goto(GALLERY.resolve().as_uri()); P.wait_for_timeout(600)
    audit(P, "the published gallery", ".psc-fold", ".psc-shot", ".psc-bar",
          ".psc-lb", ".psc-lb .x")
    # Only the published page has toggles for a view and a band.
    P.evaluate("() => document.querySelector('.psc-back').click()"); P.wait_for_timeout(300)
    pressed = P.eval_on_selector_all(
        ".psc-views button, .psc-bar button[data-band]",
        "els => els.map(e => [e.textContent.trim(), e.getAttribute('aria-pressed'),"
        " e.classList.contains('on')])")
    check("every toggle says whether it is on (1.3.1)",
          all(p[1] == ("true" if p[2] else "false") for p in pressed), str(pressed))
    P.click(".psc-vall"); P.wait_for_timeout(300)
    after = P.eval_on_selector_all(
        ".psc-views button",
        "els => els.map(e => [e.textContent.trim(), e.getAttribute('aria-pressed')])")
    check("and says so again once it changes", after[1][1] == "true", str(after))

    R = br.new_context(viewport={"width": 1400, "height": 950}).new_page()
    R.on("pageerror", lambda e: errors.append(str(e)))
    R.goto(REPORT.resolve().as_uri()); R.wait_for_timeout(600)
    check("the local report says what language it is in (3.1.1)",
          R.evaluate("() => document.documentElement.lang") == "en",
          R.evaluate("() => document.documentElement.lang"))
    check("and has one main landmark to skip the toolbar with (2.4.1)",
          R.eval_on_selector_all("main", "e => e.length") == 1
          and R.eval_on_selector("main", "e => !!e.querySelector('#grid, #folders')"))
    audit(R, "the local report", ".fold", ".shot", "header", "#lb", "#lb-close")

    check("no page errors anywhere", not errors, "; ".join(errors[:3]))
    br.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
