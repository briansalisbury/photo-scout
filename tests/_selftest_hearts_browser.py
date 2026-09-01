"""
The heart buttons in a real browser, against the real service.

A small WSGI dispatcher plays the part nginx plays in production: it serves the
gallery and the heart API from ONE origin, which is the whole reason the
production design uses a same-origin path. That makes this a faithful rehearsal
rather than a mock.

The most important test in here is the last one: with the service returning
errors, the gallery must be completely usable and the hearts simply absent.
"""
import contextlib, io, json, os, shutil, socket, sys, threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import make_server, WSGIServer

import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hearts"))

LIB = Path("/tmp/hb_lib"); OUT = Path("/tmp/hb_out")
DB = Path("/tmp/hb_hearts/hearts.sqlite3")
for d in (LIB, OUT, DB.parent):
    shutil.rmtree(d, ignore_errors=True)
os.environ["HEARTS_DB"] = str(DB)
os.environ["HEARTS_ADMIN_TOKEN"] = "browser-test-token"

import photo_scout as ps                     # noqa: E402
import photo_scout_ghost as pg               # noqa: E402
import app as hearts                         # noqa: E402

hearts.init_db()

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------------------
# One origin, two things behind it - exactly the shape nginx gives us
# ---------------------------------------------------------------------------
STATE = {"broken": False, "api_calls": 0}

CTYPE = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg",
         ".png": "image/png", ".json": "application/json"}


def dispatcher(environ, start_response):
    path = environ.get("PATH_INFO", "")
    if path.startswith("/api/hearts"):
        STATE["api_calls"] += 1
        if STATE["broken"]:
            body = b'{"error":"service is down"}'
            start_response("503 Service Unavailable",
                           [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body)))])
            return [body]
        return hearts.app(environ, start_response)

    target = (OUT / path.lstrip("/")).resolve()
    if not str(target).startswith(str(OUT.resolve())) or not target.is_file():
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"not found"]
    data = target.read_bytes()
    start_response("200 OK", [("Content-Type", CTYPE.get(target.suffix, "text/plain")),
                              ("Content-Length", str(len(data)))])
    return [data]


class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


PORT = free_port()
httpd = make_server("127.0.0.1", PORT, dispatcher, server_class=Threaded)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
ORIGIN = f"http://127.0.0.1:{PORT}"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
LIB.mkdir(parents=True)
rng = np.random.default_rng(11)
# Enough photographs that the calibration bands actually produce a shortlist:
# TOP PICK is the top 5 per cent, so a handful of images yields none at all.
for folder, day in (("2011-06-28 - Wyoming", "2011:06:28"),
                    ("2010-03-12 - Arches", "2010:03:12")):
    d = LIB / folder; d.mkdir(parents=True)
    for i in range(20):
        im = Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
                  .resize((900, 620), Image.BICUBIC)
        ex = im.getexif()
        ex.get_ifd(0x8769)[36867] = f"{day} {6 + i // 2:02d}:{(i % 2) * 30:02d}:00"
        im.save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90, exif=ex)


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
pg.ps.DEFAULT_OUT_DIR = OUT
with contextlib.redirect_stdout(io.StringIO()):
    ps.main(["--root", str(LIB)])

MANIFEST = Path("/tmp/hb_manifest.sqlite3"); MANIFEST.unlink(missing_ok=True)


def publish(*extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pg.main(["--site", ORIGIN, "--out", str(OUT), "--manifest", str(MANIFEST),
                      "--dry-run", "--emit-html"] + list(extra))
    return rc, buf.getvalue()


print("=== publishing with hearts turned on ===")
rc, log = publish("live.html", "--hearts-url", "/api/hearts",
                  "--hearts-token", "browser-test-token")
check("publish succeeds", rc == 0, f"rc={rc}\n{log[-300:]}")
page = (OUT / "live.html").read_text(encoding="utf-8")
check("the endpoint is baked into the markup", 'data-hearts="/api/hearts"' in page)
check("no admin token leaked into the page", "browser-test-token" not in page)

# A dry run must not touch the service; the allowlist is registered on a real
# publish only. Register it here the way a real publish would.
items = pg.load_shortlist(OUT / "scores.sqlite3", OUT, LIB)
client = hearts.app.test_client()
r = client.post("/api/hearts/_photos",
                json={"photos": [{"photo_id": i["photo_id"], "rel_path": i["rel_path"]}
                                 for i in items]},
                headers={"X-Admin-Token": "browser-test-token"})
check("the allowlist registers", r.status_code == 200 and r.json["registered"] == len(items),
      str(r.json))

print("\n=== fixing the allowlist without republishing ===")
# The failure this exists for: buttons render, every click says
# "unknown photo_id" because the allowlist was never registered.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = pg.main(["--site", ORIGIN, "--out", str(OUT), "--manifest", str(MANIFEST),
                  "--hearts-url", "/api/hearts", "--hearts-register-only",
                  "--hearts-token", "browser-test-token"])
log2 = buf.getvalue()
check("register-only succeeds", rc == 0, f"rc={rc}\n{log2[-300:]}")
check("it says what it did", "Allowlist updated" in log2, log2[-200:])
check("and it did not touch the Ghost page", "Created page" not in log2
      and "Updated page" not in log2)
check("it needs to know where the service is",
      pg.main(["--site", ORIGIN, "--out", str(OUT), "--manifest", str(MANIFEST),
               "--hearts-register-only"]) == 2)

# The fixture sets HEARTS_ADMIN_TOKEN for the service itself; drop it for this
# one call so we see what a user with no token configured actually gets.
_saved = os.environ.pop("HEARTS_ADMIN_TOKEN")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pg.main(["--site", ORIGIN, "--out", str(OUT), "--manifest", str(MANIFEST),
             "--hearts-url", "/api/hearts", "--dry-run", "--emit-html", "warn.html"])
warn = buf.getvalue()
os.environ["HEARTS_ADMIN_TOKEN"] = _saved
check("publishing with hearts but no token warns loudly",
      "WARNING" in warn and "unknown photo_id" in warn, warn[-400:])
check("the warning names the way out",
      "--hearts-register-only" in warn, warn[-400:])
# A dry run must never contact the service, but it should still say what a
# real run would do.
_calls = STATE["api_calls"]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    pg.main(["--site", ORIGIN, "--out", str(OUT), "--manifest", str(MANIFEST),
             "--hearts-url", "/api/hearts", "--hearts-token", "browser-test-token",
             "--dry-run", "--emit-html", "warn2.html"])
dry = buf.getvalue()
check("a dry run with a token says what a real one WOULD do",
      "would be registered" in dry,
      [l for l in dry.splitlines() if "Hearts" in l])
check("and contacts the service anyway - no", STATE["api_calls"] == _calls,
      f"{STATE['api_calls'] - _calls} calls made during a dry run")

print("\n=== in a browser ===")
from playwright.sync_api import sync_playwright                      # noqa: E402

with sync_playwright() as pw:
    br = pw.chromium.launch()
    errors = []
    ctx = br.new_context()
    P = ctx.new_page()
    P.on("pageerror", lambda e: errors.append(str(e)))
    P.set_viewport_size({"width": 1400, "height": 950})
    P.goto(f"{ORIGIN}/live.html"); P.wait_for_timeout(700)

    def rows():
        return P.eval_on_selector_all(".psc-hearts", "e => e.length")
    def shown_rows():
        return P.eval_on_selector_all(
            ".psc-hearts:not(.psc-pending)", "e => e.length")
    def counts():
        return P.eval_on_selector_all(
            ".psc-hcount", "e => e.map(x => x.textContent)")
    def filled():
        return P.eval_on_selector_all(".psc-heart.on", "e => e.length")

    total = P.eval_on_selector_all(".psc-card", "e => e.length")
    check("cards rendered", total > 0, str(total))
    check("every card has a heart", rows() == total, f"{rows()} of {total}")
    check("the row is revealed once the service answers", shown_rows() == total,
          f"{shown_rows()} of {total}")
    check("nothing is hearted yet", filled() == 0)
    check("a zero count shows nothing rather than a 0",
          all(c == "" for c in counts()), str(counts()[:3]))

    print("\n--- clicking")
    first = P.locator(".psc-heart").first
    pid = first.get_attribute("data-photo-id")
    first.click(); P.wait_for_timeout(400)
    check("the heart fills in", filled() == 1, str(filled()))
    check("the count appears",
          P.eval_on_selector(".psc-hcount", "e => e.textContent") == "1",
          P.eval_on_selector(".psc-hcount", "e => e.textContent"))
    check("it is recorded as pressed, for a screen reader",
          P.eval_on_selector(".psc-heart", "e => e.getAttribute('aria-pressed')") == "true")
    check("the service really stored it",
          client.get("/api/hearts").json["counts"].get(pid) == 1,
          str(client.get("/api/hearts").json["counts"]))

    first.click(); P.wait_for_timeout(400)
    check("clicking again removes it", filled() == 0)
    check("and the count goes back to nothing",
          P.eval_on_selector(".psc-hcount", "e => e.textContent") == "")
    check("the service agrees", not client.get("/api/hearts").json["counts"].get(pid))

    first.click(); P.wait_for_timeout(400)
    check("hearting a second time works", filled() == 1)

    print("\n--- it is remembered")
    P.reload(); P.wait_for_timeout(700)
    check("still filled after a reload", filled() == 1, str(filled()))
    check("still counted after a reload",
          P.eval_on_selector(".psc-hcount", "e => e.textContent") == "1")

    print("\n--- a different visitor")
    ctx2 = br.new_context()          # a fresh browser: no localStorage, new token
    P2 = ctx2.new_page()
    P2.goto(f"{ORIGIN}/live.html"); P2.wait_for_timeout(700)
    check("sees the count", P2.eval_on_selector(".psc-hcount", "e => e.textContent") == "1")
    check("but it is not THEIR heart",
          P2.eval_on_selector_all(".psc-heart.on", "e => e.length") == 0)
    P2.locator(".psc-heart").first.click(); P2.wait_for_timeout(400)
    check("their click adds a second heart",
          P2.eval_on_selector(".psc-hcount", "e => e.textContent") == "2",
          P2.eval_on_selector(".psc-hcount", "e => e.textContent"))
    check("the service counts two distinct browsers",
          client.get("/api/hearts").json["counts"].get(pid) == 2,
          str(client.get("/api/hearts").json["counts"]))
    P.reload(); P.wait_for_timeout(700)
    check("the first visitor now sees two as well",
          P.eval_on_selector(".psc-hcount", "e => e.textContent") == "2")
    check("and their own heart is still theirs", filled() == 1)
    ctx2.close()

    print("\n--- the service falls over mid-session")
    STATE["broken"] = True
    before = P.eval_on_selector(".psc-hcount", "e => e.textContent")
    P.locator(".psc-heart").first.click(); P.wait_for_timeout(600)
    check("the optimistic change is rolled back",
          P.eval_on_selector(".psc-hcount", "e => e.textContent") == before,
          f"{P.eval_on_selector('.psc-hcount', 'e => e.textContent')} vs {before}")
    check("and the visitor is told", "Could not save" in
          P.eval_on_selector(".psc-toast", "e => e.textContent"),
          P.eval_on_selector(".psc-toast", "e => e.textContent"))
    check("the button is usable again, not stuck disabled",
          P.eval_on_selector(".psc-heart", "e => e.disabled") is False)

    print("\n--- sorting by Most liked")
    STATE["broken"] = False
    P.reload(); P.wait_for_timeout(700)
    opts = P.eval_on_selector_all(".psc-sort option", "e => e.map(x => x.value)")
    check("the dropdown offers it", "hearts-desc" in opts and "hearts-asc" in opts, str(opts[:3]))
    check("and Most liked is the first choice offered", opts[0] == "hearts-desc", str(opts[0]))

    # Give three photographs different tallies, from three separate browsers, so
    # the counts are real rather than one person clicking repeatedly.
    ids = P.eval_on_selector_all(".psc-heart", "e => e.map(x => x.dataset.photoId)")
    plan = {ids[3]: 3, ids[1]: 2, ids[5]: 1}
    for pid, n in plan.items():
        for k in range(n):
            c = hearts.app.test_client()
            c.post(f"/api/hearts/{pid}",
                   headers={"X-Heart-Token": f"sortvoter-{pid[:6]}-{k}-aaaa"})
    P.reload(); P.wait_for_timeout(800)

    def heart_order():
        return P.eval_on_selector_all(
            ".psc-card:not(.psc-hidden)", "e => e.map(x => +(x.dataset.hearts || 0))")

    truth = hearts.app.test_client().get("/api/hearts").json["counts"]
    P.select_option(".psc-sort", "hearts-desc"); P.wait_for_timeout(400)
    got = heart_order()
    check("most liked first", got == sorted(got, reverse=True), str(got))
    check("the tallies on screen match the service exactly",
          sorted(got, reverse=True) ==
          sorted([truth.get(i, 0) for i in ids], reverse=True),
          f"page {got} vs service {sorted(truth.values(), reverse=True)}")
    check("the most-liked photograph really is first",
          got[0] == max(truth.values()), f"{got[0]} vs {max(truth.values())}")
    check("unliked photographs sink to the bottom", got[-1] == 0)

    P.select_option(".psc-sort", "hearts-asc"); P.wait_for_timeout(400)
    got = heart_order()
    check("least liked first", got == sorted(got), str(got))

    print("\n--- liking something does not make the grid jump")
    P.select_option(".psc-sort", "hearts-desc"); P.wait_for_timeout(400)
    before_order = P.eval_on_selector_all(
        ".psc-card:not(.psc-hidden)", "e => e.map(x => x.dataset.name)")
    P.locator(".psc-card:not(.psc-hidden)").last.locator(".psc-heart").click()
    P.wait_for_timeout(500)
    after_order = P.eval_on_selector_all(
        ".psc-card:not(.psc-hidden)", "e => e.map(x => x.dataset.name)")
    check("the order holds still while you click", before_order == after_order,
          "photographs moved under the cursor")
    check("but the count did go up",
          P.eval_on_selector_all(".psc-card:not(.psc-hidden)",
                                 "e => e[e.length-1].dataset.hearts") == "1")

    print("\n--- the sort survives a reload, ordered by the new counts")
    P.reload(); P.wait_for_timeout(400)
    P.select_option(".psc-sort", "hearts-desc"); P.wait_for_timeout(600)
    got = heart_order()
    check("still ranked correctly after a reload", got == sorted(got, reverse=True), str(got))

    print("\n--- showing only photographs with likes")
    def shown():
        return P.eval_on_selector_all(".psc-card:not(.psc-hidden)", "e => e.length")
    P.reload(); P.wait_for_timeout(800)
    P.select_option(".psc-sort", "score-desc"); P.wait_for_timeout(300)
    all_n = shown()
    truth = hearts.app.test_client().get("/api/hearts").json["counts"]
    on_page = P.eval_on_selector_all(".psc-heart", "e => e.map(x => x.dataset.photoId)")
    expect = sum(1 for i in on_page if truth.get(i, 0) > 0)

    check("the button is there", P.eval_on_selector_all(
        ".psc-likedonly", "e => e.length") == 1)
    check("and it is visible once the counts have landed",
          P.eval_on_selector(".psc-likedonly",
                             "e => !e.classList.contains('psc-pending')"))

    P.click(".psc-likedonly"); P.wait_for_timeout(400)
    got = heart_order()
    check("only liked photographs remain", all(n > 0 for n in got), str(got))
    check("and ALL of them do", len(got) == expect, f"{len(got)} shown, {expect} liked")
    check("the counter agrees",
          P.eval_on_selector(".psc-count", "e => e.textContent")
          == f"{expect} of {all_n}",
          P.eval_on_selector(".psc-count", "e => e.textContent"))
    check("the button shows as active",
          P.eval_on_selector(".psc-likedonly", "e => e.classList.contains('on')"))

    print("\n--- and it composes with the other filters")
    P.click('.psc-bar button[data-band="STRONG"]'); P.wait_for_timeout(300)
    verds = P.eval_on_selector_all(".psc-card:not(.psc-hidden)",
                                   "e => e.map(x => x.dataset.verdict)")
    got = heart_order()
    check("Strong AND liked, not one or the other",
          all(v == "STRONG" for v in verds) and all(n > 0 for n in got),
          f"{set(verds)}, {got}")
    P.click('.psc-bar button[data-band="all"]'); P.wait_for_timeout(300)

    P.fill(".psc-q", "Wyoming"); P.wait_for_timeout(400)
    got = heart_order()
    folders = P.eval_on_selector_all(".psc-card:not(.psc-hidden)",
                                     "e => e.map(x => x.dataset.folder)")
    check("and with the search box",
          all(n > 0 for n in got) and all("Wyoming" in f for f in folders),
          f"{folders}, {got}")
    P.fill(".psc-q", ""); P.wait_for_timeout(300)

    print("\n--- turning it back off")
    P.click(".psc-likedonly"); P.wait_for_timeout(400)
    check("everything is back", shown() == all_n, f"{shown()} of {all_n}")
    check("and the button is no longer active",
          not P.eval_on_selector(".psc-likedonly", "e => e.classList.contains('on')"))

    print("\n=== THE IMPORTANT ONE: the gallery with no heart service ===")
    STATE["broken"] = True          # the sorting block above healed it
    calls_before = STATE["api_calls"]
    P.reload(); P.wait_for_timeout(900)
    check("the service was definitely asked", STATE["api_calls"] > calls_before)
    check("no heart row is shown", shown_rows() == 0, f"{shown_rows()} visible")
    check("every photograph still renders",
          P.eval_on_selector_all(".psc-card", "e => e.length") == total)
    check("search still works",
          (P.fill(".psc-q", "Wyoming"), P.wait_for_timeout(300),
           P.eval_on_selector_all(".psc-card:not(.psc-hidden)", "e => e.length"))[2] > 0)
    P.fill(".psc-q", ""); P.wait_for_timeout(200)
    P.select_option(".psc-sort", "date-asc"); P.wait_for_timeout(300)
    ds = [d for d in P.eval_on_selector_all(
        ".psc-card:not(.psc-hidden)", "e => e.map(x => x.dataset.date)") if d]
    check("sorting still works", ds == sorted(ds), str(ds[:2]))
    card = P.locator(".psc-card").first
    card.locator(".psc-taginput").click(); card.locator(".psc-taginput").type("Still Works,")
    P.wait_for_timeout(300)
    check("tagging still works",
          "Still Works" in P.eval_on_selector_all(
              ".psc-card .psc-tag", "e => e.map(x => x.firstChild.textContent)"))
    P.locator(".psc-card:not(.psc-hidden) img").first.click(); P.wait_for_timeout(400)
    check("the lightbox still opens",
          P.eval_on_selector(".psc-lb", "e => e.classList.contains('open')"))
    P.keyboard.press("Escape"); P.wait_for_timeout(200)
    check("no JS errors, even with the service failing", not errors, str(errors[:2]))

    print("\n=== published without --hearts-url at all ===")
    STATE["broken"] = False
    rc, _ = publish("nohearts.html")
    check("publish succeeds", rc == 0)
    check("no endpoint in the markup",
          "data-hearts=" not in (OUT / "nohearts.html").read_text(encoding="utf-8"))
    P3 = ctx.new_page()
    calls_before = STATE["api_calls"]
    P3.goto(f"{ORIGIN}/nohearts.html"); P3.wait_for_timeout(700)
    check("no heart button is rendered at all",
          P3.eval_on_selector_all(".psc-heart", "e => e.length") == 0)
    check("and no Most liked option, since nothing counts them",
          "hearts-desc" not in P3.eval_on_selector_all(
              ".psc-sort option", "e => e.map(x => x.value)"))
    check("and no Liked filter either",
          P3.eval_on_selector_all(".psc-likedonly", "e => e.length") == 0)
    check("and the service is never contacted", STATE["api_calls"] == calls_before,
          f"{STATE['api_calls'] - calls_before} calls")
    check("the gallery is otherwise complete",
          P3.eval_on_selector_all(".psc-card", "e => e.length") == total)
    br.close()

httpd.shutdown()
print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
