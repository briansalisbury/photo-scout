"""
Folder galleries in the published Ghost page.

Two halves. The first checks the grouping Python does - which folders merge,
what they are called, and that the payload does not repeat a folder name once
per photograph. The second drives the real page in a browser: entering a
folder, coming back, the mosaic covers, and the rule that matters most - the
lightbox must never walk out of the folder you opened.
"""
import json
import re
import shutil
import sys
from pathlib import Path

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_scout as ps                     # noqa: E402
import photo_scout_ghost as pg               # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------------------
# A library shaped like a real one: dated folder names, a couple of Publish
# subfolders, a one-photograph shoot, and something loose at the root.
# ---------------------------------------------------------------------------
SPEC = [
    ("2010-03-12 - Arches National Park",           3),
    ("2010-03-12 - Arches National Park\\Publish",  2),
    ("2011-06-28 - Wyoming and Tetons",             5),
    ("2011-06-28 - Wyoming and Tetons\\Old Faithful", 1),
    ("2011-10-22 - Spiral Jetty Sunset",            1),
    ("2009-05-01 - Bonneville",                     2),
    ("",                                            2),
]

items, n = [], 0
for folder, count in SPEC:
    for _ in range(count):
        n += 1
        items.append({
            "photo_id": f"{n:016x}",
            "verdict": "TOP PICK" if n % 3 == 0 else "STRONG",
            "score": 60 + n,
            "filename": f"DSC_{n:04d}.NEF",
            "folder": folder,
            "taken_at": f"201{n % 3}-06-28 1{n % 6}:00:00",
            "resolution": "6000 x 4000 - 24.0 MP",
            "note": "Aesthetic 70 · Technical 60 · Desert terrain",
            "thumb_url": f"/t/{n}.jpg",
            "preview_url": f"/p/{n}.jpg",
        })
TOTAL = len(items)


print("=== which folder a photograph belongs to ===")
check("a dated folder loses its date",
      pg.folder_group("2010-03-12 - Arches National Park") == "Arches National Park")
check("a subfolder merges into its parent",
      pg.folder_group("2010-03-12 - Arches National Park\\Publish")
      == "Arches National Park")
check("a forward-slash subfolder merges too",
      pg.folder_group("2010-03-12 - Arches National Park/Publish")
      == "Arches National Park")
check("a deeper subfolder still merges to the top level",
      pg.folder_group("2011-06-28 - Wyoming\\Day 2\\Publish") == "Wyoming")
check("a photograph at the library root is Unfiled",
      pg.folder_group("") == pg.UNFILED_GROUP == "Unfiled")
check("an undated folder keeps its whole name",
      pg.folder_group("Portfolio") == "Portfolio")
# strip_folder_date leaves a bare year alone, and the group name must not
# quietly disagree with the folder label the cards already show.
check("a bare year is not mistaken for a date",
      pg.folder_group("2011 Wyoming") == "2011 Wyoming")


print("\n=== the folder tile outline ===")
# The tile keeps the page's own card; only the outline is chosen. So the thing
# worth testing is not "this hex value" but "whatever colour goes in, the
# folder shape is actually visible against that card".
SWATCHES = [
    "#b09468",   # the default
    "#3a2f1d", "#000000",            # too dark to see unaided; must be lifted
    "#ffffff", "#2e6f4f", "#3b5bdb", # bright, and two saturated
    "#fff",                          # short form
]
for swatch in SWATCHES:
    pal = pg.folder_palette(swatch)
    check(f"{swatch}: both colours are real hex values",
          all(re.fullmatch(r"#[0-9a-f]{6}", pal[k]) for k in ("line", "hover")),
          str(pal))
    seen = pg.contrast(pg._hex_rgb(pal["line"]), pg._hex_rgb(pg.TILE_BG))
    # 3:1 is the WCAG floor for a non-text interface element, which is what a
    # border is. Below it the folder shape is simply not there.
    check(f"{swatch}: the outline is visible on the card",
          seen >= pg.OUTLINE_MIN_CONTRAST - 0.01, f"{seen:.2f}:1 {pal['line']}")
    lit = pg.contrast(pg._hex_rgb(pal["hover"]), pg._hex_rgb(pg.TILE_BG))
    check(f"{swatch}: hover is brighter than the resting line",
          lit >= seen, f"{lit:.2f} vs {seen:.2f}")

check("a colour already bright enough is left exactly as given",
      pg.folder_palette("#b09468")["line"] == "#b09468")
check("and one too dark is lifted rather than rejected",
      pg.folder_palette("#3a2f1d")["line"] != "#3a2f1d",
      pg.folder_palette("#3a2f1d")["line"])
# Lifting must not turn it into a different colour.
import colorsys as _cs                                             # noqa: E402
_h_in = _cs.rgb_to_hls(*pg._hex_rgb("#3a2f1d"))[0]
_h_out = _cs.rgb_to_hls(*pg._hex_rgb(pg.folder_palette("#3a2f1d")["line"]))[0]
check("a lifted colour keeps its hue", abs(_h_in - _h_out) < 0.02,
      f"{_h_in:.3f} -> {_h_out:.3f}")

check("the default sits at the same weight as the page's muted text",
      abs(pg.contrast(pg._hex_rgb(pg.DEFAULT_FOLDER_OUTLINE), pg._hex_rgb(pg.TILE_BG))
          - pg.contrast(pg._hex_rgb("#9a9a9a"), pg._hex_rgb(pg.TILE_BG))) < 0.5,
      "an outline much louder than the dates beside it would read as a highlight")

for bad in ("nonsense", "#12345", "", "#gggggg", "rgb(1,2,3)"):
    try:
        pg.folder_palette(bad)
        check(f"{bad!r} is rejected", False)
    except ValueError:
        check(f"{bad!r} is rejected rather than rendered", True)


print("\n=== the payload ===")
html = pg.build_gallery_html(items, {})
blob = re.search(r'<script type="application/json" class="psc-data">(.*?)</script>',
                 html, re.S).group(1)
data = json.loads(blob.replace("<\\/", "</").replace("<\\u0021--", "<!--"))

check("photographs and gallery names travel separately",
      isinstance(data, dict) and set(data) == {"g", "p"}, str(list(data))[:60])
check("every photograph is in the payload", len(data["p"]) == TOTAL)
check("the seven folders merged into five galleries",
      len(data["g"]) == 5, str(data["g"]))
check("named as expected",
      set(data["g"]) == {"Arches National Park", "Bonneville", "Spiral Jetty Sunset",
                         "Unfiled", "Wyoming and Tetons"}, str(data["g"]))
check("the order is stable and A-Z", data["g"] == sorted(data["g"]), str(data["g"]))
check("every photograph carries a gallery index",
      all(isinstance(p.get("g"), int) and 0 <= p["g"] < len(data["g"])
          for p in data["p"]))

counts = {}
for p in data["p"]:
    counts[data["g"][p["g"]]] = counts.get(data["g"][p["g"]], 0) + 1
check("Arches gathers its Publish subfolder", counts["Arches National Park"] == 5,
      str(counts))
check("Wyoming gathers Old Faithful", counts["Wyoming and Tetons"] == 6, str(counts))
check("the loose photographs are Unfiled together", counts["Unfiled"] == 2, str(counts))

# The whole gallery has to fit inside one Lexical document, so a folder name
# spelled out once per photograph is a cost worth not paying.
check("a photograph names its gallery by number, not in words",
      all(isinstance(p["g"], int) for p in data["p"]) and '"g":"' not in blob)
spelled_out = ps.script_json(
    {"g": data["g"], "p": [dict(p, g=data["g"][p["g"]]) for p in data["p"]]},
    ensure_ascii=False)
check("which is smaller than spelling it out", len(blob) < len(spelled_out),
      f"{len(blob)} vs {len(spelled_out)} bytes")


print("\n=== what the page ships with ===")
check("the folder index container is present", 'class="psc-folders"' in html)
check("so is the breadcrumb", 'class="psc-crumb"' in html)
check("and the two view buttons",
      'data-view="folders"' in html and 'data-view="all"' in html)
check("folders is the default view",
      'psc-view-folders' in html and 'data-view="folders"' in html)
flat = pg.build_gallery_html(items, {}, view="all")
check("--view all opens flat instead",
      'psc-view-folders' not in flat.split('<style>')[0] and
      'data-view="all"' in flat.split('<style>')[0])
check("but the flat page still carries the folder machinery",
      'class="psc-folders"' in flat)

tinted = pg.build_gallery_html(items, {}, folder_outline="#2e6f4f")
tpal = pg.folder_palette("#2e6f4f")
check("--folder-outline reaches the stylesheet",
      f"--psc-fold-line:{tpal['line']}" in tinted, tpal["line"])
check("and the default is used when it is not given",
      f"--psc-fold-line:{pg.DEFAULT_FOLDER_OUTLINE}" in html,
      pg.DEFAULT_FOLDER_OUTLINE)
check("no colour placeholder survives into the page",
      "__FOLD" not in html and "__FOLD" not in tinted)
# The tile must keep the page's own card, or this stops being an outline.
check("the tile background is still the page's card colour",
      ".psc-fold{background:var(--psc-card)" in html)

# A media query carries no extra specificity, so a base rule declared later
# wins. This has bitten this file three times; assert the ordering instead.
css = html.split('<style>')[1].split('</style>')[0]
check("the narrow-screen overrides are still last in the stylesheet",
      css.rindex('@media (max-width:600px)') > css.index('.psc-folders{display:none'),
      "a base .psc-folders rule after the media query would silently win")


# ---------------------------------------------------------------------------
print("\n=== in a browser ===")
OUT = Path("/tmp/fold_out")
shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True)
# Inline images, so the page needs no image server and no network. Deliberately
# mixed orientations: a portrait thumbnail used to stretch its grid row and take
# the whole tile with it, which is the bug the mosaic geometry checks guard.
import base64, io                                                  # noqa: E402
from PIL import Image                                              # noqa: E402

def pix(w, h, rgb):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

SHAPES = [(60, 160), (160, 60), (100, 100), (80, 120)]      # tall, wide, square, tall
for k, it in enumerate(items):
    w, h = SHAPES[k % len(SHAPES)]
    it["thumb_url"] = it["preview_url"] = pix(w, h, (40 + k * 9 % 200, 90, 140))
page_html = pg.build_gallery_html(items, {})
(OUT / "live.html").write_text(
    "<!doctype html><meta charset='utf-8'>"
    "<body style='margin:0;background:#000'>" + page_html, encoding="utf-8")

from playwright.sync_api import sync_playwright                      # noqa: E402

with sync_playwright() as pw:
    br = pw.chromium.launch()
    errors = []
    ctx = br.new_context()
    P = ctx.new_page()
    P.on("pageerror", lambda e: errors.append(str(e)))
    P.set_viewport_size({"width": 1400, "height": 950})
    P.goto((OUT / "live.html").as_uri())
    P.wait_for_timeout(500)

    def vis(sel):
        return P.eval_on_selector_all(
            sel, "els => els.filter(e => e.offsetParent !== null).length")

    check("the page opens on the folder index", vis(".psc-fold") == 5,
          f"{vis('.psc-fold')} tiles visible")
    check("and the photo grid is not showing", vis(".psc-card") == 0)

    # ---- built on demand ---------------------------------------------------
    # Not merely hidden: the index does not build a card at all until somebody
    # opens the folder it belongs to. This is what keeps a library of ten
    # thousand photographs opening as fast as one of five hundred, so it is
    # worth a check that a hidden card cannot satisfy.
    def built(sel=".psc-card"):
        return P.eval_on_selector_all(sel, "e => e.length")

    check("the index builds no cards at all, not even hidden ones",
          built() == 0, f"{built()} in the DOM")
    check("and the counter still knows the whole library",
          re.search(r"of (\d+)$", P.inner_text(".psc-count")).group(1) == str(TOTAL),
          P.inner_text(".psc-count"))
    P.click(".psc-fold"); P.wait_for_timeout(300)
    made = built()
    check("opening a folder builds that folder's photographs",
          0 < made < TOTAL, f"{made} of {TOTAL}")
    check("and every one of them belongs to it",
          P.eval_on_selector_all(
              ".psc-card", "e => new Set(e.map(c => c.dataset.group)).size") == 1)
    P.click(".psc-back"); P.wait_for_timeout(250)
    check("coming back builds nothing further", built() == made,
          f"{built()} vs {made}")
    # The filter runs on the payload, not on the grid, so it reaches
    # photographs whose cards have never existed.
    P.fill(".psc-q", "Bonneville"); P.wait_for_timeout(350)
    check("a search reaches folders nobody has opened",
          vis(".psc-fold") == 1 and built() == made,
          f"{vis('.psc-fold')} tiles, {built()} cards")
    P.fill(".psc-q", ""); P.wait_for_timeout(300)
    check("the counter names folders and photographs",
          re.match(r"5 folders · \d+ of \d+",
                   P.inner_text(".psc-count")), P.inner_text(".psc-count"))
    check("the sort box is on screen", vis(".psc-sort") == 1)
    check("and opens the index on Folder A-Z",
          P.eval_on_selector(".psc-sort", "e => e.value") == "folder-asc",
          P.eval_on_selector(".psc-sort", "e => e.value"))
    idx_opts = P.eval_on_selector_all(".psc-sort option", "e=>e.map(x=>x.value)")
    check("offering folder ordering, which is what tiles need",
          {"folder-asc", "folder-desc", "date-desc", "date-asc"} <= set(idx_opts),
          str(idx_opts))
    # A folder is not a photograph: it has neither a score nor a file name.
    check("but neither score nor file name, which a folder does not have",
          not any(o.startswith(("score-", "name-")) for o in idx_opts),
          str(idx_opts))

    names = P.eval_on_selector_all(".psc-fold .psc-foldname", "e=>e.map(x=>x.textContent)")
    check("tiles are A-Z by folder name",
          names == ["Arches National Park", "Bonneville", "Spiral Jetty Sunset",
                    "Wyoming and Tetons", "Unfiled"], str(names))
    check("Unfiled is last whatever it would collate as", names[-1] == "Unfiled")

    # The cover is the folder's four best, not its first four.
    mos = P.eval_on_selector_all(
        ".psc-fold", """els => els.map(e => ({
            name: e.querySelector('.psc-foldname').textContent,
            cls: e.querySelector('.psc-mosaic').className,
            shown: [].slice.call(e.querySelectorAll('.psc-mosaic img'))
                     .filter(i => i.style.display !== 'none').length,
            meta: e.querySelector('.psc-foldmeta').textContent}))""")
    by = {m["name"]: m for m in mos}
    check("a five-photograph folder shows four",
          by["Arches National Park"]["shown"] == 4 and
          by["Arches National Park"]["cls"].endswith("n4"), str(by["Arches National Park"]))
    check("a one-photograph folder fills the tile",
          by["Spiral Jetty Sunset"]["shown"] == 1 and
          by["Spiral Jetty Sunset"]["cls"].endswith("n1"), str(by["Spiral Jetty Sunset"]))
    check("a two-photograph folder splits it",
          by["Unfiled"]["shown"] == 2 and by["Unfiled"]["cls"].endswith("n2"),
          str(by["Unfiled"]))
    check("the tile says how many are inside",
          by["Arches National Park"]["meta"].startswith("5 photos"),
          by["Arches National Park"]["meta"])

    print("\n--- the mosaic holds its shape whatever shape the photographs are ---")
    # A plain 1fr grid track has a min-content floor, so one portrait thumbnail
    # used to stretch its row to the full height of the photograph and drag the
    # tile with it. Every tile must be the same height and no cell may be
    # taller than it is wide.
    heights = P.eval_on_selector_all(
        ".psc-fold", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
    check("every tile in a row is the same height", len(set(heights)) == 1, str(heights))
    cells = P.eval_on_selector_all(
        ".psc-mosaic img", """els => els.filter(i => i.style.display !== 'none')
            .map(i => { const r = i.getBoundingClientRect();
                        return [Math.round(r.width), Math.round(r.height)]; })""")
    check("no cell is taller than it is wide",
          all(w >= h for w, h in cells),
          str([c for c in cells if c[0] < c[1]][:4]))
    check("and none of them collapsed to nothing",
          all(w > 8 and h > 8 for w, h in cells), str(cells[:3]))
    ratios = P.eval_on_selector_all(
        ".psc-mosaic", """els => els.map(m => {
            const r = m.getBoundingClientRect();
            return Math.round(r.width / r.height * 100) / 100; })""")
    check("every mosaic keeps the 3:2 footprint of a card image",
          all(abs(r - 1.5) < 0.06 for r in ratios), str(ratios))
    check("a portrait photograph is cropped, not letterboxed",
          P.eval_on_selector(".psc-mosaic img",
                             "e => getComputedStyle(e).objectFit") == "cover")

    print("\n--- the sort box orders the tiles on the index ---")
    def tilenames():
        return P.eval_on_selector_all(
            ".psc-fold", """els => els.filter(e => e.style.display !== 'none')
                               .map(e => e.querySelector('.psc-foldname').textContent)""")

    P.select_option(".psc-sort", "folder-desc"); P.wait_for_timeout(250)
    check("Folder Z-A reverses the named folders",
          tilenames()[:4] == ["Wyoming and Tetons", "Spiral Jetty Sunset",
                              "Bonneville", "Arches National Park"], str(tilenames()))
    check("and Unfiled stays last rather than leading the reverse",
          tilenames()[-1] == "Unfiled", str(tilenames()))

    P.select_option(".psc-sort", "date-desc"); P.wait_for_timeout(250)
    check("Date puts the folder with the newest photograph first",
          P.evaluate("""() => {
            const newest = g => [...document.querySelectorAll('.psc-card')]
              .filter(c => c.dataset.group === g)
              .map(c => c.dataset.date).sort().pop() || '';
            const ds = [...document.querySelectorAll('.psc-fold')]
              .filter(t => t.style.display !== 'none')
              .filter(t => t.querySelector('.psc-foldname').textContent !== 'Unfiled')
              .map(t => newest(t.dataset.group));
            return ds.every((v, i) => i === 0 || ds[i-1] >= v);
          }"""), str(tilenames()))

    # Each view keeps its own choice, so one does not disturb the other.
    P.click(".psc-fold"); P.wait_for_timeout(300)
    check("entering a folder switches to that view's own choice",
          P.eval_on_selector(".psc-sort", "e => e.value") == "score-desc")
    P.click(".psc-back"); P.wait_for_timeout(300)
    check("and coming back restores the index's",
          P.eval_on_selector(".psc-sort", "e => e.value") == "date-desc",
          P.eval_on_selector(".psc-sort", "e => e.value"))
    P.select_option(".psc-sort", "folder-asc"); P.wait_for_timeout(250)

    print("\n--- with no heart service, no likes anywhere ---")
    check("the tiles carry no like count",
          P.eval_on_selector_all(".psc-fhearts",
                                 "els => els.every(e => e.textContent === '')"))
    check("and the element takes up no room",
          vis(".psc-fhearts") == 0)
    check("the cover uses the folder's highest scoring photographs",
          P.evaluate("""() => {
            const t = [...document.querySelectorAll('.psc-fold')]
              .find(e => e.querySelector('.psc-foldname').textContent === 'Arches National Park');
            const srcs = [...t.querySelectorAll('.psc-mosaic img')].map(i => i.getAttribute('src'));
            const cards = [...document.querySelectorAll('.psc-card')]
              .filter(c => c.dataset.group === t.dataset.group)
              .sort((a,b) => b.dataset.score - a.dataset.score).slice(0,4);
            return cards.every(c => srcs.includes(c.querySelector('img').getAttribute('src')));
          }"""))

    print("\n--- entering a folder ---")
    P.click(".psc-fold:nth-child(4)")          # Wyoming and Tetons
    P.wait_for_timeout(250)
    check("the index gives way to that folder's photographs",
          vis(".psc-fold") == 0 and vis(".psc-card") == 6,
          f"{vis('.psc-card')} cards")
    check("the breadcrumb names it",
          P.inner_text(".psc-crumb h3") == "Wyoming and Tetons")
    check("and counts it", "6 photos" in P.inner_text(".psc-crumbn"),
          P.inner_text(".psc-crumbn"))
    check("the sort box is still there", vis(".psc-sort") == 1)
    check("and a folder opens on the best photographs first",
          P.eval_on_selector(".psc-sort", "e => e.value") == "score-desc",
          P.eval_on_selector(".psc-sort", "e => e.value"))
    # Every photograph in here shares the folder, so ordering by it sorts
    # nothing and the options are taken out of the box.
    opts = P.eval_on_selector_all(".psc-sort option", "e=>e.map(x=>x.value)")
    check("folder ordering is not offered inside a folder",
          not any(o.startswith("folder-") for o in opts), str(opts))
    check("but the rest of the sort is intact",
          {"score-desc", "score-asc", "date-desc", "name-asc"} <= set(opts), str(opts))
    check("only that folder's cards are visible",
          P.evaluate("""() => [...document.querySelectorAll('.psc-card')]
             .filter(c => !c.classList.contains('psc-hidden'))
             .every(c => c.dataset.group ===
                    document.querySelector('.psc-card:not(.psc-hidden)').dataset.group)"""))

    print("\n--- the lightbox stays inside the folder ---")
    P.click(".psc-card:not(.psc-hidden) img")
    P.wait_for_timeout(250)
    check("it opens", P.eval_on_selector(".psc-lb", "e=>e.classList.contains('open')"))
    check("and knows it is walking six, not fourteen",
          P.inner_text(".psc-count-lb").endswith("/ 6"), P.inner_text(".psc-count-lb"))
    seen = set()
    for _ in range(9):                          # more steps than the folder holds
        seen.add(P.inner_text(".psc-cap"))
        P.click(".psc-next"); P.wait_for_timeout(90)
    check("stepping past the end wraps within the folder", len(seen) == 6,
          f"{len(seen)} distinct photographs")
    check("every one of them belongs to the folder",
          P.evaluate("""() => {
            const names = [...document.querySelectorAll('.psc-card:not(.psc-hidden)')]
              .map(c => c.dataset.name);
            return names.length === 6;
          }"""))
    P.keyboard.press("Escape"); P.wait_for_timeout(200)

    print("\n--- filtering ---")
    # DSC_0008 is in Wyoming, DSC_0002 is in Arches. Inside a folder the first
    # should find exactly one photograph and the second none at all: the open
    # folder outranks the search, rather than the search pulling a stranger in.
    P.fill(".psc-q", "DSC_0008")
    P.wait_for_timeout(300)
    check("a search inside a folder finds its own photograph",
          vis(".psc-card") == 1, f"{vis('.psc-card')} cards")
    P.fill(".psc-q", "DSC_0002")
    P.wait_for_timeout(300)
    check("but cannot reach one in another folder",
          vis(".psc-card") == 0, f"{vis('.psc-card')} cards")
    check("and the breadcrumb admits none are showing",
          P.inner_text(".psc-crumbn") == "0 of 6", P.inner_text(".psc-crumbn"))
    P.fill(".psc-q", "")
    P.wait_for_timeout(250)
    check("clearing the search restores the folder", vis(".psc-card") == 6)

    P.click(".psc-back"); P.wait_for_timeout(250)
    check("back returns to the index", vis(".psc-fold") == 5 and vis(".psc-card") == 0)

    P.fill(".psc-q", "Arches")
    P.wait_for_timeout(300)
    check("a search in the index leaves only folders that match",
          vis(".psc-fold") == 1, f"{vis('.psc-fold')} tiles")
    check("and the empty state stays away while something matches",
          vis(".psc-empty") == 0)
    P.fill(".psc-q", "nothingmatchesthis")
    P.wait_for_timeout(300)
    check("a search that matches nothing says so", vis(".psc-empty") == 1)
    check("and shows no tiles", vis(".psc-fold") == 0)
    P.fill(".psc-q", "")
    P.wait_for_timeout(250)
    check("clearing it brings every folder back", vis(".psc-fold") == 5)

    print("\n--- the All photos view ---")
    P.click(".psc-vall"); P.wait_for_timeout(300)
    check("every photograph is on one page",
          vis(".psc-card") == TOTAL and vis(".psc-fold") == 0,
          f"{vis('.psc-card')} of {TOTAL}")
    check("no breadcrumb in the flat view", vis(".psc-crumb") == 0)
    all_opts = P.eval_on_selector_all(".psc-sort option", "e=>e.map(x=>x.value)")
    check("every ordering is offered where photographs span folders",
          {"score-desc", "score-asc", "folder-asc", "folder-desc",
           "date-desc", "name-asc"} <= set(all_opts), str(all_opts))
    check("the counter is back to photographs only",
          P.inner_text(".psc-count") == f"{TOTAL} of {TOTAL}",
          P.inner_text(".psc-count"))
    check("the lightbox now walks everything", P.evaluate("""() => {
            document.querySelector('.psc-card:not(.psc-hidden) img').click();
            return document.querySelector('.psc-count-lb').textContent; }""")
          .endswith("/ " + str(TOTAL)))
    P.keyboard.press("Escape"); P.wait_for_timeout(200)

    # Carrying a folder sort into a folder would leave the box displaying a
    # choice it no longer offers, so it falls back to the opening order.
    P.select_option(".psc-sort", "folder-asc"); P.wait_for_timeout(250)
    P.click(".psc-vfolders"); P.wait_for_timeout(250)
    P.click(".psc-fold"); P.wait_for_timeout(300)
    check("a folder sort carried into a folder falls back cleanly",
          P.eval_on_selector(".psc-sort", "e => e.value") == "score-desc",
          P.eval_on_selector(".psc-sort", "e => e.value"))
    check("and the cards really were reordered to match",
          P.evaluate("""() => {
            const s = [...document.querySelectorAll('.psc-card:not(.psc-hidden)')]
              .map(c => parseFloat(c.dataset.score));
            return s.every((v, i) => i === 0 || s[i-1] >= v);
          }"""))
    P.click(".psc-back"); P.wait_for_timeout(250)
    P.click(".psc-vall"); P.wait_for_timeout(250)

    print("\n--- the choice is remembered ---")
    P2 = ctx.new_page()
    P2.on("pageerror", lambda e: errors.append(str(e)))
    P2.set_viewport_size({"width": 1400, "height": 950})
    P2.goto((OUT / "live.html").as_uri()); P2.wait_for_timeout(500)
    check("a second visit opens on All photos",
          P2.eval_on_selector_all(".psc-card",
                                  "els => els.filter(e => e.offsetParent !== null).length")
          == TOTAL)
    P2.click(".psc-vfolders"); P2.wait_for_timeout(250)
    P3 = ctx.new_page()
    P3.set_viewport_size({"width": 1400, "height": 950})
    P3.goto((OUT / "live.html").as_uri()); P3.wait_for_timeout(500)
    check("switching back is remembered too",
          P3.eval_on_selector_all(".psc-fold",
                                  "els => els.filter(e => e.offsetParent !== null).length")
          == 5)

    print("\n--- a phone ---")
    P4 = ctx.new_page()
    P4.on("pageerror", lambda e: errors.append(str(e)))
    P4.set_viewport_size({"width": 390, "height": 844})     # iPhone 13 portrait
    P4.goto((OUT / "live.html").as_uri()); P4.wait_for_timeout(500)
    check("the bar does not push the page sideways",
          P4.evaluate("document.documentElement.scrollWidth <= "
                      "document.documentElement.clientWidth + 1"),
          str(P4.evaluate("[document.documentElement.scrollWidth, "
                          "document.documentElement.clientWidth]")))
    check("both view buttons are reachable",
          P4.eval_on_selector_all(".psc-views button",
                                  "els => els.filter(e => e.offsetParent !== null).length") == 2)

    print("\n--- the lightbox under a serif theme ---")
    # The overlay and the toast are moved out to <body>, beyond the gallery, so
    # a theme's body font would reach them. Many Ghost themes set a serif.
    themed = (OUT / "live.html").read_text(encoding="utf-8").replace(
        "<body", "<style>body,button{font-family:Georgia,serif;font-size:19px}"
                 "</style><body", 1)
    (OUT / "themed.html").write_text(themed, encoding="utf-8")
    P5 = ctx.new_page()
    P5.on("pageerror", lambda e: errors.append(str(e)))
    P5.goto((OUT / "themed.html").as_uri()); P5.wait_for_timeout(500)
    P5.click(".psc-vall"); P5.wait_for_timeout(300)
    P5.click(".psc-card img"); P5.wait_for_timeout(300)
    def fam(sel):
        return P5.eval_on_selector(sel, "e => getComputedStyle(e).fontFamily")
    gallery = fam(".psc-card .psc-name")
    for sel in (".psc-cap", ".psc-count-lb", ".psc-lb .x", ".psc-prev", ".psc-toast"):
        check(f"{sel} uses the gallery's font, not the theme's",
              fam(sel) == gallery and "Georgia" not in fam(sel), fam(sel))
    check("and the toast keeps its own size",
          P5.eval_on_selector(".psc-toast", "e => getComputedStyle(e).fontSize") == "13px")

    check("no page errors anywhere", not errors, "; ".join(errors[:3]))
    br.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
