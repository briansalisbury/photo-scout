"""
Drives the tagging UI in a real Chromium browser.

Static string checks can't tell you whether typing a comma actually creates a
tag, whether the dropdown filters, or whether selecting a chip hides the right
cards. This loads the generated report and uses it the way Brian would.
"""
import contextlib, io, json, shutil, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps
from playwright.sync_api import sync_playwright

LIB = Path("/tmp/tagb_lib"); OUT = Path("/tmp/tagb_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
LIB.mkdir(parents=True)
rng = np.random.default_rng(4242)
for folder, n in {"2011 Wyoming": 4, "2010 Arches": 3}.items():
    d = LIB / folder; d.mkdir(parents=True)
    for i in range(n):
        Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
             .resize((700, 500), Image.BICUBIC).save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90)

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": 5.0 + self.n * 0.12, "nima_raw": 5.0,
                "subject_score": 90.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT

# seed one tag from the file so the load path is exercised too
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "tags.json").write_text(json.dumps(
    {str(LIB / "2010 Arches" / "DSC_0000.JPG"): ["Desert"]}), encoding="utf-8")
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB)])

REPORT = OUT / "report.html"

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(REPORT.as_uri())
    page.wait_for_selector(".card")

    def visible():
        return page.eval_on_selector_all(
            ".card", "els => els.filter(e => !e.classList.contains('hidden')).length")
    def chips_of(card_idx):
        return page.eval_on_selector_all(
            f".card:nth-of-type({card_idx}) .tag",
            "els => els.map(e => e.firstChild.textContent)")

    total = page.eval_on_selector_all(".card", "e => e.length")
    print(f"       {total} cards loaded")

    print("=== seeded tag renders ===")
    seeded = page.eval_on_selector_all(
        ".card .tag", "els => els.map(e => e.firstChild.textContent)")
    check("tag from tags.json is on a card", "Desert" in seeded, str(seeded))

    print("\n=== typing a tag: Enter commits ===")
    first = page.locator(".card").first
    first.locator(".taginput").click()
    first.locator(".taginput").type("Lake Photos")
    first.locator(".taginput").press("Enter")
    page.wait_for_timeout(120)
    check("multi-word tag created", chips_of(1) == ["Lake Photos"], str(chips_of(1)))
    check("input cleared after commit",
          first.locator(".taginput").input_value() == "")

    print("\n=== comma commits too ===")
    first.locator(".taginput").type("Sunset,")
    page.wait_for_timeout(120)
    check("comma created a second tag", chips_of(1) == ["Lake Photos", "Sunset"], str(chips_of(1)))

    print("\n=== dangerous characters are stripped in the browser ===")
    first.locator(".taginput").type('<img src=x onerror=alert(1)>')
    first.locator(".taginput").press("Enter")
    page.wait_for_timeout(120)
    now = chips_of(1)
    check("no tag contains a bracket or quote",
          all(not any(ch in t for ch in '<>"\'&') for t in now), str(now))
    check("sanitised remnant kept as plain text",
          any("img" in t for t in now), str(now))
    check("no script executed", not errors, str(errors[:2]))
    check("page has no injected img element",
          page.eval_on_selector_all(".tag img", "e => e.length") == 0)

    print("\n=== punctuation-only input is rejected ===")
    before = len(chips_of(1))
    first.locator(".taginput").type("!!!@@@")
    first.locator(".taginput").press("Enter")
    page.wait_for_timeout(120)
    check("no empty tag created", len(chips_of(1)) == before, str(chips_of(1)))

    print("\n=== duplicates are ignored ===")
    first.locator(".taginput").type("lake photos")
    first.locator(".taginput").press("Enter")
    page.wait_for_timeout(120)
    check("case-insensitive duplicate rejected",
          sum(1 for t in chips_of(1) if t.lower() == "lake photos") == 1, str(chips_of(1)))

    print("\n=== colours are unique per tag and stable ===")
    colours = page.eval_on_selector_all(".card .tag", """els => els.map(e => ({
        name: e.firstChild.textContent,
        bg: getComputedStyle(e).backgroundColor}))""")
    by_name = {}
    for c in colours:
        by_name.setdefault(c["name"], set()).add(c["bg"])
    check("each tag has exactly one colour",
          all(len(v) == 1 for v in by_name.values()),
          str({k: v for k, v in by_name.items() if len(v) > 1}))
    distinct = {list(v)[0] for v in by_name.values()}
    check("different tags have different colours",
          len(distinct) == len(by_name), f"{len(distinct)} colours for {len(by_name)} tags")
    check("colours are actually applied (not the default)",
          all("rgb" in list(v)[0] for v in by_name.values()))

    print("\n=== tag on a second card, then search ===")
    second = page.locator(".card").nth(1)
    second.locator(".taginput").click()
    second.locator(".taginput").type("Lake Photos,")
    page.wait_for_timeout(120)
    check("same tag on two cards",
          page.eval_on_selector_all(".card .tag",
              "els => els.filter(e => e.firstChild.textContent === 'Lake Photos').length") == 2)

    print("\n=== live typeahead dropdown ===")
    page.click("#q")
    page.wait_for_timeout(100)
    check("menu opens on focus", page.is_visible("#tagmenu"))
    page.fill("#q", "lak")
    page.wait_for_timeout(150)
    opts = page.eval_on_selector_all("#tagmenu div .tag", "e => e.map(x => x.firstChild.textContent)")
    check("typing filters the dropdown live", opts == ["Lake Photos"], str(opts))
    counts = page.eval_on_selector_all("#tagmenu .count", "e => e.map(x => x.textContent)")
    check("dropdown shows how many photos carry the tag", counts == ["2 photos"], str(counts))
    page.fill("#q", "zzzz")
    page.wait_for_timeout(150)
    check("no-match message shown",
          "No tag matches" in (page.text_content("#tagmenu") or ""))

    print("\n=== selecting a tag filters the grid ===")
    page.fill("#q", "lake")
    page.wait_for_timeout(150)
    page.click("#tagmenu div")
    page.wait_for_timeout(200)
    chip_names = page.eval_on_selector_all("#chips .tag", "e => e.map(x => x.firstChild.textContent)")
    check("chip appears in the search box", chip_names == ["Lake Photos"], str(chip_names))
    check("search text cleared after selection", page.input_value("#q") == "")
    check("grid filtered to the tagged cards", visible() == 2, f"{visible()} visible of {total}")
    # compare the SAME tag in both places - a card carries several chips
    chip_bg = page.eval_on_selector("#chips .tag", "e => getComputedStyle(e).backgroundColor")
    card_bg = page.evaluate("""() => {
        const el = [...document.querySelectorAll('.card .tag')]
            .find(e => e.firstChild.textContent === 'Lake Photos');
        return el ? getComputedStyle(el).backgroundColor : null; }""")
    check("chip colour matches the same tag on a card", chip_bg == card_bg,
          f"{chip_bg} vs {card_bg}")

    print("\n=== a second chip WIDENS the results (OR, not AND) ===")
    one = visible()
    page.click("#q")
    page.fill("#q", "sunset")
    page.wait_for_timeout(150)
    page.click("#tagmenu div")
    page.wait_for_timeout(200)
    names = page.eval_on_selector_all("#chips .tag", "e => e.map(x => x.firstChild.textContent)")
    check("chips sorted alphabetically", names == sorted(names), str(names))
    check("adding a chip never shrinks the result set", visible() >= one,
          f"{one} -> {visible()} visible")
    check("union of both tags shown", visible() == 2, f"{visible()} visible of {total}")

    print("\n=== two tags with NO overlap still both show ===")
    # Brian's case: "Lake" on one photo, "Desert" on another, no photo has both.
    page.click("#chips .tag button"); page.wait_for_timeout(120)
    page.click("#chips .tag button"); page.wait_for_timeout(120)
    third = page.locator(".card").nth(3)
    third.locator(".taginput").click()
    third.locator(".taginput").type("Canyon,")
    page.wait_for_timeout(150)
    overlap = page.evaluate("""() => {
        let both = 0;
        for (const k in TAGS) {
          const t = TAGS[k].map(x => x.toLowerCase());
          if (t.includes('canyon') && t.includes('sunset')) both++;
        }
        return both; }""")
    check("no photo carries both tags", overlap == 0, f"{overlap} carry both")
    for term in ("canyon", "sunset"):
        page.click("#q"); page.fill("#q", term); page.wait_for_timeout(150)
        page.click("#tagmenu div"); page.wait_for_timeout(200)
    picked = page.eval_on_selector_all("#chips .tag", "e => e.map(x => x.firstChild.textContent)")
    check("both chips present", len(picked) == 2, str(picked))
    check("non-overlapping tags return BOTH photos, not zero",
          visible() == 2, f"{visible()} visible (AND would have given 0)")

    print("\n=== removing chips narrows back down ===")
    page.click("#chips .tag button")          # alphabetically first = Canyon
    page.wait_for_timeout(200)
    left = page.eval_on_selector_all("#chips .tag", "e => e.map(x => x.firstChild.textContent)")
    check("chip removed", left == ["Sunset"], str(left))
    check("back to the one photo for the remaining tag", visible() == 1, f"{visible()} visible")
    page.click("#chips .tag button")
    page.wait_for_timeout(200)
    check("all chips gone", page.eval_on_selector_all("#chips .tag", "e => e.length") == 0)
    check("everything visible again", visible() == total, f"{visible()} of {total}")

    print("\n=== keyboard: arrows and Enter ===")
    page.click("#q")
    page.fill("#q", "a")
    page.wait_for_timeout(150)
    n_opts = page.eval_on_selector_all("#tagmenu div", "e => e.length")
    if n_opts > 1:
        page.press("#q", "ArrowDown")
        page.wait_for_timeout(80)
        sel_idx = page.eval_on_selector_all(
            "#tagmenu div", "e => e.findIndex(x => x.classList.contains('sel'))")
        check("ArrowDown moves the highlight", sel_idx == 1, f"index {sel_idx}")
    page.press("#q", "Enter")
    page.wait_for_timeout(200)
    check("Enter selects the highlighted tag",
          page.eval_on_selector_all("#chips .tag", "e => e.length") == 1)
    page.click("#chips .tag button")
    page.wait_for_timeout(150)

    print("\n=== removing a tag from a card ===")
    n_before = len(chips_of(1))
    page.click(".card:nth-of-type(1) .tag button")
    page.wait_for_timeout(200)
    check("tag removed from the card", len(chips_of(1)) == n_before - 1, str(chips_of(1)))

    print("\n=== retiring a tag that survives on a hidden card ===")
    # The reported bug: a tag stays in the search box after you have deleted
    # every chip you can SEE, because one photograph still carrying it is hidden
    # by a band button, the folder picker or the near-duplicate toggle.
    pair = page.evaluate("""() => {
      const cs = [...document.querySelectorAll('.card')];
      for (let i = 0; i < cs.length; i++)
        for (let j = i + 1; j < cs.length; j++)
          if (cs[i].dataset.foldertop !== cs[j].dataset.foldertop)
            return {ids: [i, j], folder: cs[i].dataset.foldertop};
      return null;}""")
    check("the fixture has cards in two different folders", pair is not None, str(pair))
    ids, folder = pair["ids"], pair["folder"]
    for i in ids:
        card = page.locator(".card").nth(i)
        card.locator(".taginput").click()
        card.locator(".taginput").type("Retireme,")
        page.wait_for_timeout(200)
    check("tag applied to both", page.evaluate("() => tagCount('Retireme')") == 2,
          str(page.evaluate("() => tagCount('Retireme')")))

    page.select_option("#folder", folder); page.wait_for_timeout(250)
    stillvis = page.eval_on_selector_all(
        ".card:not(.hidden)",
        "e => e.filter(c => (c.dataset.tags||'').includes('|retireme|')).length")
    check("filtering to one folder hides the other tagged photograph",
          stillvis == 1, f"{stillvis} of the 2 tagged cards still visible")
    while page.eval_on_selector_all(".card:not(.hidden) .taglist .tag button",
                                    "e => e.length"):
        page.locator(".card:not(.hidden) .taglist .tag button").first.click()
        page.wait_for_timeout(180)
    check("deleting every VISIBLE chip leaves the tag alive, as it should",
          "Retireme" in page.evaluate("() => allTags()"),
          str(page.evaluate("() => allTags()")))

    page.click("#q"); page.fill("#q", "Retire"); page.wait_for_timeout(300)
    check("the dropdown offers a way to retire it",
          page.eval_on_selector_all("#tagmenu .delall", "e => e.length") == 1)
    check("and says how many photographs it is still on",
          page.eval_on_selector("#tagmenu .count", "e => e.textContent") == "1 photo",
          page.eval_on_selector("#tagmenu .count", "e => e.textContent"))
    # It must ASK first, and naming the tag and the count is the whole point.
    seen = {}
    def grab(d):
        seen["msg"] = d.message; d.dismiss()
    page.once("dialog", grab)
    page.locator("#tagmenu .delall").first.click(); page.wait_for_timeout(400)
    check("it asks before deleting", "msg" in seen, str(seen))
    check("the warning names the tag", "Retireme" in seen.get("msg", ""), seen.get("msg"))
    check("and says how far it reaches, grammatically",
          "the 1 photo that uses it" in seen.get("msg", ""), seen.get("msg"))
    check("and warns about the ones you cannot see",
          "hidden" in seen.get("msg", "").lower(), seen.get("msg"))
    check("cancelling changes nothing",
          page.evaluate("() => tagCount('Retireme')") == 1,
          str(page.evaluate("() => tagCount('Retireme')")))

    page.once("dialog", lambda d: d.accept())
    page.locator("#tagmenu .delall").first.click(); page.wait_for_timeout(400)
    check("the tag is gone from the library",
          "Retireme" not in page.evaluate("() => allTags()"),
          str(page.evaluate("() => allTags()")))
    check("gone from the hidden card too, not just the visible ones",
          page.evaluate("() => tagCount('Retireme')") == 0)
    check("no chip left behind in the search box",
          "Retireme" not in str(page.eval_on_selector_all(
              "#chips .tag", "e => e.map(x => x.firstChild.textContent)")))
    check("removing a tag is not silent",
          "Retireme" in page.eval_on_selector("#toast", "e => e.textContent"),
          page.eval_on_selector("#toast", "e => e.textContent"))

    print("\n--- and it is undoable")
    page.locator("#toast button").click(); page.wait_for_timeout(400)
    check("Undo puts it back on every photograph",
          page.evaluate("() => tagCount('Retireme')") == 1,
          str(page.evaluate("() => tagCount('Retireme')")))
    check("including the one that was hidden",
          "Retireme" in page.evaluate("() => allTags()"))
    page.click("#q"); page.fill("#q", "Retire"); page.wait_for_timeout(250)
    page.once("dialog", lambda d: d.accept())
    page.locator("#tagmenu .delall").first.click(); page.wait_for_timeout(300)
    page.fill("#q", ""); page.select_option("#folder", "all"); page.wait_for_timeout(250)

    print("\n=== stored tags stay tidy ===")
    check("no empty tag lists are persisted",
          page.evaluate("""() => {const raw = localStorage.getItem(
              Object.keys(localStorage).find(k => k.indexOf('photo') >= 0) ||
              Object.keys(localStorage)[0]);
            const o = JSON.parse(raw || '{}');
            return Object.values(o).every(v => v && v.length);}"""))

    print("\n=== persistence across a reload ===")
    state = page.evaluate("() => JSON.stringify(TAGS)")
    page.reload()
    page.wait_for_selector(".card")
    page.wait_for_timeout(200)
    after = page.evaluate("() => JSON.stringify(TAGS)")
    check("tags survive a reload via localStorage", after == state)
    check("chips re-render after reload",
          page.eval_on_selector_all(".card .tag", "e => e.length") > 0)

    print("\n=== free-text search still works ===")
    page.fill("#q", "DSC_0001")
    page.wait_for_timeout(200)
    check("filename search unaffected by tagging", 0 < visible() < total, f"{visible()} visible")
    page.fill("#q", "")
    page.wait_for_timeout(150)

    check("no JS errors during the whole session", not errors, str(errors[:3]))
    browser.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
