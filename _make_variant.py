# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brian Salisbury and contributors.
# Part of Photo Scout. This program comes with ABSOLUTELY NO WARRANTY.
# See the LICENSE file, or <https://www.gnu.org/licenses/>.
"""Regenerates photo_scout_strong_top.py from the current photo_scout.py."""
from pathlib import Path
H = Path(__file__).parent
src = (H / "photo_scout.py").read_text(encoding="utf-8")
s = src
def rep(a, b):
    global s
    assert a in s, f"MISSING: {a[:70]!r}"
    s = s.replace(a, b, 1)

rep('''photo_scout.py - Local, zero-cost quality scoring for a photo and video library.''',
'''photo_scout_strong_top.py - shortlist-only variant of photo_scout.py.

Identical scoring in every respect. The ONLY difference is the report: it shows
just the TOP PICK and STRONG photographs, with the All / Maybe / Pass controls
removed. Everything below STRONG is still scored and still stored - it simply
isn't rendered here.

Shares the output directory, scores.sqlite3 and tags.json of photo_scout.py, so
you never score or tag the library twice. Outputs are named report_strong_top.*
so the two reports never overwrite each other.''')

rep('''# Verdict bands applied to the final composite score.''',
'''# This variant renders only these verdicts. Everything else is still scored and
# still written to the shared database - it just isn't shown in this report.
REPORT_VERDICTS = ("TOP PICK", "STRONG")

# Verdict bands applied to the final composite score.''')

rep('''    write_html(rows, out_dir / "report.html", root, stats, tags)
    write_csv(rows, out_dir / "report.csv", tags)

    shortlist = [r for r in rows if r["verdict"] in ("TOP PICK", "STRONG") and not r["dup_of"]]
    write_csv(shortlist, out_dir / "shortlist.csv", tags)

    log(f"Report:    {out_dir / 'report.html'}")
    log(f"  NB the report is library-wide: it covers all {stats['folders']} folders "
        f"scored so far, not only the one just processed. Use the folder dropdown "
        f"in the report to narrow it.")
    log(f"CSV:       {out_dir / 'report.csv'}")
    log(f"Shortlist: {out_dir / 'shortlist.csv'}  ({len(shortlist)} images)")
    log(f"Stats: {stats}")''',
'''    write_html(shown, out_dir / "report_strong_top.html", root, stats, tags)
    write_csv(shown, out_dir / "report_strong_top.csv", tags)

    log(f"Shortlist report: {out_dir / 'report_strong_top.html'}")
    log(f"  {stats['top']} top picks + {stats['strong']} strong = {len(keepers)} "
        f"photographs, selected from {stats['total']} scored across "
        f"{stats['folders']} folders.")
    if stats["hidden_dups"]:
        log(f"  {stats['hidden_dups']} near-duplicates of shortlisted frames are "
            f"included but hidden behind the checkbox.")
    if not keepers:
        log("  Nothing reached STRONG yet. If the library is fully scored, the "
            "bands may need fitting - run: photo_scout.py --calibrate")
    log(f"CSV:              {out_dir / 'report_strong_top.csv'}")
    log("  (photo_scout.py's report.html / report.csv / shortlist.csv are untouched)")''')

rep('''def build_reports(cache: Cache, out_dir: Path, root: Path,
                  min_edge: int = MIN_IMAGE_EDGE) -> None:
    rows = filter_hidden(cache.all_rows(), root, min_edge)
    tags = load_tags(out_dir)''',
'''def build_reports(cache: Cache, out_dir: Path, root: Path,
                  min_edge: int = MIN_IMAGE_EDGE) -> None:
    all_rows = filter_hidden(cache.all_rows(), root, min_edge)
    tags = load_tags(out_dir)

    # The shortlist. Near-duplicates OF shortlisted frames stay in the markup so
    # the "show near-duplicates" checkbox still does something, but they remain
    # hidden by default exactly as in the full report.
    shown = [r for r in all_rows
             if not r["error"] and r["verdict"] in REPORT_VERDICTS]
    keepers = [r for r in shown if not r["dup_of"]]

    rows = all_rows  # the stats below describe the whole library, not the shortlist''')

rep('''        "extracted": sum(1 for r in rows if r["extracted_path"]),
    }''',
'''        "extracted": sum(1 for r in rows if r["extracted_path"]),
        "shortlisted": len(keepers),
        "hidden_dups": sum(1 for r in shown if r["dup_of"]),
    }''')

rep('''    stats_html = (
        f"<b>{stats['total']}</b> scored "
        f"({stats['photos']} photos, {stats['video_frames']} frames from "
        f"{stats['videos']} videos) &middot; "
        f"<b>{stats['top']}</b> top picks &middot; "
        f"<b>{stats['strong']}</b> strong &middot; "
        f"<b>{stats['maybe']}</b> maybe &middot; "
        f"<b>{stats['extracted']}</b> stills extracted from video &middot; "
        f"<b>{stats['dups']}</b> near-duplicates hidden &middot; "
        f"<b>{stats['folders']}</b> folders &middot; "
        f"<b>{stats['errors']}</b> errors"
    )''',
'''    stats_html = (
        f"<b>{stats['top']}</b> top picks + <b>{stats['strong']}</b> strong "
        f"= <b>{stats.get('shortlisted', 0)}</b> photographs &middot; "
        f"selected from {stats['total']} scored across "
        f"{stats['folders']} folders &middot; "
        f"<b>{stats['extracted']}</b> stills extracted from video"
    )''')

rep("<title>Photo Scout - photograph quality report</title>",
    "<title>Photo Scout - shortlist (top picks &amp; strong)</title>")
rep('<h1>Photo Scout &mdash; photograph quality</h1>',
    '<h1>Photo Scout &mdash; shortlist <span style="font-weight:400;color:#9a9a9a">'
    '(top picks &amp; strong only)</span></h1>')

# Only the Maybe and Pass buttons go. "All" stays and remains the default, so
# this report keeps the original exclusive-filter behaviour - All shows both
# bands, the other two narrow to one. Because the JS is untouched, this variant
# stays as close to photo_scout.py as possible, which is the whole point: less
# divergence means less to keep in sync.
rep('''    <button data-f="all" class="on">All</button>
    <button data-f="TOP PICK">Top picks</button>
    <button data-f="STRONG">Strong</button>
    <button data-f="MAYBE">Maybe</button>
    <button data-f="PASS">Pass</button>''',
'''    <button data-f="all" class="on">All</button>
    <button data-f="TOP PICK">Top picks</button>
    <button data-f="STRONG">Strong</button>''')

assert s != src
(H / "photo_scout_strong_top.py").write_text(s, encoding="utf-8")
print("regenerated photo_scout_strong_top.py")
