"""
The link-preview Worker, run against a real published page.

Paste a gallery link into Messages or Slack and the app fetches it with a robot
of its own, reads a few tags, and draws a card. The robot runs no JavaScript,
so the gallery cannot do this for itself - worker/preview.js does it on the way
out, by reading the very page being served.

The decision logic is plain JavaScript with no Cloudflare in it, so it runs
here under Node against pages this project really produces. What the Worker
itself adds is a dozen lines of plumbing: fetch, check, substitute, respond.

Checked here: the right photograph for a photo link; a folder's best
photograph for a folder link; that a name carrying quotes or markup cannot
break out of a tag; that an unknown or hostile link leaves the page alone; and
that the Worker's idea of a folder's link matches the gallery's, since the two
spell slugs separately.
"""
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_scout_ghost as pg                     # noqa: E402

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


if not shutil.which("node"):
    print("SKIP  node is not installed, so the Worker's logic cannot be run here")
    sys.exit(0)

# ---------------------------------------------------------------------------
# A published page, with the awkward cases in it
# ---------------------------------------------------------------------------
SPEC = [
    ("2011-06-28 - Wyoming and Tetons", 4),
    ("2010-03-12 - Arches National Park", 3),
    # A name that would break out of a tag if it were not escaped, and one
    # with an accent, which has to survive being spelled into a link.
    ('2012-01-01 - "Quote" <b>Bold</b>', 2),
    ("2013-02-02 - Café Cortádo", 1),
    ("", 1),
]
items, n = [], 0
for folder, count in SPEC:
    for _ in range(count):
        n += 1
        items.append({
            "photo_id": f"{n:016x}",
            "verdict": "TOP PICK" if n % 3 == 0 else "STRONG",
            "score": 50 + n,                       # ascending, so the last wins
            "filename": f'DSC_{n:04d}"<b>.NEF' if n == 2 else f"DSC_{n:04d}.NEF",
            "folder": folder,
            "taken_at": f"2011-06-2{n % 9} 1{n % 6}:00:00",
            "resolution": "6000 x 4000 - 24.0 MP",
            "note": "Aesthetic 70 · Technical 60",
            "thumb_url": f"/content/images/t{n}.jpg",
            "preview_url": f"/content/images/p{n}.jpg",
        })

PAGE = Path("/tmp/psw_page.html")
# Wrapped in a whole document, as Ghost serves it: the tags go in the head, and
# a bare gallery fragment has none.
PAGE.write_text(
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Photographs</title><meta property='og:site_name' content='Example'>"
    "<meta property='og:title' content='The whole gallery'>"
    "<meta property='og:description' content='Every photograph &gt; kept'>"
    "<meta property='og:image' content='/content/images/theme-cover.jpg'>"
    "<meta property='og:image:width' content='1200'>"
    "<meta name='twitter:card' content='summary'>"
    "<meta name='twitter:image' content='/content/images/theme-cover.jpg'>"
    "</head><body style='margin:0;background:#111'>"
    + pg.build_gallery_html(items, {}, hearts_url="")
    + "</body></html>", encoding="utf-8")
payload = json.loads(re.search(r'class="psc-data">(.*?)</script>',
                               PAGE.read_text(encoding="utf-8"), re.S).group(1))
GROUPS, ENTRIES = payload["g"], payload["p"]

RUNNER = Path("/tmp/psw_run.mjs")
RUNNER.write_text(f"""
import {{ readFileSync }} from 'node:fs';
import {{ previewTags, withPreview, folderSlugs }}
  from '{(ROOT / "worker" / "preview.js").as_posix()}';
const html = readFileSync('{PAGE.as_posix()}', 'utf8');
const [query, base] = process.argv.slice(2);
if (query === '--slugs') {{
  console.log(JSON.stringify(folderSlugs(JSON.parse(base))));
}} else {{
  const url = new URL(base + query);
  const tags = previewTags(html, url.searchParams, url.toString());
  const out = withPreview(html, tags);
  console.log(JSON.stringify({{tags, changed: out !== html,
    head: out.slice(0, out.toLowerCase().indexOf('</head>'))}}));
}}
""", encoding="utf-8")

SITE = "https://example.com/photographs/"


def run(query):
    out = subprocess.run([  # nosec B603 - fixed argv, no shell
        "node", str(RUNNER), query, SITE],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        print(out.stderr[-600:])
        return {"tags": "", "changed": False, "head": ""}
    return json.loads(out.stdout)


def tag(tags, key):
    m = re.search(r'<meta (?:property|name)="' + re.escape(key) + r'" content="([^"]*)"', tags)
    return m.group(1) if m else None


print("=== a link to one photograph ===")
target = ENTRIES[5]
r = run("?photo=" + target["id"])
check("the page is changed", r["changed"])
check("the preview image is that photograph's own",
      tag(r["tags"], "og:image") == SITE.rsplit("/", 1)[0].rsplit("/", 1)[0]
      + target["pv"] if target["pv"].startswith("/") else True,
      tag(r["tags"], "og:image"))
check("as an absolute address, which a robot needs",
      (tag(r["tags"], "og:image") or "").startswith("https://example.com/"),
      tag(r["tags"], "og:image"))
check("titled with the file name", tag(r["tags"], "og:title") == target["n"],
      tag(r["tags"], "og:title"))
check("described by folder, date and size",
      target["f"] in (tag(r["tags"], "og:description") or ""),
      tag(r["tags"], "og:description"))
check("and carries the tags X reads too",
      tag(r["tags"], "twitter:card") == "summary_large_image"
      and tag(r["tags"], "twitter:image") == tag(r["tags"], "og:image"))
check("the link itself is the address shared",
      tag(r["tags"], "og:url") == SITE + "?photo=" + target["id"],
      tag(r["tags"], "og:url"))

print("\n=== the page's own tags step aside ===")
# Two og:image tags in one head is not resolved the same way by every robot:
# some take the first, some the last. The page's own are removed, not outvoted.
head = r["head"]
def metas(key):
    return re.findall(r'<meta (?:property|name)="' + re.escape(key) + r'" content="([^"]*)"',
                      head)
for key in ("og:image", "og:title", "og:description", "og:url", "twitter:card",
            "twitter:image"):
    check(f"exactly one {key}", len(metas(key)) == 1, str(metas(key)))
check("and it is ours, not the theme's",
      "theme-cover.jpg" not in head and "The whole gallery" not in head)
check("a tag we do not set is left alone",
      "og:site_name" in head and "<title>Photographs</title>" in head)
# A tag removed by a careless pattern leaves a fragment like '">' behind, which
# a browser reads as text and which ends the head early.
check("nothing is left half-removed",
      re.sub(r"<[^>]*>", "", head).strip() == "Photographs",
      repr(re.sub(r"<[^>]*>", "", head).strip()[:120]))

print("\n=== a link to a folder ===")
# The gallery shows a folder's best photograph first; the preview uses the same
# one, so the card matches what opens.
wy = GROUPS.index("Wyoming and Tetons")
best = max((p for p in ENTRIES if p["g"] == wy), key=lambda p: float(p["s"]))
slugs = json.loads(subprocess.run(  # nosec B603 - fixed argv, no shell
    ["node", str(RUNNER), "--slugs", json.dumps(GROUPS)],
    capture_output=True, text=True, timeout=60).stdout)
r = run("?folder=" + slugs[wy])
check("the folder's highest-scoring photograph stands for it",
      (tag(r["tags"], "og:image") or "").endswith(best["pv"]),
      tag(r["tags"], "og:image"))
check("titled with the folder's name", tag(r["tags"], "og:title") == "Wyoming and Tetons")
check("and says how many are inside",
      tag(r["tags"], "og:description") == "4 photographs",
      tag(r["tags"], "og:description"))

print("\n=== the Worker and the gallery agree on a folder's link ===")
# Two separate implementations of the same spelling; a link the page hands out
# has to resolve here, including for a name that is all punctuation or accents.
for gi, name in enumerate(GROUPS):
    r = run("?folder=" + slugs[gi])
    check(f"{name!r} -> {slugs[gi]!r}",
          tag(r["tags"], "og:title") == html_mod.escape(name, quote=True),
          tag(r["tags"], "og:title"))

# The real guarantee: open the gallery, click each folder, and take the link
# the page itself produces. If the two ever disagree, a link the gallery hands
# out would preview as nothing.
with sync_playwright() as pw:
    br = pw.chromium.launch()
    pgb = br.new_context(viewport={"width": 1400, "height": 950}).new_page()
    pgb.goto(PAGE.resolve().as_uri()); pgb.wait_for_timeout(400)
    tiles = pgb.eval_on_selector_all(
        ".psc-fold", "els => els.map(e => +e.dataset.group)")
    from_page = {}
    for gi in tiles:
        pgb.click(f'.psc-fold[data-group="{gi}"]'); pgb.wait_for_timeout(200)
        from_page[gi] = pgb.evaluate(
            "new URLSearchParams(location.search).get('folder')")
        pgb.click(".psc-back"); pgb.wait_for_timeout(150)
    br.close()
check("the gallery hands out the links the Worker resolves",
      all(from_page[gi] == slugs[gi] for gi in from_page),
      str({GROUPS[gi]: (from_page[gi], slugs[gi]) for gi in from_page
           if from_page[gi] != slugs[gi]}))

print("\n=== a name cannot break out of a tag ===")
hostile = next(p for p in ENTRIES if '"' in p["n"])
r = run("?photo=" + hostile["id"])
title = tag(r["tags"], "og:title")
check("a quote in a file name is escaped", title and "&quot;" in title, title)
check("and its markup is inert", "<b>" not in r["tags"], r["tags"][:120])
quoted = [g for g in GROUPS if '"' in g][0]
r = run("?folder=" + slugs[GROUPS.index(quoted)])
check("the same goes for a folder name",
      "&quot;" in (tag(r["tags"], "og:title") or "")
      and "&lt;b&gt;" in (tag(r["tags"], "og:title") or ""),
      tag(r["tags"], "og:title"))

print("\n=== links that lead nowhere leave the page alone ===")
for query, why in (("", "an ordinary visit"),
                   ("?ref=twitter", "someone else's query"),
                   ("?photo=deadbeefdeadbeef", "a photograph since hidden"),
                   ("?folder=gone-away", "a folder since renamed"),
                   ("?photo=__proto__", "a prototype name"),
                   ("?folder=constructor", "another one"),
                   ("?photo=%3Cimg%20src%3Dx%3E", "markup")):
    r = run(query)
    check(f"{why}: the page is served untouched",
          r["tags"] == "" and not r["changed"], r["tags"][:80])

print("\n=== nothing in the Worker names anybody's site ===")
for f in ("preview.js", "preview-worker.js"):
    src = (ROOT / "worker" / f).read_text(encoding="utf-8")
    check(f"{f} carries no domain but example.com",
          not re.search(r"https?://(?!example\.com)[a-z0-9.-]+\.[a-z]{2,}", src),
          str(re.findall(r"https?://[a-z0-9.-]+", src)))

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
