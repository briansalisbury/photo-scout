"""
Capture date, folder line, extended search and sorting.

Builds real JPEGs carrying real EXIF DateTimeOriginal tags, runs the pipeline,
then drives the report in a browser to check the sort actually reorders and the
search actually matches on dates and ratings.
"""
import contextlib, io, re, shutil, sqlite3, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


print("=== date formatting ===")
check("formats as requested", ps.pretty_date("2026-08-24") == "August 24, 2026",
      ps.pretty_date("2026-08-24"))
check("no zero padding on the day", ps.pretty_date("2011-06-05") == "June 5, 2011",
      ps.pretty_date("2011-06-05"))
check("December works", ps.pretty_date("1999-12-31") == "December 31, 1999")
check("a full timestamp still renders its date",
      ps.pretty_date("2026-08-24 14:03:22") == "August 24, 2026")
for bad in (None, "", "2026-13-01", "not a date", "2026-08", "0000-00-00"):
    check(f"rejects {bad!r}", ps.pretty_date(bad) == "")

print("\n=== time of day, 24 hour ===")
for stamp, want in (("2026-08-24 14:03:22", "14:03"),
                    ("2011-06-28 07:00:00", "07:00"),
                    ("2011-06-28 07:30:00", "07:30"),
                    ("2011-06-28 00:00:00", "00:00"),   # midnight is a real time
                    ("2011-06-28 23:59:59", "23:59"),
                    ("2011-06-28", ""),                 # date only
                    ("2011-06-28 24:00:00", ""),        # hour 24 does not exist
                    ("2011-06-28 12:60:00", ""),
                    ("2011-06-28 ab:cd:ef", ""),
                    (None, ""), ("", "")):
    got = ps.pretty_time(stamp)
    check(f"pretty_time({stamp!r}) -> {want!r}", got == want,
          "" if got == want else f"got {got!r}")
check("both halves join with a separator",
      ps.pretty_taken("2011-06-28 09:14:00") == "June 28, 2011 \u00b7 09:14",
      ps.pretty_taken("2011-06-28 09:14:00"))
check("a dateless photo shows nothing at all", ps.pretty_taken(None) == "")
check("a timeless photo shows just the date",
      ps.pretty_taken("2011-06-28") == "June 28, 2011")

print("\n=== dates stripped out of folder names ===")
for raw, want in (
        ("2011-07-05 - Wyoming", "Wyoming"),
        ("2011 6 28 Wyoming and Tetons", "Wyoming and Tetons"),
        ("2011.07.05_Arches", "Arches"),
        ("20110705 Zion", "Zion"),
        ("2011_07_05  --  Grand Tetons", "Grand Tetons"),
        ("Arches 2010.03.12", "Arches"),
        ("Arches - 2010-03-12", "Arches"),
        ("2016-03 Mustang", "Mustang"),
        # left alone on purpose
        ("2011 Wyoming", "2011 Wyoming"),      # a bare year is not a date
        ("2011", "2011"),                      # nothing would be left
        ("2011-07-05", "2011-07-05"),          # ditto
        ("1998 500 Photos", "1998 500 Photos"),  # 500 is not a month
        ("2013-13-45 Weird", "2013-13-45 Weird"),  # month 13 is not a month
        ("Best of 2011", "Best of 2011"),
        ("Route 66", "Route 66"),
        ("(root)", "(root)"),
        ("", ""),
        (None, ""),
        # nested folders are cleaned a component at a time
        ("2011-07-05 - Wyoming\\Publish", "Wyoming\\Publish"),
        ("A/2016-03-09 - Mustang", "A/Mustang"),
        ("2011-07-05 - Wyoming/2011-07-06 - Day Two", "Wyoming/Day Two")):
    got = ps.strip_folder_date(raw)
    check(f"{raw!r} -> {want!r}", got == want, "" if got == want else f"got {got!r}")

print("\n=== EXIF extraction ===")
def jpeg_with_date(path, iso_dt, size=(600, 400)):
    """Write a JPEG carrying a real EXIF DateTimeOriginal."""
    rng = np.random.default_rng(abs(hash(str(path))) % 9999)
    im = Image.fromarray(rng.integers(0, 255, (8, 12, 3), dtype=np.uint8)).resize(size)
    exif = im.getexif()
    sub = exif.get_ifd(0x8769)
    sub[36867] = iso_dt                    # DateTimeOriginal
    exif[306] = "2001:01:01 00:00:00"      # DateTime: must be IGNORED in favour of the above
    im.save(path, "JPEG", quality=88, exif=exif)

TMP = Path("/tmp/meta_probe"); shutil.rmtree(TMP, ignore_errors=True); TMP.mkdir()
p = TMP / "a.jpg"; jpeg_with_date(p, "2011:06:28 14:03:22")
check("reads the whole DateTimeOriginal, time included",
      ps._exif_taken(Image.open(p)) == "2011-06-28 14:03:22",
      str(ps._exif_taken(Image.open(p))))
check("prefers DateTimeOriginal over DateTime",
      not str(ps._exif_taken(Image.open(p))).startswith("2001-01-01"))
noclock = TMP / "noclock.jpg"; jpeg_with_date(noclock, "2011:06:28")
check("a date with no clock time yields the date alone",
      ps._exif_taken(Image.open(noclock)) == "2011-06-28",
      str(ps._exif_taken(Image.open(noclock))))
junk = TMP / "junk.jpg"; jpeg_with_date(junk, "2011:06:28 99:99:99")
check("an impossible clock time is dropped, the date is kept",
      ps._exif_taken(Image.open(junk)) == "2011-06-28",
      str(ps._exif_taken(Image.open(junk))))
plain = TMP / "plain.jpg"
Image.new("RGB", (60, 40)).save(plain, "JPEG")
check("no EXIF yields None", ps._exif_taken(Image.open(plain)) is None)
check("garbage input yields None rather than raising", ps._exif_taken(b"not an image") is None)

print("\n=== end to end ===")
LIB = Path("/tmp/meta_lib"); OUT = Path("/tmp/meta_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
# Every photograph in a folder shares a DATE but has a different TIME, and the
# times are written in the opposite order to the file names, so a sort that
# ignores the clock cannot accidentally look correct.
DATES = {
    "2011-06-28 - Wyoming": "2011:06:28",
    "2010-03-12 - Arches":  "2010:03:12",
    "2016-03-09 - Mustang": "2016:03:09",
    # A folder name far wider than any card. It used to share a line with the
    # date and resolution and push both out of sight behind the ellipsis.
    "2019-05-04 - Grand Staircase Escalante National Monument, Hole in the Rock Road":
        "2019:05:04",
}
LONG_FOLDER = "Grand Staircase Escalante National Monument, Hole in the Rock Road"
TIMES = [f"{6 + (11 - i) // 2:02d}:{((11 - i) % 2) * 30:02d}:00" for i in range(12)]
for folder, day in DATES.items():
    d = LIB / folder; d.mkdir(parents=True)
    for i in range(12):
        jpeg_with_date(d / f"DSC_{i:04d}.JPG", f"{day} {TIMES[i]}", (900, 600))
# one photo with no EXIF at all, to prove undated items are handled
Image.new("RGB", (900, 600), (90, 90, 90)).save(LIB / "2010-03-12 - Arches" / "NODATE.JPG", "JPEG")

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": float(np.clip(5.0 + np.sin(self.n * 1.7) * 0.6, 3.9, 6.1)),
                "nima_raw": float(np.clip(4.9 + np.cos(self.n * 1.1) * 0.5, 3.1, 6.2)),
                "subject_score": 95.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB)])

db = sqlite3.connect(OUT / "scores.sqlite3"); db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos WHERE error IS NULL").fetchall()
dated = [r for r in rows if r["taken_at"]]
check("taken_at column exists and is populated", len(dated) >= 30,
      f"{len(dated)} of {len(rows)}")
check("dates match the EXIF written",
      {r["taken_at"][:10] for r in dated} ==
      {d.replace(":", "-") for d in DATES.values()},
      str(sorted({r["taken_at"][:10] for r in dated})))
check("the clock time is stored too, not just the day",
      all(len(r["taken_at"]) == 19 for r in dated),
      str(sorted({r["taken_at"] for r in dated})[:2]))
check("times within a day actually differ",
      len({r["taken_at"] for r in dated if r["taken_at"].startswith("2011-06-28")}) == 12,
      str(len({r["taken_at"] for r in dated if r["taken_at"].startswith("2011-06-28")})))
check("the undated file is stored as NULL, not guessed",
      any(r["filename"] == "NODATE.JPG" and r["taken_at"] is None for r in rows))

h = (OUT / "report.html").read_text(encoding="utf-8")
print("\n=== the card ===")
check("folder appears on the card, with its date stripped",
      re.search(r'<div class="folder"[^>]*>Wyoming</div>', h) is not None,
      str(re.findall(r'<div class="folder".{0,60}', h)[:2]))
check("the date is on the line below the folder, not beside it",
      # A long folder name used to push the date off the end of a shared line.
      # Splitting them is what guarantees the date is always visible.
      re.search(r'<div class="specs"><span>June 28, 2011</span>', h) is not None,
      str(re.findall(r'<div class="specs">.{0,70}', h)[:2]))
check("the time of day is shown beside the date, 24 hour",
      re.search(r"<span>June 28, 2011</span> &middot; "
                r"<span>(0[6-9]|1[0-2]):[0-5]\d</span>", h) is not None,
      str(re.findall(r"June 28, 2011</span>.{0,40}", h)[:3]))
check("each fact is wrapped so a line break cannot fall inside one",
      "<span>June 28, 2011</span>" in h and
      re.search(r"<span>\d+ \u00d7 \d+</span>", h) is not None,
      str(re.findall(r"<span>[^<]*</span>", h)[:5]))
check("the full folder name is available on hover when it is elided",
      'class="folder" title="Wyoming"' in h)
check("search blob carries the time", re.search(r'data-search="[^"]*\d\d:\d\d', h) is not None)
check("the raw dated folder name is not displayed",
      ">2011-06-28 - Wyoming" not in h)
check("but it is still searchable", "2011-06-28 - wyoming" in h.lower())
check("date appears in long form", "June 28, 2011" in h and "March 12, 2010" in h)
check("sortable attributes emitted",
      all(a in h for a in ('data-date="', 'data-name="', 'data-folder="', 'data-score="')))
check("search blob carries the long date", "june 28, 2011" in h.lower())
check("search blob carries the verdict",
      any(v in h.lower() for v in ("top pick", "strong")))
check("sort control present", 'id="sort"' in h)
for opt in ("score-desc", "date-asc", "name-asc", "folder-asc"):
    check(f"sort option {opt}", f'value="{opt}"' in h)

print("\n=== in a browser ===")
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(); P = b.new_page()
    P.set_viewport_size({"width": 1500, "height": 950})
    P.goto((OUT / "report.html").resolve().as_uri()); P.wait_for_timeout(500)

    print("\n--- a very long folder name hides nothing else")
    # The regression this layout exists for. The folder is allowed to clip; the
    # date, time and resolution beneath it are not, at any window width.
    for width in (1500, 1100, 760):
        P.set_viewport_size({"width": width, "height": 950})
        P.wait_for_timeout(150)
        long_card = P.locator(f'.card[data-folder="{LONG_FOLDER}"]').first
        specs = long_card.locator(".specs")
        txt = specs.inner_text()
        check(f"at {width}px the date survives beside the long folder",
              "May 4, 2019" in txt, txt)
        check(f"at {width}px so does the resolution",
              re.search(r"\d+ × \d+ · \d+\.\d MP", txt) is not None, txt)
        clipped = specs.evaluate("e => e.scrollWidth > e.clientWidth + 1")
        check(f"at {width}px the specs line wraps instead of truncating", not clipped)
    check("the folder itself is the one thing allowed to clip, and says so on hover",
          P.locator(f'.card[data-folder="{LONG_FOLDER}"] .folder').first
           .get_attribute("title") == LONG_FOLDER)
    P.set_viewport_size({"width": 1500, "height": 950}); P.wait_for_timeout(150)

    def order(attr):
        return P.eval_on_selector_all(
            ".card:not(.hidden)", f"e => e.map(x => x.dataset.{attr})")
    def shown():
        return P.eval_on_selector_all(".card:not(.hidden)", "e => e.length")

    total = shown()
    check("cards rendered", total > 30, str(total))

    P.select_option("#sort", "name-asc"); P.wait_for_timeout(300)
    names = order("name")
    check("sorted by file name ascending", names == sorted(names), str(names[:3]))
    P.select_option("#sort", "name-desc"); P.wait_for_timeout(300)
    check("and descending", order("name") == sorted(names, reverse=True))

    P.select_option("#sort", "date-asc"); P.wait_for_timeout(300)
    dates = [d for d in order("date") if d]
    check("sorted by date oldest first", dates == sorted(dates), str(dates[:2] + dates[-2:]))
    # The point of the fixture: every photo of a given day has a different time,
    # written in the opposite order to the file names. A sort that stopped at the
    # day would leave these in file-name order and fail here.
    sameday = [d for d in dates if d.startswith("2011-06-28")]
    check("photographs taken on the same day order by the clock",
          len(sameday) > 1 and sameday == sorted(sameday),
          f"{[d[11:16] for d in sameday]}")
    check("07:00 comes before 07:30",
          sameday.index("2011-06-28 07:00:00") < sameday.index("2011-06-28 07:30:00"))
    check("undated photos sink to the bottom",
          order("date")[-1] == "", f"last is {order('date')[-1]!r}")
    P.select_option("#sort", "date-desc"); P.wait_for_timeout(300)
    dates = [d for d in order("date") if d]
    check("sorted by date newest first", dates == sorted(dates, reverse=True))
    sameday = [d for d in dates if d.startswith("2011-06-28")]
    check("and the clock reverses with it",
          sameday == sorted(sameday, reverse=True), f"{[d[11:16] for d in sameday]}")
    check("undated still sink to the bottom in reverse", order("date")[-1] == "")

    P.select_option("#sort", "folder-asc"); P.wait_for_timeout(300)
    folders = [f for f in order("folder") if f]
    check("sorted by folder", folders == sorted(folders), str(folders[:2]))
    check("folder sort uses the name as displayed, not the dated one",
          set(folders) == {"Arches", "Mustang", "Wyoming", LONG_FOLDER},
          str(sorted(set(folders))))

    P.select_option("#sort", "score-desc"); P.wait_for_timeout(300)
    scores = [float(x) for x in order("score")]
    check("sorted by rating, best first", scores == sorted(scores, reverse=True),
          str([round(x, 1) for x in scores[:3]]))
    P.select_option("#sort", "score-asc"); P.wait_for_timeout(300)
    scores = [float(x) for x in order("score")]
    check("and worst first", scores == sorted(scores))

    print("\n=== searching the new fields ===")
    P.select_option("#sort", "score-desc"); P.wait_for_timeout(200)
    for term, label in (("June 28, 2011", "long-form date"),
                        ("2011-06-28", "ISO date"),
                        ("Wyoming", "folder"),
                        ("2010-03-12 - Arches", "the folder's name on disk"),
                        ("DSC_0003", "file name")):
        P.fill("#q", term); P.wait_for_timeout(300)
        n = shown()
        check(f"search by {label} finds matches", 0 < n < total, f"{n} of {total}")
        P.fill("#q", ""); P.wait_for_timeout(200)

    P.fill("#q", "top pick"); P.wait_for_timeout(300)
    n = shown()
    verds = P.eval_on_selector_all(".card:not(.hidden)", "e => e.map(x => x.dataset.verdict)")
    check("search by rating returns only that band",
          n > 0 and set(verds) == {"TOP PICK"}, f"{n} results, {set(verds)}")
    P.fill("#q", ""); P.wait_for_timeout(200)

    print("\n=== the written feedback is searchable ===")
    # Pull a real phrase out of one card's note and look for it.
    note = P.eval_on_selector(".card .note", "e => e.textContent")
    phrase = " ".join(note.split()[:3]).strip(" ,;.")
    check("a card carries written feedback", len(phrase) > 4, repr(note[:60]))
    P.fill("#q", phrase); P.wait_for_timeout(300)
    n = shown()
    check("searching the feedback text finds photographs", n > 0, f"{n} for {phrase!r}")

    print("\n--- and every search is case-insensitive")
    for term in (phrase, "TOP PICK", "top pick", "ToP PiCk",
                 "wyoming", "WYOMING", "WyOmInG",
                 "june 28, 2011", "JUNE 28, 2011", "June 28, 2011",
                 "dsc_0003", "DSC_0003", "dSc_0003"):
        P.fill("#q", term); P.wait_for_timeout(200)
        counts = shown()
        P.fill("#q", term.lower()); P.wait_for_timeout(200)
        lower = shown()
        P.fill("#q", term.upper()); P.wait_for_timeout(200)
        upper = shown()
        check(f"{term!r} matches regardless of case",
              counts == lower == upper and counts > 0,
              f"as typed {counts}, lower {lower}, upper {upper}")
    P.fill("#q", ""); P.wait_for_timeout(200)

    print("\n=== the folder picker ===")
    labels = P.eval_on_selector_all("#folder option", "e => e.map(x => x.textContent)")
    check("picker labels lose the date too",
          any(l.startswith("Wyoming (") for l in labels), str(labels))
    check("no dated label survives",
          not any(l.startswith("2011-06-28") for l in labels), str(labels))
    P.select_option("#folder", "2011-06-28 - Wyoming"); P.wait_for_timeout(300)
    picked = set(order("folder"))
    check("but it still filters on the real directory name",
          picked == {"Wyoming"} and 0 < shown() < total, f"{shown()} of {total}, {picked}")
    P.select_option("#folder", "all"); P.wait_for_timeout(250)

    print("\n=== sort and filter compose ===")
    P.click('button[data-f="STRONG"]'); P.wait_for_timeout(250)
    P.select_option("#sort", "date-asc"); P.wait_for_timeout(300)
    verds = P.eval_on_selector_all(".card:not(.hidden)", "e => e.map(x => x.dataset.verdict)")
    dts = [d for d in order("date") if d]
    check("filter still applies after sorting", set(verds) == {"STRONG"}, str(set(verds)))
    check("sort still applies after filtering", dts == sorted(dts))

    print("\n=== the lightbox follows the sort ===")
    # Regression: the walk list used to be filtered out of a snapshot array taken
    # at page load, so after a sort it still held the ORIGINAL order and the
    # lightbox opened a different photograph from the one that was clicked.
    P.click('button[data-f="all"]'); P.wait_for_timeout(200)
    for mode in ("name-desc", "date-asc", "folder-desc", "score-asc"):
        P.select_option("#sort", mode); P.wait_for_timeout(300)
        names = order("name")
        P.locator(".card:not(.hidden) img").first.click(); P.wait_for_timeout(250)
        got = P.eval_on_selector("#lb-name", "e => e.textContent")
        check(f"{mode}: opens the photograph that was clicked", got == names[0],
              f"clicked {names[0]}, opened {got}")
        check(f"{mode}: counter starts at one",
              P.eval_on_selector("#lb-count", "e => e.textContent")
              == f"1/{len(names)}")
        P.click("#lb-next"); P.wait_for_timeout(200)
        got = P.eval_on_selector("#lb-name", "e => e.textContent")
        check(f"{mode}: next moves to the next photograph on screen",
              got == names[1], f"expected {names[1]}, got {got}")
        # and the subtitle must describe the same card
        sub = P.eval_on_selector("#lb-sub", "e => e.textContent")
        cards2 = P.eval_on_selector_all(
            ".card:not(.hidden)", "e => e.map(x => x.dataset.folder)")
        check(f"{mode}: the caption matches that card", cards2[1] in sub,
              "" if cards2[1] in sub else f"{cards2[1]!r} not in {sub!r}")
        P.click("#lb-close"); P.wait_for_timeout(150)

    # and it must still track a filter applied while open
    P.select_option("#sort", "name-asc"); P.wait_for_timeout(250)
    P.locator(".card:not(.hidden) img").first.click(); P.wait_for_timeout(250)
    P.fill("#q", "Wyoming"); P.wait_for_timeout(350)
    inlb = P.eval_on_selector("#lb-name", "e => e.textContent")
    check("filtering while open keeps the lightbox on a visible card",
          inlb in order("name"), inlb)
    P.click("#lb-close"); P.fill("#q", ""); P.wait_for_timeout(200)

    print("\n=== sorting preserves per-card state ===")
    P.click('button[data-f="all"]'); P.wait_for_timeout(250)
    card = P.locator(".card").first
    card.locator(".taginput").click(); card.locator(".taginput").type("Keepme,")
    P.wait_for_timeout(300)
    before = P.evaluate("() => Object.keys(TAGS).length")
    P.select_option("#sort", "name-desc"); P.wait_for_timeout(350)
    check("tags survive a reorder",
          P.evaluate("() => Object.keys(TAGS).length") == before and
          P.evaluate("() => document.querySelectorAll('.tag').length") > 0)
    b.close()

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
