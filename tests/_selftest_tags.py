"""
Tagging verification: Python-side sanitising and loading, the payload baked into
the report, CSV export, --reset preservation, and the presence/consistency of the
browser-side machinery (colours, chips, dropdown, filtering).
"""
import io, contextlib, json, re, shutil, sys
from pathlib import Path
import numpy as np
from PIL import Image

# The suites live in tests/ but import the scripts from the repository root,
# so ROOT - not this file's own folder - is what goes on sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import photo_scout as ps

LIB = Path("/tmp/tag_lib"); OUT = Path("/tmp/tag_out")
for d in (LIB, OUT):
    shutil.rmtree(d, ignore_errors=True)
LIB.mkdir(parents=True)
rng = np.random.default_rng(90)
d = LIB / "2011 Wyoming"; d.mkdir()
for i in range(6):
    Image.fromarray(rng.integers(0, 255, (10, 15, 3), dtype=np.uint8)) \
         .resize((800, 560), Image.BICUBIC).save(d / f"DSC_{i:04d}.JPG", "JPEG", quality=90)

class FakeScorer:
    def __init__(self, *a, **k): self.n = 0
    def score(self, img):
        self.n += 1
        return {"aesthetic_raw": 5.0 + self.n * 0.1, "nima_raw": 5.0,
                "subject_score": 90.0, "subject_label": ps.PRIMARY_PROMPTS[0][1],
                "subject_tier": "primary"}
ps.Scorer = FakeScorer
ps.DEFAULT_OUT_DIR = OUT

ok = True
def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))

def run(*a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ps.main(["--root", str(LIB)] + list(a))
    return rc, buf.getvalue()

print("=== sanitize_tag ===")
cases = [
    ("Lake Photos", "Lake Photos", "multi-word kept"),
    ("  Lake   Photos  ", "Lake Photos", "whitespace collapsed and trimmed"),
    ("snow-capped_peak2", "snow-capped_peak2", "hyphen, underscore, digits kept"),
    ("Lake!@#$%^&*()Photos", "LakePhotos", "punctuation stripped"),
    ("<script>alert(1)</script>", "scriptalert1script", "angle brackets and quotes gone"),
    ('say "hi"', "say hi", "quotes gone"),
    ("../../etc/passwd", "etcpasswd", "path characters gone"),
    ("Tag\nWith\tControl", "Tag With Control", "newlines and tabs become spaces"),
    ("!!!", "", "punctuation-only rejected"),
    ("   ", "", "whitespace-only rejected"),
    ("---", "", "delimiter-only rejected"),
    ("x" * 80, "x" * 40, "capped at 40"),
    ("|pipe|", "pipe", "pipe stripped (it is the internal delimiter)"),
    (None, "", "non-string rejected"),
    (12345, "", "number rejected"),
]
for raw, want, why in cases:
    got = ps.sanitize_tag(raw)
    check(f"{why}", got == want, f"{raw!r} -> {got!r} (want {want!r})")

print("\n=== load_tags ===")
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "tags.json").write_text(json.dumps({
    str(d / "DSC_0000.JPG"): ["Lake Photos", "Sunset", "lake photos", "  ", "b@d!"],
    str(d / "DSC_0001.JPG"): ["Desert"],
    str(d / "DSC_0002.JPG"): [],
    "bad-value": "not a list",
    "another": 42,
}), encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    tags = ps.load_tags(OUT)
check("valid entries loaded", len(tags) == 2, str(sorted(Path(k).name for k in tags)))
check("case-insensitive dedupe keeps first spelling",
      tags[str(d / "DSC_0000.JPG")] == ["Lake Photos", "Sunset", "bd"],
      str(tags[str(d / "DSC_0000.JPG")]))
check("empty tag list dropped", str(d / "DSC_0002.JPG") not in tags)
check("non-list values dropped", "bad-value" not in tags and "another" not in tags)
check("malformed entries reported", "ignored" in buf.getvalue())

(OUT / "tags.json").write_text("{ this is not json", encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    broken = ps.load_tags(OUT)
check("corrupt file ignored, not fatal", broken == {})
check("corrupt file left on disk", (OUT / "tags.json").exists())
check("corruption explained", "Could not read" in buf.getvalue())

(OUT / "tags.json").write_text('["a list, not an object"]', encoding="utf-8")
with contextlib.redirect_stdout(io.StringIO()):
    check("non-object json ignored", ps.load_tags(OUT) == {})

print("\n=== tags reach the report ===")
real_tags = {
    str(d / "DSC_0000.JPG"): ["Lake Photos", "Sunset"],
    str(d / "DSC_0001.JPG"): ["Desert", "Lake Photos"],
}
(OUT / "tags.json").write_text(json.dumps(real_tags), encoding="utf-8")
rc, log = run()
check("run succeeded with tags present", rc == 0)
check("load reported", "Loaded 4 tags across 2 images" in log, "")
h = (OUT / "report.html").read_text(encoding="utf-8")

m = re.search(r"const TAGS = (\{.*?\});", h, re.S)
check("payload embedded", m is not None)
payload = json.loads(m.group(1)) if m else {}
check("payload matches tags.json", payload == real_tags, str(sorted(payload)))
check("placeholders replaced", "__TAGS_JSON__" not in h and "__STORAGE_KEY__" not in h)
sk = re.search(r"const STORE_KEY = '([^']+)'", h)
check("storage key present and scoped", bool(sk) and sk.group(1).startswith("photo_scout_tags_"),
      sk.group(1) if sk else "")

print("\n=== per-card markup ===")
check("every card has a tag key", h.count("data-tagkey=") == 6, str(h.count("data-tagkey=")))
check("every card has a tag input", h.count('class="taginput"') == 6)
check("comma/Enter hinted in the placeholder", "add tag, comma or Enter" in h)
check("tag keys are the database paths",
      all(str(d / f"DSC_{i:04d}.JPG").replace("\\", "&#92;") in h or
          str(d / f"DSC_{i:04d}.JPG") in h for i in range(6)))

print("\n=== browser-side machinery is present and consistent ===")
for frag, why in [
    ("function cleanTag", "sanitiser mirrored in JS"),
    ("[^A-Za-z0-9 _-]", "same character rule as Python"),
    ("function tagHue", "deterministic colour"),
    ("137.508", "golden-angle hue spacing"),
    ("function paintTag", "colour applied to chips"),
    ("createTextNode(name)", "tags rendered as text, never innerHTML"),
    ("localStorage.setItem(STORE_KEY", "persisted to localStorage"),
    ("beforeunload", "warns about unsaved tags"),
    ("id=\"tagmenu\"", "dropdown container"),
    ("function openMenu", "live typeahead"),
    ("ArrowDown", "keyboard navigation"),
    ("selectTag(menuItems[menuIdx])", "Enter selects the highlighted tag"),
    ("localeCompare", "alphabetical ordering"),
    ("remove from search", "chips are removable"),
    ("a.download = 'tags.json'", "export button"),
]:
    check(why, frag in h)

check("chip filter is exact via the pipe delimiter",
      "includes('|' + t.toLowerCase() + '|')" in h)
check("selected tags are ORed, not ANDed",
      "let okT = selected.length === 0;" in h and
      "if (cardTags.includes('|' + t.toLowerCase() + '|')) { okT = true; break; }" in h)
check("no leftover AND logic", "okT = false" not in h)
check("card tag data is pipe-delimited",
      "'|' + tagsFor(key).join('|').toLowerCase() + '|'" in h)
check("tag filter joins the other filters", "okV && okD && okQ && okK && okF && okT" in h)
check("free text also searches tags", "cardTags.includes(query)" in h)
check("no stray control characters in the report",
      not any(ord(c) < 9 or 13 < ord(c) < 32 for c in h))

print("\n=== the JS and Python sanitisers agree (executed, not assumed) ===")
import subprocess, shutil as _sh
if not _sh.which("node"):
    print("SKIP  node not available")
else:
    fn = re.search(r"( function cleanTag\(raw\) \{.*?\n \})", h, re.S)
    hue = re.search(r"( function tagHue\(name\) \{.*?\n \})", h, re.S)
    check("cleanTag extracted from the report", fn is not None)
    check("tagHue extracted from the report", hue is not None)
    corpus = [
        "Lake Photos", "  Lake   Photos  ", "snow-capped_peak2",
        "Lake!@#$%^&*()Photos", "<script>alert(1)</script>", 'say "hi"',
        "../../etc/passwd", "Tag\nWith\tControl", "!!!", "   ", "---",
        "x" * 80, "|pipe|", "a,b", "Mount   Timpanogos", "-leading", "trailing-",
        "_under_", "MiXeD CaSe", "123", "e" * 39 + "!!", "tab\there",
        "multi\n\nline\n\ntag", "  -_- ", "Zion & Bryce", "50%_grade",
    ]
    js = (fn.group(1) if fn else "") + "\n" + (hue.group(1) if hue else "") + """
const TAG_MAX = 40;
const inp = JSON.parse(process.argv[1]);
console.log(JSON.stringify({
  clean: inp.map(cleanTag),
  hues:  inp.map(cleanTag).filter(Boolean).map(tagHue)
}));
"""
    Path("/tmp/_tagjs.js").write_text("const TAG_MAX = 40;\n" + (fn.group(1) if fn else "")
                                      + "\n" + (hue.group(1) if hue else "") + """
const inp = JSON.parse(process.argv[2]);
console.log(JSON.stringify({clean: inp.map(cleanTag),
                            hues: inp.map(cleanTag).filter(Boolean).map(tagHue)}));
""", encoding="utf-8")
    out = subprocess.run(["node", "/tmp/_tagjs.js", json.dumps(corpus)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        check("node ran the extracted JS", False, out.stderr[:200])
    else:
        res = json.loads(out.stdout)
        pyc = [ps.sanitize_tag(c) for c in corpus]
        mismatches = [(c, a, b) for c, a, b in zip(corpus, pyc, res["clean"]) if a != b]
        check("JS cleanTag matches Python sanitize_tag on every input",
              not mismatches, str(mismatches[:3]))
        check("newline case now separates words",
              ps.sanitize_tag("Tag\nWith\tControl") == "Tag With Control",
              ps.sanitize_tag("Tag\nWith\tControl"))
        check("JS never emits a disallowed character",
              all(re.fullmatch(r"[A-Za-z0-9 _-]*", t) for t in res["clean"]))
        check("JS never emits a pipe", all("|" not in t for t in res["clean"]))
        check("JS respects the length cap", all(len(t) <= 40 for t in res["clean"]))
        hues = res["hues"]
        check("hues are in range", all(0 <= x < 360 for x in hues), str(sorted(set(hues))[:6]))
        distinct = len(set(hues)) / max(len(set(t for t in res["clean"] if t)), 1)
        check("distinct tags get distinct hues", distinct > 0.85, f"{distinct:.0%} unique")
        # determinism: same input twice must give the same hue
        out2 = subprocess.run(["node", "/tmp/_tagjs.js", json.dumps(corpus)],
                              capture_output=True, text=True)
        check("hues are deterministic across runs",
              json.loads(out2.stdout)["hues"] == hues)

print("\n=== CSV export ===")
import csv as _csv
with open(OUT / "report.csv", encoding="utf-8-sig") as fh:
    rows = list(_csv.DictReader(fh))
check("csv has a tags column", "tags" in rows[0])
by_name = {r["filename"]: r for r in rows}
check("tags exported for the right image",
      by_name["DSC_0000.JPG"]["tags"] == "Lake Photos; Sunset",
      by_name["DSC_0000.JPG"]["tags"])
check("untagged images have an empty cell", by_name["DSC_0005.JPG"]["tags"] == "")

print("\n=== --reset preserves tags ===")
before = (OUT / "tags.json").read_text(encoding="utf-8")
rc, log = run("--reset", "--yes")
check("reset succeeded", rc == 0)
check("reset warned that tags are preserved", "will be PRESERVED" in log)
check("tags.json still exists", (OUT / "tags.json").exists())
check("tags.json byte-identical", (OUT / "tags.json").read_text(encoding="utf-8") == before)
check("derived output really was deleted and rebuilt",
      "Deleted." in log and (OUT / "scores.sqlite3").exists())
h2 = (OUT / "report.html").read_text(encoding="utf-8")
m2 = re.search(r"const TAGS = (\{.*?\});", h2, re.S)
check("tags survive into the rebuilt report", json.loads(m2.group(1)) == real_tags)

print("\n=== no tags at all is fine ===")
(OUT / "tags.json").unlink()
rc, log = run("--report-only")
h3 = (OUT / "report.html").read_text(encoding="utf-8")
check("report builds with no tag file", rc == 0)
check("empty payload emitted", "const TAGS = {};" in h3)
check("tag inputs still rendered", h3.count('class="taginput"') == 6)

print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
sys.exit(0 if ok else 1)
