# Contributing to Photo Scout

Contributions are welcome: patches, documentation, and bug reports alike.

Field reports are particularly valuable. Photo Scout is exercised by fourteen test
suites, but a test library is a model of a real one, and models have edges. RAW
files from a camera the project has not met, a folder structure nobody anticipated,
an archive an order of magnitude larger than the ones it was built against — those
are the conditions that turn up the interesting faults, and they are hard to
manufacture.

By contributing you agree that your work is licensed under the
**GNU General Public License v3.0 or later**, the same as the rest of the project.

---

## The one rule with no exceptions

**The photo library is read-only.** No feature may write, move or delete anything
inside `--root`. Not a cache, not a sidecar file, not a thumbnail.

This is not a style preference. People point this tool at decades of irreplaceable
work, and the promise that it cannot touch that work is the reason they are willing
to. `tests/_selftest_readonly.py` proves it: it SHA-256 fingerprints every file in
a test library — contents, size, modification time, and the shape of the tree —
before and after a full pipeline run, and asserts byte-for-byte identity. Do not
weaken that test to make a feature fit.

---

## Prove it, don't assert it

Every behaviour worth relying on has a test that demonstrates it, and the tests are
deliberately adversarial rather than confirmatory.

Some examples of what that means here:

- The date-sorting fixture gives every photograph in a folder the same date but a
  different time, **written in the opposite order to the file names**, so a sort
  that stopped at the day cannot accidentally look correct.
- The heart-service test greps the raw database file for the visitor tokens it sent,
  to prove none were stored.
- The resolution test first *demonstrates the failure mode* — the same photograph
  measured at two native sizes produces wildly different Laplacian variance — and
  only then asserts that the pipeline is immune to it. An invariant is more
  convincing next to the bug it prevents.
- The browser tests drive a real Chromium. Several genuine bugs were caught only
  because a test clicked something rather than inspecting the generated HTML —
  including one where the lightbox opened the wrong photograph after sorting,
  because it walked a list captured at page load rather than the live DOM.

When you fix a bug, add the test that would have caught it, then **check the test
fails without your fix**. A regression test that passes against the broken code is
worse than none, because it looks like protection.

---

## Running the tests

The suites live in `tests/`. Each is a standalone script rather than a pytest
module: run it directly and it prints `PASS` or `FAIL` per check and exits non-zero
if anything failed. They import the scripts from the repository root, so run them
from anywhere — the path is resolved from the file's own location.

```bash
pip install playwright numpy pillow flask waitress
python -m playwright install chromium

python tests/_selftest.py                  # walking, dedup, database, scoring maths
python tests/_selftest_readonly.py         # proves the library is never modified
```

All of them, before opening a pull request:

```bash
for t in tests/_selftest*.py; do python "$t" >/dev/null 2>&1 \
  && echo "PASS $t" || echo "FAIL $t"; done
```

| Suite | Covers |
|---|---|
| `_selftest.py` | Walking, dedup, the database, reports, scoring maths |
| `_selftest_readonly.py` | Proves the photo library is never modified |
| `_selftest_video.py` | ffmpeg sampling, timestamp accuracy, still extraction |
| `_selftest_clip.py` | CLIP API compatibility across transformers versions |
| `_selftest_lightbox.py` | Preview rendering and the overlay |
| `_selftest_meta.py` | EXIF dates and times, folder names, search, sorting |
| `_selftest_strongtop.py` | The shortlist variant against a shared database |
| `_selftest_hidden.py` | `hide_from_photo_scout` folders |
| `_selftest_minsize.py` | The size floor, and that resolution never moves a score |
| `_selftest_tags.py` | Tag markup, validation, XSS resistance |
| `_selftest_tags_browser.py` | The tag UI in a real browser |
| `_selftest_ghost.py` | The Ghost publisher against a mock Admin API |
| `_selftest_hearts.py` | The heart service: API, abuse handling, persistence |
| `_selftest_hearts_browser.py` | Heart buttons in a browser, including with the service down |

The browser suites are the slowest. They are also the ones that have caught the most.

The leading underscore keeps pytest from collecting them: they execute at import
rather than defining test functions, so a bare `pytest` run would do something
surprising.

---

## Things that will surprise you

**`photo_scout_strong_top.py` is generated, not written.** `_make_variant.py`
derives it from `photo_scout.py` by targeted substitution. Edit the parent and
re-run the generator; a hand-edit will be silently overwritten.

**The scoring defaults are placeholders.** `AESTHETIC_RANGE` and the band cutoffs
get replaced automatically by calibration on first run. If you are comparing scores
across machines, compare calibrated ones or you are comparing nothing.

---

## Style

Plain Python, no framework, standard library wherever it is reasonable. The heart
service uses Flask because that is genuinely simpler than hand-rolling one; the
Ghost client mints its own JWTs rather than adding a dependency for thirty lines of
HMAC. Keep that balance unless a dependency really pays for itself.

**Comments explain why, not what.** This code is read far more often than it is
written, frequently by people who are not professional developers. A comment that
records the reasoning behind a non-obvious choice, or names the bug a line exists to
prevent, earns its place:

```python
# 64-bit hashes overflow SQLite's signed INTEGER, so they are stored as hex text.
```

A comment that restates the syntax does not.

**Errors should say what to do next.** When something fails, the message should name
the cause and the fix. The heart service, for instance, catches the "cannot open
database" error that a root-owned Docker volume produces and prints the exact
`chown` command, rather than a traceback repeated every two seconds.

---

## Reporting a bug

Useful reports include:

- what you ran, verbatim, with any secrets masked
- what happened, including the full error
- your platform, Python version, and whether you are on GPU or CPU
- roughly how large the library is, and what file formats are in it

If it involves the report page, the browser matters too. If it involves RAW
decoding, the camera model does.
