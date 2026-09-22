"""
The folder index in the LOCAL report.

The published gallery has its own suite; this is the same idea carried into
report.html, where the cards are written by Python rather than built by
JavaScript. Two things are being checked, and the second is the one that pays:

  * the index looks and behaves like the published one - tiles, covers, the
    breadcrumb, the two views, and which orderings each view offers;
  * no card exists until the folder holding it is opened. The cards are parsed
    into a <template>, which a browser does not lay out, and moved into the
    grid a folder at a time. A hidden card would satisfy none of the checks
    below, which is the point of counting elements rather than visible ones.

Everything the local report has and the published one does not - near
duplicates, the Photos/video-frames filter, the folder picker, the lightbox's
1:1 and copy-path - has to keep working inside the folder view, so that is
checked here too rather than assumed.
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

import photo_scout as ps                     # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------------------
# A library shaped like a real one: dated folder names, a Publish subfolder
# that must merge into its parent, a one-photograph shoot, and something loose
# at the root.
# ---------------------------------------------------------------------------
LIB = Path("/tmp/ps_lf"); OUT = Path("/tmp/ps_lf_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)

SPEC = [
    ("2010-03-12 - Arches National Park",             4),
    ("2010-03-12 - Arches National Park/Publish",     2),
    ("2011-06-28 - Wyoming and Tetons",               5),
    ("2011-10-22 - Spiral Jetty Sunset",              1),
    ("2009-05-01 - Bonneville",                       3),
    ("",                                              2),
]
# 'Arches National Park' collects six: four of its own and two from Publish.
GROUPS_EXPECTED = ["Arches National Park", "Bonneville", "Spiral Jetty Sunset",
                   "Wyoming and Tetons", "Unfiled"]
ARCHES, TOTAL = 6, sum(n for _, n in SPEC)

rng = np.random.default_rng(7)
n = 0
for folder, count in SPEC:
    d = LIB / folder if folder else LIB
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(count):
        n += 1
        im = Image.fromarray(rng.integers(0, 255, (12, 18, 3), dtype=np.uint8))
        # One portrait frame, so the mosaic's landscape crop is exercised on a
        # real image rather than only in the stylesheet.
        size = (620, 900) if n == 3 else (900, 620)
        im = im.resize(size, Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"201{n % 3}:06:{1 + n % 27:02d} 1{n % 6}:00:00"
        im.save(d / f"DSC_{n:04d}.JPG", "JPEG", quality=90, exif=ex)


class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        aes = float(np.clip(5.0 + np.sin(self.n * 1.7) * 0.6, 3.9, 6.1))
        return {"aesthetic_raw": aes,
                "nima_raw": float(np.clip(4.9 + np.cos(self.n * 1.1) * 0.5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}


ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT
ps.main(["--root", str(LIB)])
REPORT = OUT / "report.html"
h = REPORT.read_text(encoding="utf-8")

print("=== what Python wrote ===")
check("the cards are parked in a template, not in the grid",
      '<template id="cardsrc">' in h and '<div id="grid"></div>' in h)
# Counted inside the template, not across the whole document: the page's own
# script mentions these attribute names too.
CARDS = h.split('<template id="cardsrc">', 1)[1].split("</template>", 1)[0]
check("every card carries the gallery it belongs to",
      CARDS.count("data-group=") == TOTAL, str(CARDS.count("data-group=")))
check("and the thumbnail the tile needs before the card exists",
      CARDS.count("data-thumb=") == TOTAL, str(CARDS.count("data-thumb=")))
check("the outline colour was resolved, not left as a placeholder",
      "__FOLDLINE__" not in h and ps.DEFAULT_FOLDER_OUTLINE in h)
check("a subfolder merges into its parent",
      ps.folder_group("2010-03-12 - Arches National Park/Publish")
      == "Arches National Park")
check("a photograph at the library root is Unfiled",
      ps.folder_group("") == ps.UNFILED_GROUP)

print("\n=== in a browser ===")
with sync_playwright() as pw:
    br = pw.chromium.launch()
    errors = []
    P = br.new_context().new_page()
    P.on("pageerror", lambda e: errors.append(str(e)))
    P.set_viewport_size({"width": 1400, "height": 950})
    P.goto(REPORT.resolve().as_uri())
    P.wait_for_timeout(500)

    def vis(sel):
        return P.eval_on_selector_all(
            sel, "els => els.filter(e => e.offsetParent !== null).length")
    def built(sel=".card"):
        return P.eval_on_selector_all(sel, "e => e.length")
    def sorts():
        return P.eval_on_selector_all("#sort option", "e => e.map(x => x.value)")

    print("\n--- the index")
    check("the report opens on the folder index",
          vis(".fold") == len(GROUPS_EXPECTED), f"{vis('.fold')} tiles")
    names = P.eval_on_selector_all(".fold .foldname", "e => e.map(x => x.textContent)")
    check("tiles are A-Z by folder name, with the date stripped",
          names == GROUPS_EXPECTED, str(names))
    check("Unfiled is last whatever it would collate as", names[-1] == "Unfiled")

    # Not merely hidden: the card does not exist. This is what makes the index
    # cost the same whatever the library holds.
    check("no card has been built at all, not even a hidden one",
          built() == 0, f"{built()} in the DOM")
    check("but the counter still knows the whole library",
          f"{TOTAL} shown" in P.inner_text("#shown"), P.inner_text("#shown"))
    check("and the folder picker is put away, the tiles being the folders",
          vis("#folder") == 0)
    check("the index offers folder and date ordering",
          {"folder-asc", "folder-desc", "date-desc", "date-asc"} <= set(sorts()),
          str(sorts()))
    # A folder is not a photograph: it has neither a score nor a file name.
    check("but neither score nor file name, which a folder does not have",
          not any(s.startswith(("score-", "name-")) for s in sorts()), str(sorts()))

    print("\n--- the covers")
    mos = P.eval_on_selector_all(".fold", """els => els.map(e => ({
        name: e.querySelector('.foldname').textContent,
        imgs: [...e.querySelectorAll('.mosaic img')]
                .filter(i => i.style.display !== 'none').length,
        cls: e.querySelector('.mosaic').className,
        meta: e.querySelector('.foldmeta').textContent}))""")
    by = {m["name"]: m for m in mos}
    check("a six-photograph folder shows four",
          by["Arches National Park"]["imgs"] == 4, str(by["Arches National Park"]))
    check("a one-photograph folder fills the tile",
          by["Spiral Jetty Sunset"]["imgs"] == 1 and
          by["Spiral Jetty Sunset"]["cls"].endswith("n1"),
          str(by["Spiral Jetty Sunset"]))
    check("the tile says how many are inside",
          by["Arches National Park"]["meta"].startswith(f"{ARCHES} photos"),
          by["Arches National Park"]["meta"])
    cells = P.eval_on_selector_all(".fold .mosaic img", """els => els
        .filter(i => i.style.display !== 'none')
        .map(i => { const r = i.getBoundingClientRect();
                    return Math.round(r.height - r.width); })""")
    check("no cell is taller than it is wide, portrait frames included",
          all(d <= 1 for d in cells), str(sorted(cells)[-3:]))
    heights = P.eval_on_selector_all(
        ".fold", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
    check("every tile is the same height", len(set(heights)) == 1, str(set(heights)))

    print("\n--- opening one")
    P.click('.fold[data-group="%s"]' % P.eval_on_selector(
        ".fold .foldname:text-is('Arches National Park')",
        "e => e.parentElement.dataset.group"))
    P.wait_for_timeout(350)
    check("the index gives way to that folder's photographs",
          vis(".card") == ARCHES and vis(".fold") == 0, f"{vis('.card')} cards")
    check("only that folder's cards were built",
          built() == ARCHES, f"{built()} in the DOM")
    check("the breadcrumb names it",
          P.inner_text("#crumb h2") == "Arches National Park")
    check("and counts it", f"{ARCHES} photos" in P.inner_text("#crumbn"),
          P.inner_text("#crumbn"))
    check("a folder opens on the best photographs first",
          P.eval_on_selector("#sort", "e => e.value") == "score-desc")
    check("folder ordering is not offered inside a folder",
          not any(s.startswith("folder-") for s in sorts()), str(sorts()))
    check("but score and file name are back",
          {"score-desc", "name-asc"} <= set(sorts()), str(sorts()))
    # The subfolder still says something; the parent's own photographs do not.
    labels = P.eval_on_selector_all(
        ".card:not(.hidden) .folder",
        "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent)")
    check("a merged subfolder keeps its label, the parent drops its own",
          set(labels) == {"Arches National Park/Publish"} or
          set(labels) == {"Arches National Park\\Publish"}, str(set(labels)))

    print("\n--- the lightbox stops at the folder's edge")
    P.click(".card:not(.hidden) img")
    P.wait_for_timeout(300)
    check("it opens", P.eval_on_selector("#lb", "e => e.classList.contains('open')"))
    check("and knows it is walking six, not seventeen",
          P.inner_text("#lb-count").strip().endswith(f"/{ARCHES}"),
          P.inner_text("#lb-count"))
    check("the 1:1 and copy-path controls came with it",
          vis("#lb-zoom") == 1 and vis("#lb-copy") == 1)
    P.keyboard.press("Escape"); P.wait_for_timeout(250)

    print("\n--- tagging a card that was built late")
    inp = P.query_selector(".card:not(.hidden) .taginput")
    inp.fill("zebra"); inp.press("Enter"); P.wait_for_timeout(250)
    check("a card built on opening a folder takes a tag",
          P.eval_on_selector(".card:not(.hidden)", "c => c.dataset.tags") == "|zebra|",
          P.eval_on_selector(".card:not(.hidden)", "c => c.dataset.tags"))

    print("\n--- the local-only filters, inside a folder")
    P.select_option("#kind", "video")
    P.wait_for_timeout(250)
    check("Video frames only empties a folder of stills",
          vis(".card") == 0, f"{vis('.card')} cards")
    check("and the breadcrumb admits none are showing",
          P.inner_text("#crumbn").startswith("0 of"), P.inner_text("#crumbn"))
    P.select_option("#kind", "all"); P.wait_for_timeout(250)
    check("putting it back restores the folder", vis(".card") == ARCHES)
    P.check("#dups"); P.wait_for_timeout(250)
    check("show near-duplicates never removes anything", vis(".card") >= ARCHES)
    P.uncheck("#dups"); P.wait_for_timeout(250)

    print("\n--- back to the index")
    P.click("#back"); P.wait_for_timeout(300)
    check("the tiles come back", vis(".fold") == len(GROUPS_EXPECTED))
    check("and nothing further was built", built() == ARCHES, str(built()))
    check("the index remembered its own ordering",
          P.eval_on_selector("#sort", "e => e.value") == "folder-asc")

    print("\n--- searching from the index")
    # The filter runs on the arrays harvested at load, so it reaches folders
    # whose cards have never existed.
    P.fill("#q", "Bonneville"); P.wait_for_timeout(400)
    check("a search reaches a folder nobody has opened",
          vis(".fold") == 1, f"{vis('.fold')} tiles")
    check("without building a single extra card", built() == ARCHES, str(built()))
    check("and the count reads folders and photographs",
          P.inner_text("#shown").startswith("1 folder · 3"),
          P.inner_text("#shown"))
    P.fill("#q", "nothingmatchesthis"); P.wait_for_timeout(400)
    check("a search that matches nothing says so", vis("#empty") == 1)
    check("and shows no tiles", vis(".fold") == 0)
    P.fill("#q", ""); P.wait_for_timeout(400)
    check("clearing it brings every folder back", vis(".fold") == len(GROUPS_EXPECTED))

    print("\n--- All photos")
    P.click("#views button[data-view='all']"); P.wait_for_timeout(600)
    check("every photograph is on one page",
          vis(".card") == TOTAL and vis(".fold") == 0, f"{vis('.card')} cards")
    check("which is the one view that does build them all",
          built() == TOTAL, str(built()))
    check("no breadcrumb in the flat view", vis("#crumb") == 0)
    check("the folder picker comes back, folders spanning the page now",
          vis("#folder") == 1)
    check("and every ordering is offered",
          {"score-desc", "date-asc", "folder-asc", "name-desc"} <= set(sorts()),
          str(sorts()))
    opts = P.eval_on_selector_all("#folder option", "e => e.map(x => x.textContent)")
    P.select_option("#folder",
                    label=[o for o in opts if o.startswith("Bonneville")][0])
    P.wait_for_timeout(300)
    check("the picker still narrows the flat page", vis(".card") == 3,
          f"{vis('.card')} cards")
    P.select_option("#folder", "all"); P.wait_for_timeout(300)

    print("\n--- the choice is remembered")
    # A new page in the SAME context, which is what shares localStorage. A
    # second context would be a different browser as far as the report is
    # concerned, and would rightly know nothing.
    P2 = P.context.new_page()
    P2.on("pageerror", lambda e: errors.append(str(e)))
    P2.goto(REPORT.resolve().as_uri()); P2.wait_for_timeout(500)
    check("a second visit opens on All photos",
          P2.eval_on_selector_all(".card", "e => e.length") == TOTAL,
          str(P2.eval_on_selector_all(".card", "e => e.length")))
    P2.click("#views button[data-view='folders']"); P2.wait_for_timeout(300)
    P3 = P.context.new_page()
    P3.on("pageerror", lambda e: errors.append(str(e)))
    P3.goto(REPORT.resolve().as_uri()); P3.wait_for_timeout(500)
    check("and switching back is remembered too",
          P3.eval_on_selector_all(".fold", "e => e.length") == len(GROUPS_EXPECTED)
          and P3.eval_on_selector_all(".card", "e => e.length") == 0,
          f"{P3.eval_on_selector_all('.fold', 'e => e.length')} tiles, "
          f"{P3.eval_on_selector_all('.card', 'e => e.length')} cards")

    print("\n--- the colours tell the controls apart")
    P3.click("#views button[data-view='all']"); P3.wait_for_timeout(300)
    def bg(sel):
        return P3.eval_on_selector(sel, "e => getComputedStyle(e).backgroundColor")
    check("the active view and the active band are different colours",
          bg("#views button.on") != bg("button[data-f].on"),
          f"{bg('#views button.on')} vs {bg('button[data-f].on')}")

    print("\n--- a phone in portrait")
    ph = br.new_context(viewport={"width": 390, "height": 844},
                        has_touch=True, is_mobile=True).new_page()
    ph.on("pageerror", lambda e: errors.append(str(e)))
    ph.goto(REPORT.resolve().as_uri()); ph.wait_for_timeout(500)
    ph.click("#views button[data-view='all']"); ph.wait_for_timeout(300)
    check("the controls do not push the page sideways",
          ph.evaluate("document.documentElement.scrollWidth <= "
                      "document.documentElement.clientWidth + 1"))
    ph.evaluate("window.scrollTo(0, 900)"); ph.wait_for_timeout(200)
    pinned = ph.eval_on_selector("header", "e => e.getBoundingClientRect().bottom")
    check("scrolled down, the pinned header takes under a quarter of the screen",
          pinned < 844 * 0.25, f"{pinned:.0f}px of 844")
    check("with the title scrolled away and the controls still in reach",
          ph.eval_on_selector("h1", "e => e.getBoundingClientRect().bottom") <= 0
          and ph.eval_on_selector(".controls", "e => e.getBoundingClientRect().top") >= 0)

    check("no page errors anywhere", not errors, "; ".join(errors[:3]))
    br.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
