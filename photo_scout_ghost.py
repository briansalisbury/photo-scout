#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brian Salisbury and contributors.
# Part of Photo Scout. This program comes with ABSOLUTELY NO WARRANTY.
# See the LICENSE file, or <https://www.gnu.org/licenses/>.
"""
photo_scout_ghost.py - publish the Photo Scout shortlist to a Ghost site.

Uploads the shortlisted thumbnails and previews into Ghost's own image storage,
then creates or updates a single Ghost page containing the gallery. Photographs
end up served from your domain by Ghost, with no external image host.

    python photo_scout_ghost.py --site https://example.com --dry-run
    python photo_scout_ghost.py --site https://example.com --key ID:SECRET

The key comes from Ghost admin: Settings -> Advanced -> Integrations -> Add custom
integration, then the ADMIN API key (not the Content API key, which is read-only
and cannot upload). It is 89 characters shaped '<24-hex id>:<64-hex secret>'. Set
it as GHOST_ADMIN_KEY instead of passing --key to keep it out of shell history:

    bash/zsh    export GHOST_ADMIN_KEY='<id>:<secret>'
    PowerShell  $env:GHOST_ADMIN_KEY = "<id>:<secret>"

'export' does not exist in PowerShell, and either form lasts only for that
terminal session. README section 11 covers making it permanent.

Reads scores.sqlite3 READ-ONLY. Never modifies the photo library, and never
modifies Ghost's own database - everything goes through the documented Admin API.


THE PATH PROBLEM, AND HOW THIS SOLVES IT
========================================

The local report addresses images as 'thumbs/ab12cd.jpg' and links to originals
as 'file:///D:/Photos/...' or 'file:///home/you/photos/...'. Neither survives
publication:

  * file:// links are meaningless in a visitor's browser AND leak your directory
    layout. They are removed entirely, not rewritten.
  * relative thumb paths point at a folder that only exists on your machine.
  * Ghost decides where an uploaded image lives - the URL embeds the upload
    year and month, e.g. /content/images/2026/08/foo.jpg - so the destination
    cannot be computed in advance. It must be captured from the upload response.

Four rules make the conversion reliable.

1. STABLE PUBLIC IDENTITY
   photo_id = sha256(relative_path_with_forward_slashes)[:16]

   Derived from the path RELATIVE to the library root, so it is identical
   wherever the library happens to sit, and it never exposes
   your filesystem. Same identifier the heart service uses.

2. DETERMINISTIC, COLLISION-PROOF FILENAMES
   psc-<photo_id>-t.jpg  (thumbnail)
   psc-<photo_id>-p.jpg  (preview)

   This matters more than it looks. The library contains DSC_0000.JPG in many
   different folders. Uploading those under their own names would make Ghost
   silently append -1, -2, -3 to deduplicate, and nothing in the response would
   say which folder each came from. Hashed names cannot collide, are computable
   before upload, and give the manifest a reliable key.

3. AN UPLOAD MANIFEST, SO RE-PUBLISHING IS FREE
   publish.sqlite3 records photo_id + kind -> the URL Ghost returned, alongside
   a SHA-256 of the bytes actually uploaded. A second run uploads nothing. If a
   preview is regenerated its hash changes and only that file is re-uploaded.

   Without this, every publish would duplicate every image in Ghost's storage.

   The manifest is deliberately a SEPARATE database from scores.sqlite3, which
   photo_scout.py --reset deletes. Uploads must outlive a rescore.

4. ROOT-RELATIVE URLS IN THE PAGE
   Ghost returns absolute URLs (https://example.com/content/images/...). The
   manifest stores only the path portion, and the page emits /content/images/...
   Same-origin, so it keeps working if the domain ever changes, if the site is
   reached over http during testing, or behind a different hostname.

Responsive sizes (/content/images/size/w600/...) are deliberately NOT used:
Ghost only serves widths declared in the active theme's package.json image_sizes,
and an undeclared width 404s. Uploading thumbnail and preview separately works on
any theme with no theme changes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Reuse the scoring project's helpers rather than duplicating them.
import photo_scout as ps  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The upload manifest lives beside THIS SCRIPT, not in photo_scout's output
# directory. That directory is deleted wholesale by `photo_scout.py --reset`,
# and losing the manifest would orphan every image already in Ghost and
# re-upload the entire gallery as duplicates.
PUBLISH_DB = "publish.sqlite3"
DEFAULT_SLUG = "photo-scout"
# Blank by default: the gallery is the page, and a heading above it is usually
# just another thing to scroll past. A blank title also hides the theme's whole
# heading band - see resolve_title().
DEFAULT_TITLE = ""

# What Ghost files an untitled page under. Nothing displays it - the heading is
# hidden - but the admin list needs a name, and an empty one is not reliably
# accepted by the API.
UNTITLED_GHOST_TITLE = "Photo Scout Gallery"
UPLOAD_PREFIX = "psc"               # marks our uploads in Ghost's media library

# Only these verdicts are published.
PUBLISH_VERDICTS = ("TOP PICK", "STRONG")

JWT_TTL_SECONDS = 300               # Ghost rejects anything longer than 5 minutes

# Python's urllib announces itself as "Python-urllib/3.x", which Cloudflare's
# Browser Integrity Check rejects outright with error 1010 - the request never
# reaches Ghost. Identifying the tool properly is enough in most configurations.
USER_AGENT = "photo-scout-ghost/1.0 (+https://github.com/ - Ghost Admin API client)"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def photo_id_for(rel_path: str) -> str:
    """
    Stable 16-hex-character public identifier for a photograph.

    Normalised to forward slashes and lower case so a Windows-scored library and
    a Linux-scored one produce identical ids for the same photograph.
    """
    norm = rel_path.replace("\\", "/").strip("/").lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def upload_filename(photo_id: str, kind: str) -> str:
    """kind is 'thumb' or 'preview'. Result is safe for any filesystem or CDN."""
    suffix = {"thumb": "t", "preview": "p"}[kind]
    return f"{UPLOAD_PREFIX}-{photo_id}-{suffix}.jpg"


def to_site_relative(url: str) -> str:
    """
    Reduce a Ghost absolute URL to its site-relative path.

    Storing '/content/images/2026/08/psc-abc-p.jpg' rather than the full URL
    means the published page keeps working if the site later moves domain, is
    served over plain http in testing, or sits behind a different hostname.
    """
    parsed = urllib.parse.urlparse(url)
    return parsed.path if parsed.path else url


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Ghost Admin API
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def ghost_jwt(admin_key: str) -> str:
    """
    Build a Ghost Admin API token.

    Ghost's scheme: split the key on ':' into id and hex secret, sign an HS256
    JWT whose header carries kid=id, with claims iat/exp/aud='/admin/'. The
    secret is hex and must be decoded to bytes before signing.

    Implemented with the standard library so this script needs no pyjwt.
    """
    if ":" not in admin_key:
        raise ValueError("Admin API key must look like <id>:<hex secret>")
    key_id, secret_hex = admin_key.split(":", 1)
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise ValueError(f"The secret half of the Admin API key is not hex: {exc}") from None

    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": now, "exp": now + JWT_TTL_SECONDS, "aud": "/admin/"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "." +
        _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return signing_input.decode("ascii") + "." + _b64url(signature)


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def plaintext_endpoint(url: str) -> bool:
    """
    True when this URL would put a credential on the wire in the clear.

    Loopback is exempt: a tunnel to a Ghost on your own machine is a normal way
    to work, and nothing leaves the host. Everything else over http:// means the
    Admin API key, or the heart admin token, is readable by anything between
    here and the server - and an Admin key is full write access to the site.
    """
    parsed = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if parsed.scheme != "http":
        return False
    return (parsed.hostname or "").lower() not in LOOPBACK_HOSTS


class GhostError(RuntimeError):
    pass


def _edge_block_hint(code: int, body: str, admin_url: str) -> str:
    """
    Explain the errors that come from a CDN in front of Ghost rather than from
    Ghost itself. These are opaque otherwise - a bare 'error code: 1010' says
    nothing about what to change.
    """
    low = body.lower()
    if "1010" in body or "browser signature" in low:
        return (
            "\n\n  This is Cloudflare, not Ghost - the request never reached your"
            "\n  site. Error 1010 is the Browser Integrity Check rejecting the"
            "\n  client's signature."
            "\n"
            "\n  This script now sends a proper User-Agent, which usually clears it."
            "\n  If you are still seeing 1010, allow the Admin API past the check:"
            "\n"
            "\n    Cloudflare dashboard -> your domain -> Security -> WAF"
            "\n    -> Custom rules -> Create rule"
            "\n      Field: URI Path   Operator: starts with   Value: /ghost/api/"
            "\n      Action: Skip -> Browser Integrity Check (and Bot Fight Mode)"
            "\n"
            "\n  Narrower and safer than disabling the check site-wide, since the"
            "\n  Admin API is already protected by your key."
            "\n"
            "\n  Alternatively point --admin-url at the origin server directly so"
            "\n  the upload bypasses the CDN entirely."
        )
    if code == 403 and ("cloudflare" in low or "cf-ray" in low):
        return ("\n\n  This looks like a Cloudflare block rather than Ghost. Check"
                "\n  Security -> Events in the Cloudflare dashboard to see which"
                "\n  rule fired, then add a Skip rule for /ghost/api/.")
    if code == 413:
        return ("\n\n  The image was rejected as too large. Lower PREVIEW_SIZE in"
                "\n  photo_scout.py and regenerate, or raise the upload limit in"
                "\n  Ghost and any proxy in front of it (nginx client_max_body_size).")
    if code == 401:
        return ("\n\n  Authentication failed. Confirm you copied the ADMIN API key"
                "\n  (it contains a colon), not the Content API key, and that"
                f"\n  {admin_url} is the API URL shown on the integration page.")
    return ""


class GhostClient:
    """
    Talks to the Ghost Admin API.

    `admin_url` may differ from the public site: Ghost can be configured with a
    separate admin hostname (admin.example.com) while the front end, and every
    image URL it serves, stays on the main domain. Only API calls go to the
    admin host - image paths are stored site-relative, so they resolve correctly
    on the public site regardless of which host returned them.
    """

    def __init__(self, admin_url: str, admin_key: str, timeout: int = 120,
                 user_agent: str = USER_AGENT):
        self.admin_url = admin_url.rstrip("/")
        self.admin_key = admin_key
        self.timeout = timeout
        self.user_agent = user_agent
        self.api = f"{self.admin_url}/ghost/api/admin"

    # -- plumbing ----------------------------------------------------------
    def _auth_header(self) -> str:
        # Minted per request: the token is only valid for five minutes and an
        # upload run can easily outlast that.
        return "Ghost " + ghost_jwt(self.admin_key)

    def _request(self, method: str, path: str, *, data: bytes = None,
                 content_type: str = None, allow_404: bool = False) -> dict:
        url = f"{self.api}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth_header())
        req.add_header("Accept-Version", "v5.0")
        req.add_header("User-Agent", self.user_agent)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_404:
                return {}          # a legitimate "not there yet", not a failure
            detail = exc.read().decode("utf-8", "replace")
            hint = _edge_block_hint(exc.code, detail, self.admin_url)
            raise GhostError(
                f"{method} {path} -> HTTP {exc.code}\n{detail[:400]}" + hint) from None
        except urllib.error.URLError as exc:
            raise GhostError(
                f"{method} {path} -> could not reach {self.admin_url}: {exc.reason}") from None
        return json.loads(body) if body else {}

    # -- images ------------------------------------------------------------
    def upload_image(self, local: Path, filename: str, ref: str) -> str:
        """Upload one image, returning the absolute URL Ghost assigned it."""
        # A multipart boundary only has to be unique, so the digest is a
        # formatting device; the randomness comes from os.urandom.
        boundary = "----psc" + hashlib.sha1(
            os.urandom(16), usedforsecurity=False).hexdigest()
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"

        parts: list[bytes] = []

        def field(name: str, value: str):
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        )
        parts.append(local.read_bytes())
        parts.append(b"\r\n")
        field("purpose", "image")
        # ref comes back in the response; keep it to the opaque id so no folder
        # names are handed to Ghost.
        field("ref", ref)
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))

        payload = b"".join(parts)
        out = self._request("POST", "/images/upload/", data=payload,
                            content_type=f"multipart/form-data; boundary={boundary}")
        try:
            return out["images"][0]["url"]
        except (KeyError, IndexError):
            raise GhostError(f"Unexpected upload response: {json.dumps(out)[:300]}") from None

    # -- pages -------------------------------------------------------------
    def find_page(self, slug: str) -> Optional[dict]:
        """
        Look up the gallery page, or None if it has not been created yet.

        Ghost answers 404 for an unknown slug, which is the expected state on the
        very first publish - so it must not be treated as an error.
        """
        out = self._request("GET", f"/pages/slug/{urllib.parse.quote(slug)}/?formats=lexical",
                            allow_404=True)
        pages = out.get("pages") or []
        return pages[0] if pages else None

    def create_page(self, slug: str, title: str, lexical: str, status: str) -> dict:
        body = json.dumps({"pages": [{
            "slug": slug, "title": title, "lexical": lexical, "status": status,
        }]}).encode("utf-8")
        # No ?source= parameter. Lexical is Ghost's native storage format, so it
        # is sent as-is; `source` exists only to request conversion FROM html,
        # and passing source=lexical is rejected with a 422 AllowedValues error.
        out = self._request("POST", "/pages/", data=body,
                            content_type="application/json")
        return out["pages"][0]

    def update_page(self, page: dict, title: str, lexical: str, status: str) -> dict:
        # updated_at is Ghost's optimistic-concurrency check; without it the
        # edit is rejected.
        body = json.dumps({"pages": [{
            "id": page["id"], "updated_at": page["updated_at"],
            "slug": page["slug"], "title": title, "lexical": lexical, "status": status,
        }]}).encode("utf-8")
        out = self._request("PUT", f"/pages/{page['id']}/", data=body,
                            content_type="application/json")
        return out["pages"][0]


# ---------------------------------------------------------------------------
# Upload manifest
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    photo_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'thumb' | 'preview'
    local_sha   TEXT NOT NULL,          -- sha256 of the bytes uploaded
    url_path    TEXT NOT NULL,          -- site-relative, e.g. /content/images/...
    filename    TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL,
    PRIMARY KEY (photo_id, kind)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Manifest:
    """
    Remembers what has already been uploaded, so publishing is idempotent.

    Kept apart from scores.sqlite3 on purpose: that database is destroyed by
    photo_scout.py --reset, and losing this one would mean re-uploading the
    entire gallery and leaving orphaned duplicates in Ghost's storage.
    """

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(MANIFEST_SCHEMA)
        self.conn.commit()

    def get(self, photo_id: str, kind: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM uploads WHERE photo_id = ? AND kind = ?", (photo_id, kind)
        ).fetchone()

    def put(self, photo_id: str, kind: str, local_sha: str, url_path: str, filename: str):
        self.conn.execute(
            """INSERT INTO uploads (photo_id, kind, local_sha, url_path, filename, uploaded_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(photo_id, kind) DO UPDATE SET
                 local_sha=excluded.local_sha, url_path=excluded.url_path,
                 filename=excluded.filename, uploaded_at=excluded.uploaded_at""",
            (photo_id, kind, local_sha, url_path, filename, int(time.time())),
        )

    def commit(self):
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]


# ---------------------------------------------------------------------------
# Selecting what to publish
# ---------------------------------------------------------------------------

def load_shortlist(scores_db: Path, out_dir: Path, root: Path) -> list[dict]:
    """
    Read the shortlist out of scores.sqlite3 (read-only) and pair each row with
    its local thumbnail and preview files.
    """
    uri = f"file:{urllib.parse.quote(str(scores_db))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # One placeholder per verdict. The only thing interpolated is the count
    # of question marks; the values themselves are bound.
    placeholders = ",".join("?" for _ in PUBLISH_VERDICTS)
    sql = ("SELECT * FROM photos WHERE error IS NULL AND dup_of IS NULL "  # nosec B608
           f"AND verdict IN ({placeholders}) ORDER BY composite DESC")
    rows = conn.execute(sql, PUBLISH_VERDICTS).fetchall()
    conn.close()

    items = []
    for r in rows:
        rel = r["rel_path"] or r["filename"]
        # Hidden or gone from disk: same rule the local report applies, so the
        # published page can never show something the report does not.
        if ps.is_hidden(rel) or ps.is_hidden(r["path"]):
            continue
        # Below the size floor: same rule again. A row with no recorded
        # dimensions predates those columns and counts as unknown, which is
        # never grounds for dropping it.
        try:
            w, h = r["width"], r["height"]
        except (IndexError, KeyError):
            w = h = None
        if w and h and ps.below_size_floor((w, h), ps.MIN_IMAGE_EDGE):
            continue
        real_src = r["source_video"] or ps.split_virtual_path(r["path"])[0]
        try:
            if root.exists() and not Path(real_src).exists():
                continue
        except OSError:
            pass
        pid = photo_id_for(rel)
        # photo_scout names previews with the same hash as thumbnails; only the
        # directory differs.
        stem = ps.thumb_name(r["path"])
        thumb = out_dir / "thumbs" / stem
        preview = out_dir / "previews" / stem
        items.append({
            "photo_id": pid,
            # Relative to out_dir, so a locally-emitted preview page can point at
            # the real files on disk instead of at URLs that don't exist yet.
            "thumb_rel": f"thumbs/{stem}" if thumb.exists() else None,
            "preview_rel": f"previews/{stem}" if preview.exists() else None,
            "rel_path": rel,
            "filename": r["filename"],
            "folder": r["folder"] or "",
            # A database written before capture dates existed simply has no
            # column; the page then renders without dates rather than failing.
            "taken_at": (r["taken_at"] if "taken_at" in r.keys() else None) or "",
            # Pixel dimensions, shown because the score deliberately ignores
            # them. Empty for a row written before those columns existed.
            "resolution": ps.pretty_resolution(w, h),
            "verdict": r["verdict"],
            "score": round(r["composite"] or 0.0, 1),
            "note": r["note"] or "",
            "thumb_local": thumb if thumb.exists() else None,
            "preview_local": preview if preview.exists() else None,
        })
    return items


def load_tags(out_dir: Path) -> dict:
    """Tags are browser-side, but a tags.json export can be baked in read-only."""
    path = out_dir / "tags.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # Keys in tags.json are absolute local paths; re-key them by photo_id.
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------

def publish_images(client: Optional[GhostClient], manifest: Manifest,
                   items: list[dict], dry_run: bool) -> dict:
    stats = {"uploaded": 0, "reused": 0, "missing": 0, "bytes": 0, "would_upload": 0}
    for item in items:
        for kind, key in (("thumb", "thumb_local"), ("preview", "preview_local")):
            local = item[key]
            if local is None:
                item[f"{kind}_url"] = None
                stats["missing"] += 1
                continue

            digest = sha256_file(local)
            have = manifest.get(item["photo_id"], kind)
            if have and have["local_sha"] == digest:
                item[f"{kind}_url"] = have["url_path"]
                stats["reused"] += 1
                continue

            name = upload_filename(item["photo_id"], kind)
            if dry_run:
                # No URL is invented. The local preview page renders from the
                # files on disk, and the published markup is only ever built
                # after a real upload has returned a real path.
                item[f"{kind}_url"] = None
                stats["would_upload"] += 1
                stats["bytes"] += local.stat().st_size
                continue

            url = client.upload_image(local, name, item["photo_id"])
            path = to_site_relative(url)
            manifest.put(item["photo_id"], kind, digest, path, name)
            manifest.commit()
            item[f"{kind}_url"] = path
            stats["uploaded"] += 1
            stats["bytes"] += local.stat().st_size
            ps.log(f"  uploaded {name}  -> {path}")
    return stats


# ---------------------------------------------------------------------------
# The gallery markup
# ---------------------------------------------------------------------------

# CSS for the host page's own title block, chosen by --title-size. A Ghost theme
# usually gives that block a viewport-scaled band - the default is
# max(12vmin, 64px) above the title - which dwarfs a gallery embedded below it.
#
# The rules are scoped by a .psc-host class the gallery's own script puts on
# <html>, so only a page actually carrying a gallery is affected, and the
# selector works in browsers without :has(). The class names cover the headers
# Ghost's own themes have shipped over the years; an unrecognised theme keeps
# its spacing and nothing breaks.
_HOST_HEADER = ":is(.article-header,.gh-article-header,.post-full-header,.gh-canvas>.post-full-header)"
_HOST_TITLE = ":is(.article-title,.gh-article-title,.post-full-title,.page-title)"

# The folder tiles' colour scheme lives in photo_scout, so the published
# page and the local report cannot drift apart on it.
DEFAULT_FOLDER_OUTLINE = ps.DEFAULT_FOLDER_OUTLINE
OUTLINE_MIN_CONTRAST = ps.OUTLINE_MIN_CONTRAST
TILE_BG = ps.TILE_BG
contrast = ps.contrast
folder_palette = ps.folder_palette
_hex_rgb = ps._hex_rgb
_rgb_hex = ps._rgb_hex
_relative_luminance = ps._relative_luminance


TITLE_SIZE_CSS = {
    "keep": "",
    "compact": (
        f".psc-host {_HOST_HEADER}{{padding-top:26px!important;"
        f"padding-bottom:10px!important}}\n"
        f".psc-host {_HOST_TITLE}{{font-size:clamp(20px,2.6vw,30px)!important;"
        f"margin:0!important;line-height:1.2!important}}"
    ),
    "hide": f".psc-host {_HOST_HEADER}{{display:none!important}}",
}

GALLERY_CSS = """
<style>
.psc-wrap{--psc-bg:#111;--psc-fg:#e8e8e8;--psc-mut:#9a9a9a;--psc-card:#1b1b1b;--psc-line:#2c2c2c;
  --psc-colw:__COLW__px}
.psc-wrap{background:var(--psc-bg);color:var(--psc-fg);padding:10px 16px;border-radius:12px;
  font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;box-sizing:border-box}
/* Themes space the children of their content area generously - Ghost's own
   default is max(12vmin,64px), which is a visible hole under a full-width embed
   on a tall screen. !important because those rules are usually more specific
   than anything addressable from here, and the theme's selector is not knowable
   in advance. Only this element is affected. Set with --gap; a negative value
   tucks the gallery up under the page title. */
.psc-wrap.psc-wrap{margin-top:__GAPPX__!important;margin-bottom:__GAPPX__!important}
/* Ghost themes constrain page content to a narrow reading column - typically
   around 720px - which would pin this grid at two columns on any monitor. Break
   out of that column and size against the viewport instead.
   96vw rather than 100vw: 100vw includes the vertical scrollbar, which would
   push the page into horizontal overflow. */
/* Centred with a negative margin, deliberately NOT with translateX(-50%).
   Any ancestor carrying a transform (or filter, perspective, will-change,
   contain) becomes the containing block for position:fixed descendants - which
   would make the lightbox position against this div instead of the viewport,
   so it would not cover the window and would scroll with the page. */
.psc-wrap.psc-bleed{width:min(96vw,__MAXW__px);
  margin-left:calc((100% - min(96vw,__MAXW__px)) / 2)}
@media (max-width:600px){.psc-wrap.psc-bleed{width:100%;margin-left:0;
  border-radius:0;padding:8px 10px}}
/* Sticks to the top so the filters and the search box stay reachable a
   thousand photographs down. The negative margins let its background run to
   the edge of the gallery, so cards scroll under it rather than beside it.
   z-index stays below the lightbox, which is 100 and re-parented to <body>. */
.psc-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  position:sticky;top:0;z-index:5;background:var(--psc-bg);
  margin:0 -16px 14px;padding:10px 16px;border-bottom:1px solid var(--psc-line)}
.psc-bar button,.psc-bar select,.psc-bar input{background:#242424;color:var(--psc-fg);
  border:1px solid #3a3a3a;border-radius:6px;padding:6px 11px;font-size:13px;cursor:pointer}
.psc-bar button.on{background:#2f6f4f;border-color:#3f8f68}
.psc-bar input{flex:1;min-width:180px;cursor:text}
.psc-count{color:var(--psc-mut);font-size:12.5px;margin-left:auto}
/* Thumbnail size. A touchscreen has no ctrl+scroll, and pinching zooms the
   whole page rather than reflowing the grid, so the columns get their own
   control. 34px square clears the 24px minimum for a comfortable tap. */
.psc-zoom{display:inline-flex;gap:4px}
.psc-zoom button{min-width:34px;padding:6px 9px;font-size:15px;line-height:1;
  font-variant-numeric:tabular-nums}
.psc-zoom button[disabled]{opacity:.35;cursor:default}
.psc-foot{color:#777;font-size:12px;text-align:center;padding:18px 0 4px}
.psc-foot a{color:#9a9a9a;text-decoration:none;border-bottom:1px solid #3a3a3a}
.psc-foot a:hover{color:var(--psc-fg);border-bottom-color:#6a6a6a}
__HEADCSS__
/* min(100%,Npx) stops a single column from overflowing a container narrower
   than the track minimum, which is what produces sideways scroll on phones. */
.psc-grid{display:grid;gap:14px;
  grid-template-columns:repeat(auto-fill,minmax(min(100%,var(--psc-colw)),1fr))}
.psc-card{background:var(--psc-card);border:1px solid var(--psc-line);border-radius:10px;overflow:hidden}
.psc-card img{width:100%;aspect-ratio:3/2;object-fit:cover;background:#000;display:block;cursor:zoom-in}
.psc-body{padding:9px 11px 11px}
.psc-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.psc-badge{font-size:10px;letter-spacing:.06em;padding:2px 7px;border-radius:99px;font-weight:700}
.psc-TOPPICK{background:#1e5f3f;color:#8ff0bd}
.psc-STRONG{background:#1f4a63;color:#9fd8f5}
.psc-name{font-weight:600;font-size:12.5px;word-break:break-all;margin-top:4px}
/* Two muted lines, split by how long each can get. .psc-meta holds the folder:
   one free-text field of unbounded length, so it takes the whole line and
   truncates, with the full name on its title attribute. .psc-specs holds facts
   of predictable width - date, time, dimensions, megapixels - and WRAPS instead
   of clipping, so none of them can be cut off. They shared a line until a long
   folder name was found pushing the date and resolution out of sight. */
.psc-meta{color:var(--psc-mut);font-size:11.5px;margin-top:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.psc-specs{color:#6f6f6f;font-size:11px;margin-top:2px;line-height:1.45;
  font-variant-numeric:tabular-nums}
/* Each fact is atomic: a break may fall between the date and the time, or
   between the dimensions and the megapixel figure, but never through the middle
   of any one of them. A date split over two lines reads as a typo. */
.psc-specs span{white-space:nowrap}
.psc-note{color:#b6b6b6;font-size:12px;margin-top:5px}
.psc-flag{color:#d9a441}
.psc-tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;align-items:center}
.psc-tag{font-size:11px;padding:3px 7px;border-radius:99px;display:inline-flex;
  align-items:center;gap:5px;line-height:1.35;
  background:var(--tb);color:var(--tf);border:1px solid var(--td)}
.psc-tag button{background:none;border:none;color:inherit;cursor:pointer;
  font-size:13px;line-height:1;padding:0;opacity:.65}
.psc-tag button:hover{opacity:1}
.psc-tagwrap{margin-top:8px;padding-top:8px;border-top:1px solid #2a2a2a}
.psc-taginput{flex:1;min-width:96px;background:#141414;border:1px dashed #3a3a3a;
  color:var(--psc-fg);border-radius:6px;padding:4px 7px;font-size:11.5px;
  font-family:inherit}
.psc-taginput:focus{outline:none;border-color:#5a5a5a;border-style:solid}
/* The search box grows to hold chips, so it is a container rather than a bare
   input, and it anchors the dropdown. */
.psc-searchwrap{position:relative;flex:1;min-width:220px;display:flex;
  flex-wrap:wrap;gap:5px;align-items:center;background:#242424;
  border:1px solid #3a3a3a;border-radius:6px;padding:4px 7px}
.psc-searchwrap.focus{border-color:#5a7f9a}
.psc-bar .psc-q{flex:1;min-width:110px;background:transparent;border:none;
  padding:2px;font-size:13px;color:var(--psc-fg)}
.psc-bar .psc-q:focus{outline:none}
.psc-tagmenu{position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:30;
  background:#1d1d1d;border:1px solid #3a3a3a;border-radius:8px;
  max-height:260px;overflow:auto;display:none;
  box-shadow:0 10px 30px rgba(0,0,0,.6);text-align:left}
.psc-tagmenu.open{display:block}
.psc-tagmenu > div{padding:7px 10px;cursor:pointer;font-size:13px;
  display:flex;align-items:center;gap:8px}
.psc-tagmenu > div.sel,.psc-tagmenu > div:hover{background:#2c2c2c}
.psc-tagmenu .psc-cnt{margin-left:auto;color:var(--psc-mut);font-size:11.5px}
.psc-tagmenu .psc-delall{background:none;border:1px solid #4a3a3a;color:#c98a8a;
  border-radius:6px;font-size:11px;line-height:1;padding:4px 7px;cursor:pointer;
  margin-left:6px;flex:0 0 auto}
.psc-tagmenu .psc-delall:hover{background:#3a2626;border-color:#7a4a4a;color:#f0b4b4}
.psc-tagmenu .psc-none{color:var(--psc-mut);cursor:default}
.psc-tagmenu .psc-none:hover{background:transparent}
.psc-toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
  background:#252525;border:1px solid #3d3d3d;color:#eee;padding:10px 16px;
  border-radius:8px;font-size:13px;opacity:0;pointer-events:none;
  transition:opacity .18s;max-width:78vw;z-index:2147483100}
.psc-toast.show{opacity:1}
/* The bar itself stays click-through; only an action button inside is clickable. */
.psc-toast button{pointer-events:auto;margin-left:12px;background:#333;
  border:1px solid #555;color:#eee;border-radius:6px;padding:3px 9px;
  font-size:12px;cursor:pointer}
.psc-hearts{display:flex;align-items:center;gap:6px;margin-top:8px}
.psc-heart{background:none;border:none;cursor:pointer;font-size:17px;line-height:1;
  padding:2px 4px;filter:grayscale(1) opacity(.45);transition:filter .12s,transform .1s}
.psc-heart:hover{transform:scale(1.15)}
.psc-heart.on{filter:none}
.psc-heart[disabled]{cursor:default;opacity:.3}
.psc-hcount{color:var(--psc-mut);font-size:12px;font-variant-numeric:tabular-nums;
  min-width:1ch}
/* Until the first read from the service lands, the row is not shown at all -
   better nothing than a row of zeros that might be wrong. */
.psc-hearts.psc-pending{visibility:hidden}
/* display:none rather than visibility:hidden - a bar button must not leave a
   gap in the row while the counts are still on their way. */
.psc-bar .psc-likedonly.psc-pending{display:none}
.psc-hidden{display:none!important}
.psc-lb{position:fixed;inset:0;width:100%;height:100vh;height:100dvh;
  overscroll-behavior:contain;touch-action:none;
  background:rgba(0,0,0,.94);z-index:2147483000;display:none;
  align-items:center;justify-content:center}
.psc-lb.open{display:flex}
/* The overlay and the toast are moved out to <body>, beyond the gallery, so
   they would otherwise take the theme's font - a serif, on many themes. They
   get the gallery's own instead; buttons do not inherit a font by default. */
.psc-lb{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.psc-toast{font-family:-apple-system,Segoe UI,Roboto,sans-serif}
.psc-lb button,.psc-toast button{font-family:inherit}
.psc-lb img{max-width:96vw;max-height:92vh;max-height:92dvh;object-fit:contain}
.psc-lb .x{position:absolute;top:14px;right:20px;color:#ddd;font-size:30px;cursor:pointer;
  background:none;border:none;z-index:2}
.psc-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.45);
  border:1px solid rgba(255,255,255,.18);color:#eee;font-size:26px;line-height:1;
  width:52px;height:76px;border-radius:10px;cursor:pointer;z-index:2;
  display:flex;align-items:center;justify-content:center}
.psc-nav:hover{background:rgba(0,0,0,.75)}
.psc-prev{left:14px} .psc-next{right:14px}
.psc-count-lb{position:absolute;bottom:16px;left:50%;transform:translateX(-50%);
  color:#bbb;font-size:12.5px;background:rgba(0,0,0,.45);padding:4px 12px;border-radius:99px;
  font-variant-numeric:tabular-nums;z-index:2}
.psc-cap{position:absolute;top:16px;left:20px;color:#ccc;font-size:12.5px;
  background:rgba(0,0,0,.45);padding:4px 12px;border-radius:99px;z-index:2;
  max-width:60vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Its own pill under the caption rather than appended to it: a long file name
   would otherwise run into the ellipsis and take the resolution with it, and
   this is the view where knowing the pixel dimensions actually matters. */
.psc-dims-lb{position:absolute;top:45px;left:20px;color:#9a9a9a;font-size:11.5px;
  background:rgba(0,0,0,.45);padding:3px 12px;border-radius:99px;z-index:2;
  white-space:nowrap;font-variant-numeric:tabular-nums}
.psc-dims-lb:empty{display:none}
/* Bottom right, clear of the heart on the left and the count in the middle,
   and dressed like both of them. */
.psc-link-lb{position:absolute;bottom:14px;right:20px;z-index:2;color:#ddd;
  background:rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.18);
  border-radius:99px;padding:5px 14px;font-size:12.5px;cursor:pointer}
.psc-link-lb:hover{background:rgba(0,0,0,.75)}
.psc-hearts-lb{position:absolute;bottom:14px;left:20px;z-index:2;margin:0;
  background:rgba(0,0,0,.45);padding:5px 14px;border-radius:99px}
.psc-hearts-lb .psc-heart{font-size:20px}
.psc-hearts-lb .psc-hcount{color:#ddd;font-size:13px;
  font-variant-numeric:tabular-nums}
@media (max-width:600px){.psc-nav{width:44px;height:64px;font-size:22px}
  .psc-prev{left:6px} .psc-next{right:6px} .psc-cap{max-width:50vw}}

/* iOS Safari zooms the entire page when a text field smaller than 16px takes
   focus, and there is no way to ask it not to without disabling pinch for the
   whole site. Sizing the field up is the only fix that does not cost the
   visitor something. Touch pointers only, so the desktop bar stays compact. */
@media (pointer:coarse){
  .psc-bar input,.psc-bar select,.psc-bar .psc-q{font-size:16px}
}

/* ---- folder galleries --------------------------------------------------
   Two buttons welded into one control, so they read as a pair of states rather
   than two unrelated actions. The active one picks up .on from the bar's own
   rule, which is more specific than anything here and so stays in charge of
   the colour. */
.psc-views{display:inline-flex}
.psc-views button{border-radius:0;margin:0}
.psc-views button:first-child{border-radius:6px 0 0 6px}
/* The two share an edge by overlapping it rather than dropping one border, so
   whichever is active can sit on top and show its outline all the way round. */
.psc-views button:last-child{border-radius:0 6px 6px 0;margin-left:-1px}
.psc-views button.on{position:relative;z-index:1}

/* Each group of toggles has its own colour when active, so a glance tells you
   which view you are in, which rating band is on, and whether Liked is
   filtering - rather than one green for all three questions. Manila for the
   view, matching the folder tiles; the familiar green for the bands; rose for
   Liked, after the heart. Text on each clears 7:1, or 4.8:1 for the green. */
.psc-bar .psc-views button.on{background:#54452a;border-color:#b09468;color:#f5e9d0}
.psc-bar .psc-likedonly.on{background:#732c40;border-color:#c2607a;color:#ffe0e7}

/* The index shares the photo grid's column track, so a tile is exactly as wide
   as a card and the two views line up when you switch between them. */
/* Only the outline is coloured, and it comes from --folder-outline via
   folder_palette(). The card behind it is the page's own, so the tiles sit in
   the same family as every other panel and the text on them keeps the colours
   it has everywhere else. Change the flag, not this. */
.psc-folders{display:none;grid-gap:14px;gap:14px;--psc-tab:13px;
  --psc-fold-line:__FOLDLINE__;--psc-fold-line-hover:__FOLDLINEHOVER__;
  /* The tab hangs above each tile, so the rows need that much more between
     them or the tab of one row overlaps the tile above it. */
  row-gap:calc(14px + var(--psc-tab));
  grid-template-columns:repeat(auto-fill,minmax(min(100%,var(--psc-colw)),1fr))}
.psc-view-folders .psc-folders{display:grid}
.psc-view-folders .psc-grid{display:none}
/* Inside a folder the index goes away and the ordinary grid comes back. */
.psc-view-folders.psc-open .psc-folders{display:none}
.psc-view-folders.psc-open .psc-grid{display:grid}

/* Shaped like a folder: a tab up on the left, then the body. The tab is a
   pseudo-element rather than markup, so the tile stays one button with one
   label and nothing extra for a screen reader to read out.

   flex-column rather than block: a button centres its contents vertically by
   default, which leaves a gap above the mosaic on any tile shorter than its
   row and stops the covers lining up across the grid. */
.psc-fold{background:var(--psc-card);border:1px solid var(--psc-fold-line);
  border-radius:0 10px 10px 10px;cursor:pointer;text-align:left;
  padding:0;color:inherit;font:inherit;width:100%;position:relative;
  margin-top:var(--psc-tab);
  display:flex;flex-direction:column;align-items:stretch}
/* The tab ends exactly where the body's top edge does, covering that edge so
   the two read as one piece of card. border-box, so the height means the same
   thing whatever box-sizing rule the theme applies to everything: any taller
   and the tab's side lines run on into the body. */
.psc-fold::before{content:'';position:absolute;left:-1px;top:calc(-1px - var(--psc-tab));
  width:46%;max-width:150px;box-sizing:border-box;height:calc(var(--psc-tab) + 1px);
  background:var(--psc-card);border:1px solid var(--psc-fold-line);
  border-bottom:none;border-radius:8px 8px 0 0;
  /* One more pixel of card below the tab, inset from its side lines. At a zoom
     that is not a whole number the tab's edge rounds to a pixel boundary and
     can stop a fraction short of covering the line beneath it; this covers
     that fraction without lengthening the tab's own sides. */
  box-shadow:0 2px 0 -1px var(--psc-card)}
/* Inset, so a margin of the folder's own card shows around the photographs and
   they read as prints sitting inside it rather than as the folder itself. The
   overflow has to live here rather than on the tile: on the tile it would clip
   the tab away. */
.psc-fold > .psc-mosaic{margin:6px 6px 0;border-radius:5px;overflow:hidden}
/* Only the edge catches the light. Lifting the whole card would make the tile
   flash, which is a lot of movement for a hover. */
.psc-fold:hover,.psc-fold:hover::before{border-color:var(--psc-fold-line-hover)}
.psc-fold:focus-visible{outline:2px solid #3f8f68;outline-offset:2px}
/* Four thumbnails in one tile, on the same 3:2 footprint a single card image
   uses.

   minmax(0,1fr) rather than 1fr on every track, and min-width/min-height:0 on
   the images. A plain 1fr is minmax(auto,1fr), and that auto floor is the
   image's own min-content size - so one portrait photograph stretches its row
   to the full height of the photograph, the aspect-ratio above is overruled,
   and the tile grows to several times the height of its neighbours. Zeroing
   the floor is what lets object-fit:cover crop instead. */
/* The gap and the loading placeholder are the tile's own card showing between
   the prints, rather than black, so the four read as four rather than as one
   grid with lines drawn on it. */
.psc-mosaic{display:grid;gap:2px;aspect-ratio:3/2;overflow:hidden;
  background:var(--psc-card);
  grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  grid-template-rows:minmax(0,1fr) minmax(0,1fr)}
.psc-mosaic img{width:100%;height:100%;min-width:0;min-height:0;
  object-fit:cover;display:block;background:var(--psc-card)}
/* No cell is ever taller than it is wide, whatever shape the photographs are:
   a portrait frame is cropped to a landscape window rather than given a tall
   one to sit in. One fills the tile, two stack as full-width bands, three put
   the odd one along the bottom. */
.psc-mosaic.n1 img{grid-column:1/3;grid-row:1/3}
.psc-mosaic.n2 img{grid-column:1/3}
.psc-mosaic.n3 img:nth-child(3){grid-column:1/3}
/* Two lines, then an ellipsis. Folder names here run to eighty characters and
   a tile that grew to fit one would drag its whole row with it; the full name
   is on the title attribute. This is the same bargain .psc-meta strikes on a
   card, for the same reason. */
.psc-foldname{font-weight:600;font-size:13px;padding:9px 11px 0;
  overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;
  line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* margin-top:auto pins the count to the bottom of the tile, so the counts
   across a row sit on one line however the names above them wrapped. */
.psc-foldmeta{color:var(--psc-mut);font-size:11.5px;
  padding:3px 11px 11px;
  margin-top:auto;display:flex;align-items:baseline;gap:8px}
/* The folder's likes, pushed to the right. Empty and invisible until somebody
   has actually liked something in there - a row of zeroes would be noise, and
   a gallery published without a heart service must look untouched by it. */
.psc-fhearts{margin-left:auto;flex:none;font-variant-numeric:tabular-nums}
.psc-fhearts:empty{display:none}
/* Inside a gallery, a card whose folder is just the gallery's own name has
   nothing to add by repeating it. A card from a merged subfolder keeps its
   label, because there the folder still says something. */
.psc-view-folders.psc-open .psc-card[data-samefolder="1"] .psc-meta{display:none}
/* The header that replaces the index once a folder is open. */
.psc-crumb{display:none;align-items:center;gap:10px;margin:0 0 12px}
.psc-view-folders.psc-open .psc-crumb{display:flex}
.psc-crumb h3{margin:0;font-size:15px;font-weight:600;overflow-wrap:anywhere}
.psc-crumb .psc-back{background:#242424;color:var(--psc-fg);border:1px solid #3a3a3a;
  border-radius:6px;padding:6px 11px;font-size:13px;cursor:pointer;flex:none}
.psc-crumb .psc-back:hover{border-color:#5a5a5a}
.psc-crumbn{color:var(--psc-mut);font-size:12.5px;margin-left:auto;flex:none}
/* Copy link: the same quiet button as the way back, so the header reads as
   one row of controls rather than a call to action. */
.psc-crumb .psc-link{background:#242424;color:var(--psc-fg);border:1px solid #3a3a3a;
  border-radius:6px;padding:6px 11px;font-size:13px;cursor:pointer;flex:none}
.psc-crumb .psc-link:hover{border-color:#5a5a5a}
/* Shown instead of the tiles when a search matches nothing anywhere. */
.psc-empty{display:none;color:var(--psc-mut);padding:26px 2px;text-align:center}

/* Narrow screens. These must come after the rules they override: a media query
   carries no extra specificity, so a base rule declared later would win. */
@media (max-width:600px){
  /* On a 390px phone the gutter, not the column width, is what keeps a third
     column off the screen: three 115px columns need 361px at 8px, 373px at 14. */
  .psc-grid{gap:8px}
  .psc-folders{gap:8px;row-gap:calc(8px + var(--psc-tab))}
  .psc-bar{margin-left:-10px;margin-right:-10px;padding:6px 10px;gap:6px}
  /* A tighter bar on a phone, where it is pinned over the photographs. The
     buttons keep a comfortable height for a thumb; the saving comes from the
     padding around them and from the rows they wrap onto. */
  .psc-bar button{padding:5px 8px;font-size:12.5px}
  .psc-bar .psc-zoom button{min-width:30px;font-size:14px}
  /* A 60% basis puts the search box on a row of its own with the count
     beside it, rather than letting it squeeze in after the buttons. */
  .psc-searchwrap{flex:1 1 60%;min-width:0;padding:2px 7px}
  .psc-bar .psc-q{min-width:0}
  .psc-bar select{padding:4px 8px}
  /* The count sits beside the search box instead of on a row of its own. It
     follows the search in the markup, so giving both the same order keeps
     them together; the sort, Save tags and zoom follow on the next row. */
  .psc-searchwrap,.psc-count{order:1}
  .psc-sort,.psc-save,.psc-zoom{order:2}
  .psc-count{font-size:11.5px}
}
</style>
"""

GALLERY_JS = r"""
<script>
(function(){
  var root = document.currentScript.closest('.psc-wrap') ||
             document.querySelector('.psc-wrap');
  if (!root) return;
  // Lets the stylesheet reach the host page's title block without affecting
  // any other page on the site.
  document.documentElement.classList.add('psc-host');
  var RAW = JSON.parse(root.querySelector('.psc-data').textContent);
  // Photographs in one list, gallery names in another, each photograph holding
  // the index of its own. Everything below works on DATA exactly as it did when
  // the payload was a bare array.
  var DATA = RAW.p, GROUPS = RAW.g;
  var grid = root.querySelector('.psc-grid');
  var folders = root.querySelector('.psc-folders');
  var crumb = root.querySelector('.psc-crumb');
  var crumbN = crumb.querySelector('.psc-crumbn');
  var emptyEl = root.querySelector('.psc-empty');
  // Which gallery is open, as an index into GROUPS, or -1 for the index itself.
  var openGroup = -1;
  var lb = root.querySelector('.psc-lb'), lbImg = lb.querySelector('img');
  // Re-parent the overlay to <body>. position:fixed resolves against the
  // nearest ancestor with a transform/filter/perspective rather than against
  // the viewport, and a Ghost theme may apply one anywhere above us. Hanging it
  // off body removes that whole class of breakage.
  if (lb.parentNode !== document.body) document.body.appendChild(lb);
  var band = 'all', query = '', likedOnly = false;
  // Empty when the gallery was published without --hearts-url, in which case
  // no heart button is rendered at all.
  var HEARTS_API = root.dataset.hearts || '';
  // The overlay's own heart row, and the hook the hearts client fills in. Both
  // stay null when no heart service is configured.
  var lbHearts = lb.querySelector('.psc-hearts-lb');
  var paintLbHeart = null;
  // Which photograph the overlay is showing, or '' when it is closed.
  function lbOpenOn(){
    if (!lb.classList.contains('open') || cur < 0 || !order.length) return '';
    var p = DATA[order[cur]];
    return p ? p.id : '';
  }

  function hue(s){var h=0;s=s.toLowerCase();
    for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;
    return Math.round((h*137.508)%360);}

  var MONTHS = ['January','February','March','April','May','June','July',
                'August','September','October','November','December'];
  // '2026-08-24 14:03:22' -> 'August 24, 2026 \u00b7 14:03'. Anything malformed
  // renders as nothing rather than as a half-formed date. The time is dropped
  // when the camera did not record one; the date is never invented.
  function prettyDate(iso){
    if (!iso || iso.length < 10) return '';
    var y = +iso.slice(0,4), m = +iso.slice(5,7), d = +iso.slice(8,10);
    if (!(y >= 1900 && y <= 2200 && m >= 1 && m <= 12 && d >= 1 && d <= 31)) return '';
    var out = MONTHS[m-1] + ' ' + d + ', ' + y;
    if (iso.length >= 16) {
      var hh = +iso.slice(11,13), mi = +iso.slice(14,16);
      if (hh >= 0 && hh <= 23 && mi >= 0 && mi <= 59)
        out += ' \u00b7 ' + (hh < 10 ? '0' : '') + hh + ':' + (mi < 10 ? '0' : '') + mi;
    }
    return out;
  }

  // ---- the model -----------------------------------------------------------
  // What the filters run on. The grid is built from this on demand - a folder's
  // cards only come into existence when somebody opens that folder - so
  // filtering, counting and the folder index all answer from here rather than
  // from whatever happens to be in the DOM. A library of ten thousand
  // photographs therefore costs the same to open as one of five hundred.
  //
  // Everything searchable in one string: file name, folder, every date form,
  // the rating band and the written feedback, so 'wyoming', 'August 2011',
  // '2011-06', 'top pick' and 'moody landscape' all find photographs.
  // Lowercased here and the query is lowercased on input, so every search is
  // case-insensitive in both directions.
  //
  // Tags are NOT baked in: they change while the page is open, and apply()
  // reads TAGBLOB for them instead.
  var SEARCH = DATA.map(function(p){
    return ((p.n||'') + ' ' + (p.f||'') + ' ' + (p.fr||'') + ' ' + (p.d||'') +
            ' ' + prettyDate(p.d) + ' ' + (p.r||'') + ' ' + (p.v||'') + ' ' +
            (p.note||'') + ' ' + (p.w||'')).toLowerCase();
  });
  // 1 where the photograph passes the current filters. Written by apply(), read
  // by everything that has to count, cover or order a folder.
  var MATCH = new Uint8Array(DATA.length);
  // Heart tallies by photo_id, held outside the DOM so a folder nobody has
  // opened still contributes its likes to the index.
  var HEARTS = {};
  // Filled in by the hearts client, so cards built later still get painted.
  var paintNewCardHearts = null;

  function buildCard(i){
    var p = DATA[i];
    var c = document.createElement('div');
    c.className = 'psc-card';
    var pd = prettyDate(p.d);
    c.dataset.verdict = p.v;
    c.dataset.search = SEARCH[i];
    c.dataset.idx = i;
    c.dataset.group = p.g;
    // Whether this card's folder label is simply the gallery it sits in, so
    // the stylesheet can drop it once you are already inside that gallery.
    if ((p.f || '') === GROUPS[p.g]) c.dataset.samefolder = '1';
    c.dataset.name = p.n || '';
    c.dataset.folder = p.f || '';
    c.dataset.date = p.d || '';
    c.dataset.score = p.s;
    c.dataset.photoId = p.id;

    if (p.th) {
      var im = document.createElement('img');
      im.loading = 'lazy'; im.src = p.th; im.alt = p.n || '';
      im.addEventListener('click', function(){ navPhoto(i); });
      c.appendChild(im);
    }
    var b = document.createElement('div'); b.className = 'psc-body';
    var top = document.createElement('div'); top.className = 'psc-top';
    var badge = document.createElement('span');
    badge.className = 'psc-badge psc-' + p.v.replace(/[^A-Z]/g,'');
    badge.textContent = p.v;
    var sc = document.createElement('span'); sc.textContent = p.s;
    sc.style.cssText = 'font-variant-numeric:tabular-nums;font-weight:700';
    top.appendChild(badge); top.appendChild(sc); b.appendChild(top);

    var nm = document.createElement('div'); nm.className='psc-name';
    nm.textContent = p.n || ''; b.appendChild(nm);
    // The folder alone on its line. It is the only unbounded field, so it is
    // the only one allowed to truncate; the title shows it in full on hover.
    if (p.f){
      var mt = document.createElement('div'); mt.className='psc-meta';
      mt.textContent = p.f;
      mt.title = p.f;
      b.appendChild(mt);
    }
    // Split back into individual facts so the line can wrap between them and
    // never through one. prettyDate and the resolution share a separator, so a
    // single split recovers all four.
    var specs = [pd, p.r || ''].filter(Boolean).join(' \u00b7 ').split(' \u00b7 ');
    if (specs[0]){
      var sp = document.createElement('div'); sp.className = 'psc-specs';
      specs.forEach(function(fact, k){
        if (k) sp.appendChild(document.createTextNode(' \u00b7 '));
        var s = document.createElement('span');
        s.textContent = fact;
        sp.appendChild(s);
      });
      b.appendChild(sp);
    }
    if (p.note || p.w){
      var nt=document.createElement('div'); nt.className='psc-note';
      nt.textContent = p.note || '';
      if (p.w){
        nt.appendChild(document.createTextNode(' \u00b7 '));
        var fl=document.createElement('span'); fl.className='psc-flag';
        fl.textContent=p.w; nt.appendChild(fl);
      }
      b.appendChild(nt);
    }

    // Editable tag row. Chips are painted in renderCardTags once TAGS has been
    // merged with whatever this visitor stored last time.
    var tw = document.createElement('div');
    tw.className = 'psc-tags psc-tagwrap';
    var ti = document.createElement('input');
    ti.className = 'psc-taginput'; ti.type = 'text';
    ti.autocomplete = 'off'; ti.spellcheck = false;
    ti.placeholder = 'add tag, comma or Enter';
    tw.appendChild(ti);
    b.appendChild(tw);

    // Heart button and its tally. Hidden until the service answers, and left
    // hidden for good if it never does.
    if (HEARTS_API) {
      var hrow = document.createElement('div');
      hrow.className = 'psc-hearts psc-pending';
      var hb = document.createElement('button');
      hb.className = 'psc-heart'; hb.type = 'button';
      hb.dataset.photoId = p.id; hb.textContent = '\u2764\ufe0f';
      hb.title = 'Like this photograph';
      hb.setAttribute('aria-pressed', 'false');
      var hc = document.createElement('span');
      hc.className = 'psc-hcount';
      hrow.appendChild(hb); hrow.appendChild(hc);
      b.appendChild(hrow);
    }

    c.appendChild(b);
    renderCardTags(c);
    wireTagInput(c);
    if (HEARTS[p.id] != null) c.dataset.hearts = HEARTS[p.id];
    return c;
  }

  // idx -> the card element, once it has been built. Sparse on purpose.
  var cardFor = new Array(DATA.length);
  // The cards that currently exist, in the order the grid shows them.
  function builtCards(){ return [].slice.call(grid.children); }

  // Build whatever is missing from a list of indices and hand back the new
  // elements. Everything downstream - tags, hearts, the sort order - is applied
  // to them here, so a card built on the thousandth click is indistinguishable
  // from one built at load.
  function ensureCards(list){
    var made = [], frag = null;
    for (var k = 0; k < list.length; k++){
      var i = list[k];
      if (cardFor[i]) continue;
      var c = buildCard(i);
      cardFor[i] = c;
      if (!frag) frag = document.createDocumentFragment();
      frag.appendChild(c);
      made.push(c);
    }
    if (frag) grid.appendChild(frag);
    if (made.length && paintNewCardHearts) paintNewCardHearts(made);
    return made;
  }

  var ALLIDX = null;
  function allIdx(){
    if (!ALLIDX){
      ALLIDX = new Array(DATA.length);
      for (var i = 0; i < DATA.length; i++) ALLIDX[i] = i;
    }
    return ALLIDX;
  }

  // The cards the current view actually needs. The folder index needs none at
  // all: it is drawn from the model, and opening a folder is what brings that
  // folder's photographs into the page.
  function ensureView(){
    var list = view === 'all' ? allIdx()
             : (openGroup >= 0 ? byGroup[openGroup] : null);
    if (list && ensureCards(list).length) sortCards(photoSort);
  }

  var counter = root.querySelector('.psc-count');

  // ---- tagging ------------------------------------------------------------
  // Everything here lives in the visitor's own browser. Nothing is sent to
  // Ghost, and tags are keyed by photo_id, so no path from the photographer's
  // disk is ever published.
  var STORE_KEY = 'psc-tags:' + location.pathname;
  var TAGS = {}, storageWorks = true;
  DATA.forEach(function(p){ if (p.t && p.t.length) TAGS[p.id] = p.t.slice(); });
  try {
    var saved = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    // The visitor's own edits win over whatever was baked into the page.
    for (var k in saved) if (Array.isArray(saved[k])) TAGS[k] = saved[k];
  } catch (e) {}

  // The searchable form of each photograph's tags, kept alongside TAGS so the
  // filter can read them for photographs whose cards do not exist yet.
  // Pipe-delimited so a chip cannot match a substring: "Lake" must not match
  // "Lake Photos". A pipe can never appear in a tag.
  var TAGBLOB = {};
  function reblob(id){
    var t = TAGS[id];
    if (t && t.length) TAGBLOB[id] = '|' + t.join('|').toLowerCase() + '|';
    else delete TAGBLOB[id];
  }
  function reblobAll(){
    TAGBLOB = {};
    for (var k in TAGS) reblob(k);
  }
  reblobAll();

  var toastEl = root.querySelector('.psc-toast') ||
                document.body.appendChild(document.createElement('div'));
  toastEl.className = 'psc-toast';
  // Same reasoning as the lightbox: an ancestor transform would capture a
  // position:fixed child, so the toast hangs off <body>.
  if (toastEl.parentNode !== document.body) document.body.appendChild(toastEl);
  var toastTimer = null;
  function toast(msg, actionLabel, onAction){
    toastEl.textContent = msg;
    if (actionLabel && onAction){
      var bt = document.createElement('button');
      bt.type = 'button'; bt.textContent = actionLabel;
      bt.onclick = function(){ toastEl.classList.remove('show'); onAction(); };
      toastEl.appendChild(bt);
    }
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    // An Undo that vanishes in two seconds is not an Undo.
    toastTimer = setTimeout(function(){ toastEl.classList.remove('show'); },
                            (actionLabel && onAction) ? 9000 : 2400);
  }

  function persist(){
    // Empty lists are pruned rather than stored: otherwise every photograph the
    // visitor merely looked at would leave an entry behind.
    for (var k in TAGS) if (!TAGS[k] || !TAGS[k].length) delete TAGS[k];
    try { localStorage.setItem(STORE_KEY, JSON.stringify(TAGS)); }
    catch (e) {
      if (storageWorks){
        storageWorks = false;
        toast('This browser will not store tags - use "Save tags" before closing');
      }
    }
  }

  // Letters, digits, space, underscore and hyphen only. This is what keeps a
  // tag from ever being anything but text - it can hold no markup to inject.
  function cleanTag(raw){
    return (raw || '').replace(/[^A-Za-z0-9 _-]/g, '')
                      .replace(/\s+/g, ' ').trim().slice(0, 40);
  }
  function tagsFor(id){ return TAGS[id] || (TAGS[id] = []); }
  function allTags(){
    var seen = {}, out = [];
    for (var k in TAGS) TAGS[k].forEach(function(t){
      var l = t.toLowerCase();
      if (!(l in seen)) { seen[l] = t; out.push(t); }
    });
    return out.sort(function(a,b){ return a.localeCompare(b); });
  }
  function tagCount(name){
    var n = name.toLowerCase(), c = 0;
    for (var k in TAGS)
      if (TAGS[k].some(function(t){ return t.toLowerCase() === n; })) c++;
    return c;
  }
  function paint(el, name){
    var h = hue(name);
    el.style.setProperty('--tb','hsl('+h+' 58% 24%)');
    el.style.setProperty('--tf','hsl('+h+' 85% 82%)');
    el.style.setProperty('--td','hsl('+h+' 50% 38%)');
  }
  function makeChip(name, title, onRemove){
    var el = document.createElement('span');
    el.className = 'psc-tag';
    paint(el, name);
    el.appendChild(document.createTextNode(name));   // text, never innerHTML
    if (onRemove){
      var x = document.createElement('button');
      x.type = 'button'; x.textContent = '\u00d7'; x.title = title || '';
      x.onclick = function(ev){ ev.stopPropagation(); onRemove(); };
      el.appendChild(x);
    }
    return el;
  }

  function renderCardTags(card){
    var id = card.dataset.photoId;
    var list = card.querySelector('.psc-tags');
    var input = list.querySelector('.psc-taginput');
    [].slice.call(list.querySelectorAll('.psc-tag')).forEach(function(n){ n.remove(); });
    tagsFor(id).forEach(function(name){
      var chip = makeChip(name, 'remove tag', function(){
        TAGS[id] = tagsFor(id).filter(function(t){ return t !== name; });
        if (!TAGS[id].length) delete TAGS[id];
        persist(); renderCardTags(card); refreshTagUI();
      });
      list.insertBefore(chip, input);
    });
    reblob(id);
    card.dataset.tags = TAGBLOB[id] || '';
  }

  function addTagToCard(card, raw){
    var name = cleanTag(raw);
    if (!name) return false;
    var cur = tagsFor(card.dataset.photoId);
    if (cur.some(function(t){ return t.toLowerCase() === name.toLowerCase(); }))
      return false;
    cur.push(name);
    cur.sort(function(a,b){ return a.localeCompare(b); });
    persist(); renderCardTags(card); refreshTagUI();
    return true;
  }

  function wireTagInput(card){
    var input = card.querySelector('.psc-taginput');
    input.addEventListener('keydown', function(e){
      if (e.key === 'Enter' || e.key === ','){
        e.preventDefault(); addTagToCard(card, input.value); input.value = '';
      } else if (e.key === 'Backspace' && !input.value){
        var cur = tagsFor(card.dataset.photoId);
        if (cur.length){
          cur.pop();
          if (!cur.length) delete TAGS[card.dataset.photoId];
          persist(); renderCardTags(card); refreshTagUI();
        }
      } else if (e.key === 'Escape'){ input.value = ''; input.blur(); }
    });
    input.addEventListener('input', function(){
      if (input.value.indexOf(',') >= 0){
        var parts = input.value.split(','), tail = parts.pop();
        parts.forEach(function(p){ addTagToCard(card, p); });
        input.value = cleanTag(tail);
      }
    });
    // Catches the half-typed tag people leave behind when they click away.
    input.addEventListener('blur', function(){
      if (input.value.trim()){ addTagToCard(card, input.value); input.value = ''; }
    });
  }

  // ---- the tag search ------------------------------------------------------
  var selected = [];
  var chipBox = root.querySelector('.psc-chips');
  var menu = root.querySelector('.psc-tagmenu');
  var searchWrap = root.querySelector('.psc-searchwrap');
  var qEl = root.querySelector('.psc-q');

  function renderChips(){
    chipBox.textContent = '';
    selected.sort(function(a,b){ return a.localeCompare(b); });
    selected.forEach(function(name){
      chipBox.appendChild(makeChip(name, 'remove from search', function(){
        selected.splice(selected.indexOf(name), 1);
        renderChips(); apply();
      }));
    });
  }
  function selectTag(name){
    if (!selected.some(function(t){ return t.toLowerCase() === name.toLowerCase(); }))
      selected.push(name);
    qEl.value = '';
    clearTimeout(qTimer); qTimer = null; query = '';
    closeMenu(); renderChips(); apply();
  }

  var menuItems = [], menuIdx = -1;
  function closeMenu(){ menu.classList.remove('open'); menuItems = []; menuIdx = -1; }
  function openMenu(term){
    var t = (term || '').trim().toLowerCase();
    var chosen = selected.map(function(x){ return x.toLowerCase(); });
    var matches = allTags().filter(function(n){
      return chosen.indexOf(n.toLowerCase()) < 0 &&
             (!t || n.toLowerCase().indexOf(t) >= 0);
    });
    menu.textContent = '';
    menuItems = matches.slice(0, 40);
    menuIdx = menuItems.length ? 0 : -1;
    if (!menuItems.length){
      if (!t){ closeMenu(); return; }
      var d0 = document.createElement('div');
      d0.className = 'psc-none';
      d0.textContent = 'No tag matches - still searching everything else';
      menu.appendChild(d0);
      menu.classList.add('open');
      return;
    }
    menuItems.forEach(function(name, i){
      var d = document.createElement('div');
      if (i === menuIdx) d.className = 'sel';
      d.appendChild(makeChip(name, null, null));
      var c = document.createElement('span');
      c.className = 'psc-cnt';
      var n = tagCount(name);
      c.textContent = n + (n === 1 ? ' photo' : ' photos');
      d.appendChild(c);
      var del = document.createElement('button');
      del.className = 'psc-delall'; del.type = 'button'; del.textContent = 'remove';
      del.title = 'Remove "' + name + '" from every photo that carries it';
      // mousedown, not click: the row selects on mousedown, so the button has to
      // intercept the same event or the tag would be picked before it is deleted.
      del.onmousedown = function(e){ e.preventDefault(); e.stopPropagation();
                                     removeTagEverywhere(name); };
      d.appendChild(del);
      d.onmousedown = function(e){ e.preventDefault(); selectTag(name); };
      menu.appendChild(d);
    });
    menu.classList.add('open');
  }
  function moveMenu(delta){
    if (!menuItems.length) return;
    menuIdx = (menuIdx + delta + menuItems.length) % menuItems.length;
    [].slice.call(menu.children).forEach(function(c, i){
      c.classList.toggle('sel', i === menuIdx); });
    var el = menu.children[menuIdx];
    if (el && el.scrollIntoView) el.scrollIntoView({block: 'nearest'});
  }

  // A tag survives as long as ONE photograph still carries it, and that
  // photograph may be hidden by the band buttons or the search, so deleting
  // every chip you can see is not always enough to retire a tag.
  function removeTagEverywhere(name){
    var needle = name.toLowerCase();
    var n = tagCount(name);
    if (!n){ refreshTagUI(); return; }
    var scope = n === 1 ? 'the 1 photo that uses it'
                        : 'all ' + n + ' photos that use it';
    if (!confirm('Delete the tag "' + name + '" from ' + scope + '?\n\n' +
                 'This includes photos hidden by the current filters.')) return;
    var before = JSON.stringify(TAGS), hit = 0;
    Object.keys(TAGS).forEach(function(k){
      var kept = TAGS[k].filter(function(t){ return t.toLowerCase() !== needle; });
      if (kept.length !== TAGS[k].length) hit++;
      if (kept.length) TAGS[k] = kept; else delete TAGS[k];
    });
    persist(); reblobAll();
    builtCards().forEach(function(c){ renderCardTags(c); });
    refreshTagUI();
    toast('Removed "' + name + '" from ' + hit + (hit === 1 ? ' photo' : ' photos'),
          'Undo', function(){
      var restored = JSON.parse(before);
      Object.keys(TAGS).forEach(function(k){ delete TAGS[k]; });
      for (var k in restored) TAGS[k] = restored[k];
      persist(); reblobAll();
      builtCards().forEach(function(c){ renderCardTags(c); });
      refreshTagUI();
      toast('Put "' + name + '" back');
    });
  }

  function refreshTagUI(){
    // A tag can leave the gallery entirely; drop it from the search too.
    var live = allTags().map(function(t){ return t.toLowerCase(); });
    for (var i = selected.length - 1; i >= 0; i--)
      if (live.indexOf(selected[i].toLowerCase()) < 0) selected.splice(i, 1);
    renderChips();
    if (menu.classList.contains('open')) openMenu(qEl.value);
    apply();
  }

  qEl.addEventListener('focus', function(){
    searchWrap.classList.add('focus'); openMenu(qEl.value); });
  qEl.addEventListener('blur', function(){
    searchWrap.classList.remove('focus'); setTimeout(closeMenu, 120); });
  qEl.addEventListener('keydown', function(e){
    if (e.key === 'ArrowDown'){ e.preventDefault(); moveMenu(1); }
    else if (e.key === 'ArrowUp'){ e.preventDefault(); moveMenu(-1); }
    else if (e.key === 'Enter'){
      if (menuIdx >= 0 && menuItems[menuIdx]){ e.preventDefault(); selectTag(menuItems[menuIdx]); }
    } else if (e.key === 'Backspace' && !qEl.value && selected.length){
      selected.pop(); renderChips(); apply();
    } else if (e.key === 'Escape'){
      qEl.value = ''; closeMenu(); searchNow('');
    }
  });
  // Clicking the padding around the input should focus it, as a single box would.
  searchWrap.addEventListener('mousedown', function(e){
    if (e.target === searchWrap) { e.preventDefault(); qEl.focus(); } });

  var saveBtn = root.querySelector('.psc-save');
  if (saveBtn) saveBtn.onclick = function(){
    var blob = new Blob([JSON.stringify(TAGS, null, 2)], {type:'application/json'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'psc-web-tags.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 2000);
    toast('psc-web-tags.json downloaded');
  };
  // Two passes, and the split is the point. The first runs over the model and
  // decides what matches; it touches no elements, so it costs the same whether
  // one card has been built or ten thousand. The second only walks the cards
  // that actually exist, which in the folder index is none at all.
  function apply(){
    var n = 0, folderOpen = (view === 'folders' && openGroup >= 0);
    // Lowercased and wrapped once rather than once per photograph.
    var chips = selected.map(function(t){ return '|' + t.toLowerCase() + '|'; });
    for (var i = 0; i < DATA.length; i++){
      var p = DATA[i];
      var tagged = TAGBLOB[p.id] || '';
      var ok = (band === 'all' || p.v === band);
      // Selected tags are ORed: each chip widens the results rather than
      // narrowing them, so two tags show every photograph carrying either.
      if (ok && chips.length){
        ok = false;
        for (var j = 0; j < chips.length; j++)
          if (tagged.indexOf(chips[j]) >= 0){ ok = true; break; }
      }
      // Composes with everything else: Top picks + Liked shows top picks that
      // someone has liked, not one or the other.
      if (ok && likedOnly) ok = (HEARTS[p.id] || 0) > 0;
      if (ok && query) ok = SEARCH[i].indexOf(query) >= 0 ||
                            tagged.indexOf(query) >= 0;
      // An open folder is the last word: whatever the filters allow, nothing
      // from another gallery appears, and the lightbox arrows - which walk
      // whatever is visible - therefore stop at the folder's edges.
      if (ok && folderOpen && p.g !== openGroup) ok = false;
      MATCH[i] = ok ? 1 : 0;
      if (ok) n++;
    }
    var live = grid.children;
    for (var k = 0; k < live.length; k++)
      live[k].classList.toggle('psc-hidden', !MATCH[+live[k].dataset.idx]);
    if (inIndex()){
      // n is every photograph that matched, across all the folders still
      // standing, so the two halves of this line answer different questions.
      var nf = refreshFolders();
      counter.textContent = nf + (nf === 1 ? ' folder' : ' folders') +
                            ' · ' + n + ' of ' + DATA.length;
    } else {
      emptyEl.style.display = 'none';
      counter.textContent = n + ' of ' + DATA.length;
      if (folderOpen){
        crumbN.textContent = n === byGroup[openGroup].length
          ? n + (n === 1 ? ' photo' : ' photos')
          : n + ' of ' + byGroup[openGroup].length;
      }
    }
    // If the grid is refiltered while the overlay is open, rebuild the walk
    // list but stay on the same photograph when it survives the change.
    if (lb.classList.contains('open')) {
      var wasOn = (cur >= 0 && order.length) ? order[cur] : -1;
      order = visibleIdx();
      cur = order.indexOf(wasOn);
      if (cur < 0) {
        if (order.length) { cur = 0; show(); writeHash('photo', false); }
        else { closeLb(); writeHash(openGroup >= 0 ? 'folder' : '', false); }
      } else show();
    }
  }
  root.querySelectorAll('.psc-bar button[data-band]').forEach(function(btn){
    btn.onclick=function(){
      root.querySelectorAll('.psc-bar button[data-band]').forEach(function(x){
        x.classList.remove('on');});
      btn.classList.add('on'); band=btn.dataset.band; apply();
    };
  });
  var likedBtn = root.querySelector('.psc-likedonly');
  if (likedBtn) likedBtn.onclick = function(){
    likedOnly = !likedOnly;
    likedBtn.classList.toggle('on', likedOnly);
    apply();
  };

  // The tag menu reopens on every keystroke - it is what the visitor is
  // watching while they type - but the filter itself waits for a pause. On a
  // large library each pass reads every photograph, and running one per
  // keystroke is what makes a search box feel like it is fighting back.
  var qTimer = null;
  function searchNow(value){
    clearTimeout(qTimer); qTimer = null;
    query = (value || '').toLowerCase();
    apply();
  }
  qEl.oninput = function(e){
    var raw = e.target.value;
    openMenu(raw);
    clearTimeout(qTimer);
    qTimer = setTimeout(function(){ searchNow(raw); }, 120);
  };

  // ---- sorting ------------------------------------------------------------
  // Cards are moved in the DOM rather than rebuilt, so tags, hearts and any
  // other per-card state survive a sort. visibleIdx() walks the grid in DOM
  // order, so the lightbox arrows follow the sort with no extra bookkeeping.
  var collator = new Intl.Collator(undefined, {numeric:true, sensitivity:'base'});
  function sortCards(mode){
    var bits = mode.split('-'), key = bits[0], sign = bits[1]==='desc' ? -1 : 1;
    function value(c){
      if (key === 'hearts') return parseFloat(c.dataset.hearts || '0');
      if (key === 'score') return parseFloat(c.dataset.score || '0');
      if (key === 'date') return c.dataset.date || '';
      if (key === 'folder') return c.dataset.folder || '';
      return c.dataset.name || '';
    }
    var ordered = builtCards().sort(function(a,b){
      var va = value(a), vb = value(b), r;
      if (key === 'score' || key === 'hearts') r = va - vb;
      else if (!va && !vb) r = 0;
      // Undated or unfoldered photographs always sink to the bottom rather
      // than clumping at whichever end the direction happens to favour.
      else if (!va) return 1;
      else if (!vb) return -1;
      // Timestamps are 'YYYY-MM-DD HH:MM:SS', so a plain string comparison is
      // already chronological to the second. The collator is deliberately not
      // used here: its numeric mode reads digit runs as numbers, which is right
      // for file names and wrong for a fixed-width date.
      else if (key === 'date') r = va < vb ? -1 : va > vb ? 1 : 0;
      else r = collator.compare(va, vb);
      // Ties fall back to the rating, so equal dates stay meaningfully ordered.
      if (r === 0) return parseFloat(b.dataset.score||'0') - parseFloat(a.dataset.score||'0');
      return r * sign;
    });
    var frag = document.createDocumentFragment();
    ordered.forEach(function(c){ frag.appendChild(c); });
    grid.appendChild(frag);
  }
  var sortSel = root.querySelector('.psc-sort');
  if (sortSel) sortSel.onchange = function(e){
    // On the index the box orders the folder tiles; everywhere else it orders
    // the photographs. Each view remembers its own choice.
    if (inIndex()){ indexSort = e.target.value; sortFolders(indexSort); }
    else { photoSort = e.target.value; sortCards(photoSort); }
    apply();
  };

  // ---- folder galleries ---------------------------------------------------
  // Two ways through the same photographs: an index of folders to pick from, or
  // all of them at once. Nothing is duplicated - the cards are the same elements
  // in both, and the index hides the grid rather than emptying it, so tags,
  // hearts, the sort order and the lightbox all survive switching back and
  // forth.
  var VIEW_KEY = 'psc-view:' + location.pathname;
  var view = root.dataset.view === 'all' ? 'all' : 'folders';
  try {
    var savedView = localStorage.getItem(VIEW_KEY);
    if (savedView === 'all' || savedView === 'folders') view = savedView;
  } catch (e) {}

  // Which photographs belong to which gallery, as indices into DATA. Indices
  // rather than elements: a folder has to be counted, covered and sorted long
  // before anybody opens it, and until they do it has no cards.
  var byGroup = GROUPS.map(function(){ return []; });
  DATA.forEach(function(p, i){ byGroup[+p.g].push(i); });

  // A-Z by name, with the same collator the Folder A-Z sort uses on cards, so
  // the two agree to the letter. Fixed rather than adjustable: a box offering
  // one order and its reverse is a control that earns nothing. Unfiled is not
  // a folder anybody named, so it sits at the end whatever it would collate as.
  var UNFILED = 'Unfiled';
  var folderOrder = GROUPS.map(function(_, i){ return i; }).sort(function(a, b){
    var na = GROUPS[a], nb = GROUPS[b];
    if ((na === UNFILED) !== (nb === UNFILED)) return na === UNFILED ? 1 : -1;
    return collator.compare(na, nb);
  });

  // '2011' for a single year, '2010-2011' for a shoot that crossed one.
  function yearSpan(a, b){
    var ya = a.slice(0, 4), yb = b.slice(0, 4);
    return ya === yb ? ya : ya + '–' + yb;
  }

  var tiles = [];
  folderOrder.forEach(function(gi){
    // A real <button>, so it is focusable, reachable by keyboard and announced
    // as a control without any of that having to be reimplemented.
    var t = document.createElement('button');
    t.type = 'button';
    t.className = 'psc-fold';
    t.dataset.group = gi;
    var mos = document.createElement('div');
    mos.className = 'psc-mosaic';
    var imgs = [];
    for (var k = 0; k < 4; k++){
      var im = document.createElement('img');
      im.loading = 'lazy'; im.alt = '';
      mos.appendChild(im); imgs.push(im);
    }
    var nm = document.createElement('div');
    nm.className = 'psc-foldname';
    nm.textContent = GROUPS[gi];
    nm.title = GROUPS[gi];
    var mt = document.createElement('div');
    mt.className = 'psc-foldmeta';
    var mc = document.createElement('span');
    var mh = document.createElement('span');
    mh.className = 'psc-fhearts';
    mt.appendChild(mc); mt.appendChild(mh);
    t.appendChild(mos); t.appendChild(nm); t.appendChild(mt);
    t.onclick = function(){ navFolder(gi); };
    folders.appendChild(t);
    tiles.push({el: t, gi: gi, mos: mos, imgs: imgs, meta: mc, hearts: mh});
  });

  // The cover shows the best of whatever currently passes the filters, so a
  // search for 'night' leaves every folder wearing its best night photograph
  // rather than a cover that no longer represents what is inside.
  function paintTile(t){
    var live = [], oldest = '', newest = '', likes = 0, ids = byGroup[t.gi];
    for (var k = 0; k < ids.length; k++){
      if (!MATCH[ids[k]]) continue;
      var p = DATA[ids[k]];
      live.push(ids[k]);
      likes += HEARTS[p.id] || 0;
      var d = p.d || '';
      if (d){ if (!oldest || d < oldest) oldest = d; if (d > newest) newest = d; }
    }
    t.el.style.display = live.length ? '' : 'none';
    if (!live.length) return;
    live.sort(function(a, b){
      return (parseFloat(DATA[b].s) || 0) - (parseFloat(DATA[a].s) || 0);
    });
    var pick = live.slice(0, 4);
    // The class drives the layout: one photograph fills the tile, two split it,
    // three put the best one down the left. Four is the plain 2x2.
    t.mos.className = 'psc-mosaic n' + pick.length;
    t.imgs.forEach(function(im, k){
      if (pick[k] === undefined){
        im.style.display = 'none'; im.removeAttribute('src'); return;
      }
      im.style.display = '';
      // Only touched when it actually changes, so typing in the search box does
      // not restart a download on every keystroke.
      var want = DATA[pick[k]].th || '';
      if (im.getAttribute('src') !== want) im.setAttribute('src', want);
    });
    t.meta.textContent = live.length + (live.length === 1 ? ' photo' : ' photos') +
      (newest ? ' · ' + yearSpan(oldest || newest, newest) : '');
    // Blank, not zero: a folder nobody has liked says nothing at all, and the
    // :empty rule then takes the element out of the layout entirely.
    t.hearts.textContent = likes > 0 ? '\u2764\ufe0f ' + likes : '';
  }

  // The heart tallies arrive after the page has drawn, and change again every
  // time somebody clicks one, so the index is repainted on both.
  function heartsChanged(){ if (inIndex()) refreshFolders(); }

  function inIndex(){ return view === 'folders' && openGroup < 0; }

  // One sort box serves both views, but they want different answers, so each
  // keeps its own choice. The index opens on A-Z because that is the order the
  // folders are meant to read in; a folder opens on the best photographs first,
  // the same as the flat gallery.
  var indexSort = 'folder-asc', photoSort = 'score-desc';

  // What a folder is worth under each ordering. Score, date and likes are read
  // off the photographs currently passing the filters, so a search reorders the
  // tiles by what actually survived it rather than by what used to be inside.
  function folderKey(gi, key){
    var best = null, likes = 0, seen = false, ids = byGroup[gi];
    for (var k = 0; k < ids.length; k++){
      if (!MATCH[ids[k]]) continue;
      var p = DATA[ids[k]];
      seen = true;
      likes += HEARTS[p.id] || 0;
      if (key === 'score'){
        var s = parseFloat(p.s) || 0;
        if (best === null || s > best) best = s;
      } else if (key === 'date'){
        var d = p.d || '';
        if (d && (best === null || d > best)) best = d;
      }
    }
    if (key === 'hearts') return seen ? likes : 0;
    return best;
  }

  // Tiles are moved rather than rebuilt, so their mosaics keep the images they
  // have already downloaded.
  function sortFolders(mode){
    var bits = mode.split('-'), key = bits[0], sign = bits[1] === 'desc' ? -1 : 1;
    // A tile has no file name of its own, so File name orders it the only way
    // that means anything here - by the folder's name.
    if (key === 'name') key = 'folder';
    var order = folderOrder.slice().sort(function(a, b){
      var na = GROUPS[a], nb = GROUPS[b];
      // Unfiled is not a folder anybody named; it sits at the end of every
      // ordering rather than leading the reversed ones.
      if ((na === UNFILED) !== (nb === UNFILED)) return na === UNFILED ? 1 : -1;
      var r;
      if (key === 'folder') r = collator.compare(na, nb);
      else {
        var va = folderKey(a, key), vb = folderKey(b, key);
        // A folder with nothing showing sinks, whichever way the sort runs.
        if (va === null && vb === null) r = 0;
        else if (va === null) return 1;
        else if (vb === null) return -1;
        else if (key === 'date') r = va < vb ? -1 : va > vb ? 1 : 0;
        else r = va - vb;
      }
      // Ties fall back to the name, so equal scores stay in a readable order.
      if (r === 0) return collator.compare(na, nb);
      return r * sign;
    });
    var pos = {};
    order.forEach(function(gi, i){ pos[gi] = i; });
    var frag = document.createDocumentFragment();
    tiles.slice().sort(function(a, b){ return pos[a.gi] - pos[b.gi]; })
         .forEach(function(t){ frag.appendChild(t.el); });
    folders.appendChild(frag);
  }

  // Called by apply() once the filters have decided what is visible.
  function refreshFolders(){
    var shown = 0;
    tiles.forEach(function(t){
      paintTile(t);
      if (t.el.style.display !== 'none') shown++;
    });
    // Score, date and likes are read off what survived the filters, so the
    // tiles are reordered alongside being repainted.
    sortFolders(indexSort);
    emptyEl.style.display = shown ? 'none' : 'block';
    return shown;
  }

  // Inside a folder, every photograph shares that folder, so ordering by it
  // sorts nothing. The options are taken out of the box there and put back in
  // All photos, where photographs really do span folders.
  //
  // Removed and reinserted rather than hidden: `hidden` on an <option> is not
  // honoured everywhere, and a select that silently ignores it would show an
  // option that does nothing.
  var allSortOpts = sortSel ? [].slice.call(sortSel.options) : [];
  // Each view drops the orderings that say nothing in it.
  //
  // On the index: Score and File name. A folder is not a photograph - it has no
  // score and no file name of its own, so neither is a question anybody is
  // asking of a row of folders.
  // Inside a folder: Folder. Every photograph in there shares it.
  // In All photos: none of them - the photographs span folders, and each one
  // has a score and a name.
  function syncSortOptions(){
    if (!sortSel) return;
    var drop = inIndex() ? ['score-', 'name-']
             : (view === 'folders' && openGroup >= 0) ? ['folder-'] : [];
    var want = allSortOpts.filter(function(o){
      return !drop.some(function(d){ return o.value.indexOf(d) === 0; });
    });
    // Compared by value, not by count: the index drops two options and a folder
    // drops two different ones, so the lists are the same LENGTH in both and a
    // length check would skip the rebuild and leave the wrong two missing.
    var have = [].slice.call(sortSel.options).map(function(o){ return o.value; });
    if (have.join(',') === want.map(function(o){ return o.value; }).join(',')) return;
    sortSel.textContent = '';
    want.forEach(function(o){ sortSel.appendChild(o); });
  }

  function offers(value){
    return [].slice.call(sortSel.options).some(function(o){ return o.value === value; });
  }

  // The sort box is on screen in every view; what changes is what it offers and
  // what it acts on. Each view keeps its own choice, so moving between them
  // never silently reorders the other one.
  function syncBar(){
    syncSortOptions();
    if (sortSel){
      var want = inIndex() ? indexSort : photoSort;
      // A choice the box no longer carries falls back to the order that view
      // opens on - which differs, since the index cannot offer Score and a
      // folder cannot offer Folder.
      if (!offers(want)){
        if (inIndex()) { indexSort = 'folder-asc'; want = indexSort; }
        else { photoSort = 'score-desc'; want = photoSort; sortCards(want); }
      }
      sortSel.value = want;
      if (inIndex()) sortFolders(indexSort);
    }
    root.classList.toggle('psc-view-folders', view === 'folders');
    root.classList.toggle('psc-open', view === 'folders' && openGroup >= 0);
    root.querySelectorAll('.psc-views button').forEach(function(b){
      b.classList.toggle('on', b.dataset.view === view);
    });
  }

  // Both entering and leaving a folder put you at the top of the gallery.
  // Landing halfway down a folder you have never seen, at whatever offset the
  // index happened to be scrolled to, is disorienting.
  function toTop(){
    if (root.getBoundingClientRect().top < 0) root.scrollIntoView({block: 'start'});
  }

  function openFolder(gi){
    openGroup = gi;
    crumb.querySelector('h3').textContent = GROUPS[gi];
    // This is where a folder's photographs come into the page.
    ensureView();
    syncBar(); apply(); toTop();
  }

  function closeFolder(){
    openGroup = -1;
    syncBar(); apply(); toTop();
  }

  function setView(v){
    view = v;
    openGroup = -1;
    try { localStorage.setItem(VIEW_KEY, v); } catch (e) {}
    // All photos is an explicit ask for everything at once, so it is the one
    // view that does build the whole grid.
    ensureView();
    syncBar(); apply();
  }

  root.querySelectorAll('.psc-views button').forEach(function(b){
    b.onclick = function(){ setView(b.dataset.view); writeHash('', false); };
  });
  var backBtn = root.querySelector('.psc-back');
  if (backBtn) backBtn.onclick = navCloseFolder;
  ensureView();
  syncBar();

  // ---- thumbnail size -----------------------------------------------------
  // Pinching a phone zooms the page, which magnifies one column rather than
  // showing more; these reflow the grid instead. The chosen step is remembered
  // per page, so a visitor who prefers a dense contact sheet keeps it.
  (function zoom(){
    var STEPS = [100, 115, 130, 160, 200, 240, 300, 380, 480, 620];
    var smaller = root.querySelector('.psc-smaller'),
        bigger  = root.querySelector('.psc-bigger');
    if (!smaller || !bigger) return;
    var KEY = 'psc-colw:' + location.pathname;
    var base = parseInt(getComputedStyle(root).getPropertyValue('--psc-colw'), 10) || 260;
    // Start from the step nearest the width the page was published with, so
    // --column-width still sets the default a first-time visitor sees.
    // A narrow screen starts two-up: one column at the published width fills a
    // phone, which is not a contact sheet. Any saved choice still wins.
    var want = innerWidth < 600 ? 160 : base;
    var at = 0;
    STEPS.forEach(function(w, i){
      if (Math.abs(w - want) < Math.abs(STEPS[at] - want)) at = i;
    });
    try {
      var saved = STEPS.indexOf(parseInt(localStorage.getItem(KEY), 10));
      if (saved >= 0) at = saved;
    } catch (e) {}

    function apply(){
      root.style.setProperty('--psc-colw', STEPS[at] + 'px');
      smaller.disabled = at === 0;
      bigger.disabled = at === STEPS.length - 1;
      try { localStorage.setItem(KEY, STEPS[at]); } catch (e) {}
    }
    smaller.addEventListener('click', function(){ if (at > 0) { at--; apply(); } });
    bigger.addEventListener('click', function(){
      if (at < STEPS.length - 1) { at++; apply(); } });
    apply();
  })();

  // ---- hearts -------------------------------------------------------------
  // Everything below is wrapped so that any failure - service down, network
  // blocked, adblocker, CDN in the way - leaves the gallery completely
  // functional with the hearts simply absent.
  (function hearts(){
    if (!HEARTS_API) return;

    // One identifier per browser, minted once. It is not an account and
    // identifies nobody: the service stores only a salted hash of it.
    var TOKEN_KEY = 'psc-heart-token';
    var token = '';
    try {
      token = localStorage.getItem(TOKEN_KEY) || '';
      if (!token) {
        token = (crypto && crypto.randomUUID) ? crypto.randomUUID()
              : 'x' + Math.random().toString(36).slice(2) +
                      Math.random().toString(36).slice(2);
        localStorage.setItem(TOKEN_KEY, token);
      }
    } catch (e) {
      // Private browsing with storage blocked. Hearts still work for this
      // page view; they just will not be remembered on the next one.
      token = 'x' + Math.random().toString(36).slice(2) +
                    Math.random().toString(36).slice(2);
    }

    function api(path, opts){
      opts = opts || {};
      opts.headers = opts.headers || {};
      opts.headers['X-Heart-Token'] = token;
      opts.cache = 'no-store';
      return fetch(HEARTS_API + path, opts).then(function(r){
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
    }

    // The tally for each photograph, so the card and the overlay always agree
    // no matter which one was clicked.
    var TALLY = {};

    function paintRow(row, count, mine){
      var btn = row.querySelector('.psc-heart');
      var num = row.querySelector('.psc-hcount');
      btn.classList.toggle('on', !!mine);
      btn.setAttribute('aria-pressed', mine ? 'true' : 'false');
      btn.title = mine ? 'Remove your like' : 'Like this photograph';
      num.textContent = count > 0 ? count : '';
      row.dataset.count = count;
      // Mirrored onto the card so sortCards can read it alongside date, name
      // and rating without knowing anything about hearts.
      var card = row.closest('.psc-card');
      if (card) card.dataset.hearts = count;
      row.dataset.mine = mine ? '1' : '';
    }

    // The tally is recorded whether or not anything is showing it. HEARTS is
    // what the folder index and the Liked filter read, and a folder nobody has
    // opened has no cards to read instead.
    function record(id, count, mine){
      TALLY[id] = {count: count, mine: !!mine};
      HEARTS[id] = count;
    }

    // Paint every row showing this photograph - its card, and the overlay if it
    // happens to be open on it.
    function paintPhoto(id, count, mine){
      record(id, count, mine);
      var sel = '.psc-heart[data-photo-id="' + id + '"]';
      [root, lb].forEach(function(scope){
        var btn = scope.querySelector(sel);
        if (btn) paintRow(btn.closest('.psc-hearts'), count, mine);
      });
      heartsChanged();
    }

    // Bring a set of heart rows up to date with the tallies already in hand,
    // and reveal them. Used once when the service answers, and again for every
    // card built after that.
    function paintRows(rows){
      rows.forEach(function(row){
        var btn = row.querySelector('.psc-heart');
        if (!btn) return;
        var t = TALLY[btn.dataset.photoId];
        if (!t) return;
        paintRow(row, t.count, t.mine);
        row.classList.remove('psc-pending');   // reveal only on success
      });
    }

    // Called by show() as the overlay moves between photographs.
    paintLbHeart = function(id){
      if (!lbHearts) return;
      var t = TALLY[id];
      lbHearts.querySelector('.psc-heart').dataset.photoId = id;
      lbHearts.classList.toggle('psc-pending', !t);
      if (t) paintRow(lbHearts, t.count, t.mine);
    };

    var answered = false;
    // Cards built after the service has answered - when a folder is opened -
    // arrive with their heart rows still hidden, so they are caught up here.
    paintNewCardHearts = function(made){
      if (!answered) return;
      var rows = [];
      made.forEach(function(c){
        var r = c.querySelector('.psc-hearts');
        if (r) rows.push(r);
      });
      paintRows(rows);
    };

    api('', {method: 'GET'}).then(function(data){
      var counts = data.counts || {}, mine = {};
      (data.mine || []).forEach(function(id){ mine[id] = true; });
      // Every photograph, not just the ones with a card: the folder index is
      // drawn from these numbers and most of its folders are still unopened.
      DATA.forEach(function(p){ record(p.id, counts[p.id] || 0, !!mine[p.id]); });
      answered = true;
      paintRows([].slice.call(root.querySelectorAll('.psc-hearts')));
      if (lbOpenOn()) paintLbHeart(lbOpenOn());
      heartsChanged();          // the folder index shows each folder's total
      // The tallies arrive after the grid is built, so a visitor who is already
      // on "Most liked" would be looking at an order built from zeroes.
      // Deliberately NOT re-sorted when someone clicks a heart: photographs
      // rearranging under the cursor is disorienting.
      var sel = root.querySelector('.psc-sort');
      if (sel && sel.value.indexOf('hearts') === 0) sortCards(sel.value);
      // Only offer the Liked filter once real counts exist. Pressing it while
      // every card still reads zero would empty the grid.
      if (likedBtn) likedBtn.classList.remove('psc-pending');
    }).catch(function(){
      // Leave every row hidden. Nothing is logged to the console: a visitor
      // does not need to see that a feature they never knew about is down.
    });

    // Both scopes: the overlay is re-parented to <body> and no longer bubbles
    // up to the gallery root.
    [root, lb].forEach(function(scope){
    scope.addEventListener('click', function(ev){
      var btn = ev.target.closest && ev.target.closest('.psc-heart');
      if (!btn) return;
      ev.stopPropagation();          // in the overlay, the backdrop closes
      var row = btn.closest('.psc-hearts');
      if (!row || row.classList.contains('psc-pending')) return;

      // Optimistic: the button responds instantly, and is put back if the
      // request fails. On this host the round trip is a few milliseconds, so a
      // spinner would flicker rather than inform.
      var id = btn.dataset.photoId;
      var was = TALLY[id] || {count: parseInt(row.dataset.count || '0', 10),
                              mine: !!row.dataset.mine};
      var nowMine = !was.mine;
      paintPhoto(id, Math.max(0, was.count + (nowMine ? 1 : -1)), nowMine);
      btn.disabled = true;

      api('/' + id, {method: nowMine ? 'POST' : 'DELETE'})
        .then(function(res){
          // Trust the server's number over the optimistic one: another
          // visitor may have hearted the same photograph in between.
          paintPhoto(id, res.count, res.hearted);
        })
        .catch(function(){
          paintPhoto(id, was.count, was.mine);
          toast('Could not save that like - the service may be down');
        })
        .then(function(){ btn.disabled = false; });
    });
    });
  })();

  // Which element actually scrolls varies by theme: sometimes <html>,
  // sometimes <body>. Locking only one of them leaves the page scrolling
  // behind the overlay, so lock both and restore exactly what was there.
  var navPrev = lb.querySelector('.psc-prev'),
      navNext = lb.querySelector('.psc-next'),
      lbCount = lb.querySelector('.psc-count-lb'),
      lbCap   = lb.querySelector('.psc-cap'),
      lbDims  = lb.querySelector('.psc-dims-lb');

  // The arrows walk whatever is VISIBLE, so they follow the All / Top picks /
  // Strong buttons and the search box with no extra bookkeeping. The list is
  // captured on open and rebuilt if the grid is refiltered underneath.
  var order = [], cur = -1;
  function visibleIdx(){
    // Read the live grid so the walk order matches what the visitor sees,
    // including after a sort has moved the cards around.
    var out = [], live = grid.children;
    for (var i = 0; i < live.length; i++)
      if (!live[i].classList.contains('psc-hidden')) out.push(+live[i].dataset.idx);
    return out;
  }
  function show(){
    if (cur < 0 || !order.length) return;
    var p = DATA[order[cur]];
    lbImg.src = p.pv || p.th || '';
    lbImg.alt = p.n || '';
    lbCap.textContent = p.n || '';
    // Kept out of the caption: a long file name would push it past the
    // ellipsis, and this is the view where resolution actually matters.
    lbDims.textContent = p.r || '';
    if (paintLbHeart) paintLbHeart(p.id);
    lbCount.textContent = (cur + 1) + ' / ' + order.length;
    var many = order.length > 1;
    navPrev.style.display = many ? 'flex' : 'none';
    navNext.style.display = many ? 'flex' : 'none';
    [-1, 1].forEach(function(d){          // warm the neighbours
      var n = DATA[order[(cur + d + order.length) % order.length]];
      if (n) { var im = new Image(); im.src = n.pv || n.th || ''; }
    });
  }
  function step(d){
    if (!order.length) return;
    cur = (cur + d + order.length) % order.length;    // wraps at both ends
    show();
    // Replaced rather than pushed: fifty photographs swiped through should not
    // take fifty presses of Back to leave.
    writeHash('photo', false);
  }

  var lockPrev = null;
  // Pinning the body at its current offset stops the page moving behind the
  // overlay. Two details matter on iOS, and both are about ROTATION:
  //
  //  * no overflow:hidden on <html>. That freezes the layout viewport at the
  //    portrait width; Safari then finds a 390px page in an 844px window after
  //    a rotation and scales up from the top-left to fill it. The fixed body
  //    alone already leaves nothing to scroll.
  //  * left/right rather than width. The body has to be free to stretch to the
  //    new width, and a fixed width is the same freeze by another route.
  function setScrollLock(on){
    var bd = document.body;
    if (on) {
      if (lockPrev) return;
      var y = window.scrollY || document.documentElement.scrollTop || 0;
      lockPrev = {y: y, position: bd.style.position, top: bd.style.top,
                  left: bd.style.left, right: bd.style.right};
      bd.style.position = 'fixed';
      bd.style.top = (-y) + 'px';
      bd.style.left = '0';
      bd.style.right = '0';
    } else if (lockPrev) {
      bd.style.position = lockPrev.position;
      bd.style.top = lockPrev.top;
      bd.style.left = lockPrev.left;
      bd.style.right = lockPrev.right;
      window.scrollTo(0, lockPrev.y);
      lockPrev = null;
    }
  }

  // iOS Safari sometimes rescales the whole page when the device is rotated,
  // anchored at the top-left. It shows up most on a short page - one of the
  // filter buttons pressed, so there are only a couple of rows - and it is not
  // confined to the lightbox, so this watches the gallery as a whole.
  //
  // Briefly pinning maximum-scale forces Safari back to 1; the tag is restored
  // a moment later so pinch-to-zoom keeps working everywhere on the site.
  function unzoom(){
    var meta = document.querySelector('meta[name="viewport"]');
    if (!meta) return;
    var was = meta.getAttribute('content');
    if (/maximum-scale/.test(was)) return;      // the site already pins it
    meta.setAttribute('content', was + ',maximum-scale=1');
    setTimeout(function(){ meta.setAttribute('content', was); }, 350);
  }

  // Only a rotation that ZOOMED counts. Somebody who pinched in deliberately
  // and then turned the phone keeps their zoom: the correction fires when the
  // scale was 1 before the turn and is not 1 after it.
  (function watchRotation(){
    var vv = window.visualViewport;
    if (!vv) return;
    var wasScale = vv.scale, wasPortrait = vv.height >= vv.width;
    vv.addEventListener('resize', function(){
      var nowPortrait = vv.height >= vv.width;
      var turned = nowPortrait !== wasPortrait;
      if (turned && wasScale <= 1.01 && vv.scale > 1.01) unzoom();
      // Read after the correction is queued: the next resize compares against
      // where this one left off.
      wasPortrait = nowPortrait;
      wasScale = vv.scale;
    });
  })();
  function openLb(i){
    order = visibleIdx();
    cur = order.indexOf(i);
    if (cur < 0) { order = [i]; cur = 0; }   // opened something already filtered out
    lb.classList.add('open');
    show();
    setScrollLock(true);
  }
  function closeLb(){
    // Leave the page on the photograph being looked at, not the one that was
    // clicked several swipes ago.
    var card = cur >= 0 && order.length
      ? grid.querySelector('.psc-card[data-idx="' + order[cur] + '"]') : null;
    lb.classList.remove('open');
    lbImg.src='';
    setScrollLock(false);
    if (card && !card.classList.contains('psc-hidden'))
      card.scrollIntoView({block: 'center'});
  }

  // Swipe between photographs. A drag is a swipe only when it is decisively
  // sideways: a mostly-vertical one is someone trying to dismiss or scroll,
  // and a short one is a tap that wandered.
  var tx = 0, ty = 0, tracking = false;
  lb.addEventListener('touchstart', function(e){
    if (e.touches.length !== 1) { tracking = false; return; }
    tracking = true; tx = e.touches[0].clientX; ty = e.touches[0].clientY;
  }, {passive: true});
  lb.addEventListener('touchend', function(e){
    if (!tracking || order.length < 2) return;
    tracking = false;
    var t = e.changedTouches[0];
    var dx = t.clientX - tx, dy = t.clientY - ty;
    if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    step(dx < 0 ? 1 : -1);
  }, {passive: true});
  // Backdrop closes; the photo and the arrows must not.
  lb.addEventListener('click', function(e){ if (e.target === lb) navCloseLb(); });
  lbImg.addEventListener('click', function(e){ e.stopPropagation(); });
  navPrev.addEventListener('click', function(e){ e.stopPropagation(); step(-1); });
  navNext.addEventListener('click', function(e){ e.stopPropagation(); step(1); });
  lb.querySelector('.x').addEventListener('click', function(e){
    e.stopPropagation(); navCloseLb(); });
  // overflow:hidden on html/body is not reliably honoured for wheel scrolling,
  // so the gesture is cancelled at source while the overlay is open. Needs
  // passive:false or preventDefault is ignored.
  function blockScroll(e){ if (lb.classList.contains('open')) e.preventDefault(); }
  lb.addEventListener('wheel', blockScroll, {passive: false});
  // A two-finger sideways flick on a trackpad arrives as wheel events carrying
  // deltaX. Momentum fires dozens of them, so the accumulator locks after one
  // step and only rearms once the flick has died down.
  var wacc = 0, wlock = false, wtimer = null;
  lb.addEventListener('wheel', function(e){
    if (!lb.classList.contains('open') || order.length < 2) return;
    if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;   // vertical: not ours
    clearTimeout(wtimer);
    wtimer = setTimeout(function(){ wacc = 0; wlock = false; }, 240);
    if (wlock) return;
    wacc += e.deltaX;
    if (Math.abs(wacc) < 60) return;
    var dir = wacc < 0 ? -1 : 1;
    wacc = 0; wlock = true;
    step(dir);
  }, {passive: true});
  lb.addEventListener('touchmove', blockScroll, {passive: false});
  document.addEventListener('keydown', function(e){
    if (!lb.classList.contains('open')) return;
    if (e.key === 'Escape') navCloseLb();
    else if (e.key === 'ArrowLeft')  { e.preventDefault(); step(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
    else if (e.key === 'Home') { e.preventDefault(); cur = 0; show(); writeHash('photo', false); }
    else if (e.key === 'End')  { e.preventDefault(); cur = order.length - 1; show();
                                 writeHash('photo', false); }
  });

  // ---- links to a folder or a photograph ----------------------------------
  // The address carries what is open, after the '#':
  //
  //   #folder=arches-national-park    that folder
  //   #photo=3f9c1d84b0e77265         that photograph, inside its folder
  //
  // Everything after a '#' stays in the browser - Ghost never sees it - so
  // nothing on the server has to know these exist. Opening a folder or a
  // photograph adds a history entry, so a phone's Back button closes the
  // overlay or returns to the index instead of leaving the page; stepping
  // through the overlay only rewrites the entry it is on.
  //
  // What follows the '#' is whatever somebody typed or was sent. It is only
  // ever looked up, never written into the page, and the tables it is looked
  // up in have no prototype - on an ordinary object, '#folder=constructor'
  // would find something.
  function slugify(name){
    var s = String(name);
    if (s.normalize) s = s.normalize('NFKD').replace(/[̀-ͯ]/g, '');
    s = s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    // A name with no Latin letters at all still needs a link.
    return s || encodeURIComponent(String(name).toLowerCase());
  }
  // The key a link is matched on. Encoding is normalised, so a link survives a
  // browser or a chat app re-encoding it on the way.
  function linkKey(raw){
    try { return encodeURIComponent(decodeURIComponent(raw)).toLowerCase(); }
    catch (e) { return ''; }
  }
  var SLUGS = [], SLUG_TO_G = Object.create(null), ID_TO_I = Object.create(null);
  GROUPS.forEach(function(name, gi){
    // Two folders can come down to the same words once punctuation goes;
    // the second gets a number rather than stealing the first one's link.
    var s = slugify(name), base = s, n = 2;
    while (linkKey(s) in SLUG_TO_G) s = base + '-' + (n++);
    SLUGS.push(s); SLUG_TO_G[linkKey(s)] = gi;
  });
  DATA.forEach(function(p, i){ ID_TO_I[String(p.id).toLowerCase()] = i; });

  function pageUrl(){ return location.href.split('#')[0]; }
  function currentHash(){
    if (lb.classList.contains('open') && cur >= 0 && order.length)
      return '#photo=' + DATA[order[cur]].id;
    if (view === 'folders' && openGroup >= 0) return '#folder=' + SLUGS[openGroup];
    return '';
  }
  // push: a new history entry. Otherwise the current one is rewritten, and
  // keeps knowing whether this page pushed it - which is what decides whether
  // closing something can safely go Back, or would leave the site.
  function writeHash(kind, push){
    try {
      var st = history.state, url = pageUrl() + currentHash();
      if (push) history.pushState({psc: kind, pushed: true}, '', url);
      else history.replaceState({psc: kind, pushed: !!(st && st.pushed && st.psc === kind)},
                                '', url);
    } catch (e) {}
  }
  function pushedHere(kind){
    var st = history.state;
    return !!(st && st.psc === kind && st.pushed);
  }

  function navFolder(gi){ openFolder(gi); writeHash('folder', true); }
  function navPhoto(i){ openLb(i); writeHash('photo', true); }
  // Closing goes Back when this page put the entry there, so Back and the
  // close button agree. Otherwise - a photograph opened straight from a link -
  // Back would leave the gallery, so the entry is rewritten instead.
  function navCloseLb(){
    if (pushedHere('photo')) { history.back(); return; }
    closeLb(); writeHash(openGroup >= 0 ? 'folder' : '', false);
  }
  function navCloseFolder(){
    if (pushedHere('folder')) { history.back(); return; }
    closeFolder(); writeHash('', false);
  }

  function readHash(){
    var m = /^#(folder|photo)=([^&]+)$/.exec(location.hash || '');
    return m ? {kind: m[1], key: linkKey(m[2])} : null;
  }
  function notHere(kind){
    toast(kind === 'photo' ? 'That photograph is no longer in this gallery'
                           : 'That folder is no longer in this gallery');
    try { history.replaceState(null, '', pageUrl() + currentHash()); } catch (e) {}
  }
  // first: the page has just loaded from a link. Otherwise Back or Forward
  // has moved between entries this page made.
  function followLink(first){
    var t = readHash(), lbOpen = lb.classList.contains('open');
    if (!t){
      // A hash that is not one of ours - Ghost's own #/portal/, say - is left
      // alone on arrival. Arriving back at one means leaving our states.
      if (first) return;
      if (lbOpen) closeLb();
      if (view === 'folders' && openGroup >= 0) closeFolder();
      return;
    }
    if (t.kind === 'folder'){
      if (!(t.key in SLUG_TO_G)) return notHere('folder');
      var gi = SLUG_TO_G[t.key];
      if (lbOpen) closeLb();
      // A link wins over the view this visitor last chose, without replacing
      // it: the saved choice is still there the next time they come in.
      view = 'folders';
      if (openGroup !== gi) openFolder(gi); else { syncBar(); apply(); }
      return;
    }
    if (!(t.key in ID_TO_I)) return notHere('photo');
    var i = ID_TO_I[t.key];
    // Opened inside its own folder, so the arrows walk the shoot it came from.
    // In All photos, Back and Forward stay in All photos.
    if (first || view === 'folders'){
      view = 'folders';
      if (openGroup !== DATA[i].g) openFolder(DATA[i].g);
    }
    if (lbOpen){
      order = visibleIdx(); cur = order.indexOf(i);
      if (cur < 0) { order = [i]; cur = 0; }
      show();
    } else openLb(i);
  }
  window.addEventListener('popstate', function(){ followLink(false); });

  // ---- copy or share a link -----------------------------------------------
  // On a phone the system share sheet, which is what a person on a phone
  // expects; on a computer the clipboard, which is what they expect there.
  var canShare = !!(navigator.share && window.matchMedia &&
                    matchMedia('(pointer:coarse)').matches);
  function shareLink(url, title){
    if (canShare){ navigator.share({title: title, url: url}).catch(function(){}); return; }
    function done(){ toast('Link copied'); }
    function fallback(){
      var ta = document.createElement('textarea');
      ta.value = url; ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      ta.remove();
      if (ok) done(); else toast('Copy this link: ' + url);
    }
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(url).then(done, fallback);
    else fallback();
  }
  var lbLink = lb.querySelector('.psc-link-lb');
  var crumbLink = root.querySelector('.psc-crumb .psc-link');
  [lbLink, crumbLink].forEach(function(b){ if (b && canShare) b.textContent = 'Share'; });
  if (lbLink) lbLink.addEventListener('click', function(e){
    e.stopPropagation();                   // the backdrop closes the overlay
    if (cur < 0 || !order.length) return;
    var p = DATA[order[cur]];
    shareLink(pageUrl() + '#photo=' + p.id, p.n || document.title);
  });
  if (crumbLink) crumbLink.addEventListener('click', function(){
    if (openGroup < 0) return;
    shareLink(pageUrl() + '#folder=' + SLUGS[openGroup], GROUPS[openGroup]);
  });

  apply();
  followLink(true);
})();
</script>
"""


# Grouping lives in photo_scout so the published page and the local report
# cannot drift apart on what counts as one folder. Re-exported here because
# that is the name this module has always offered.
UNFILED_GROUP = ps.UNFILED_GROUP
folder_group = ps.folder_group


def build_gallery_html(items: list[dict], tags_by_id: dict,
                       local: bool = False, bleed: bool = True,
                       max_width: int = 1800, col_width: int = 260,
                       gap: int = 8, title_size: str = "compact",
                       hearts_url: str = "", view: str = "folders",
                       folder_outline: str = DEFAULT_FOLDER_OUTLINE) -> str:
    """
    One self-contained HTML card.

    The photographs are emitted as a single JSON payload rather than a few
    hundred blocks of markup: the page stays small, the Ghost editor does not
    have to hold an enormous string, and rendering happens once in the browser.

    Note what is absent - there are no file:// links. Those are meaningless to a
    visitor and would publish your directory layout.
    """
    # Narrowed to the two words it can be before it reaches an attribute. The
    # command line already constrains it; this is for any other caller, so the
    # attribute cannot be anything but one of these whatever is passed in.
    view = "all" if view == "all" else "folders"
    # Gallery names are sent once in their own list and referred to by number.
    # Spelled out on every photograph they would be the single largest thing in
    # the payload - a few hundred repetitions of the same long folder name - and
    # the whole gallery has to fit inside one Lexical document.
    #
    # Sorted here only so the published markup is stable between runs. The page
    # decides display order for itself, with the same collator it uses for every
    # other sort, so 'Folder A-Z' and the default agree to the letter.
    groups = sorted({folder_group(it["folder"]) for it in items},
                    key=lambda s: (s.casefold(), s))
    group_index = {name: i for i, name in enumerate(groups)}

    payload = []
    for it in items:
        note_main, note_flag = ps.split_note(it["note"])
        entry = {
            "id": it["photo_id"],
            "v": it["verdict"],
            "s": it["score"],
            "n": it["filename"],
            # A date baked into the folder name is dropped from the display -
            # the capture date beside it already says when. The name as it
            # stands on disk is kept in "fr" so a search for it still works.
            "f": ps.strip_folder_date(it["folder"]),
            # Which gallery this belongs to, as an index into the list above.
            "g": group_index[folder_group(it["folder"])],
            # 'YYYY-MM-DD HH:MM:SS' (or just the date when the camera recorded
            # no clock time). One field: it sorts as plain text down to the
            # minute, and the browser renders the long form, so the payload does
            # not carry the same moment twice.
            "d": it.get("taken_at") or "",
            # '6000 x 4000 - 24.0 MP'. Pre-rendered here rather than sent as two
            # numbers: the browser would only ever format it this one way.
            "r": it.get("resolution") or "",
            # The facts, and separately any named fault, which the page
            # colours so it can be scanned rather than read.
            "note": note_main,
            **({"w": note_flag} if note_flag else {}),
            # local=True renders straight from the files beside this page, so a
            # dry run shows the real gallery rather than a grid of broken images.
            "th": (it.get("thumb_rel") or it.get("preview_rel")) if local
                  else (it.get("thumb_url") or it.get("preview_url")),
            "pv": (it.get("preview_rel") or it.get("thumb_rel")) if local
                  else (it.get("preview_url") or it.get("thumb_url")),
        }
        if it["folder"] and it["folder"] != entry["f"]:
            entry["fr"] = it["folder"]
        tl = tags_by_id.get(it["photo_id"]) or []
        if tl:
            entry["t"] = tl
        payload.append(entry)

    data_json = ps.script_json({"g": groups, "p": payload}, ensure_ascii=False)

    top = sum(1 for i in items if i["verdict"] == "TOP PICK")
    strong = sum(1 for i in items if i["verdict"] == "STRONG")

    pal = folder_palette(folder_outline)
    css = (GALLERY_CSS.replace("__MAXW__", str(max_width))
                      .replace("__COLW__", str(col_width))
                      .replace("__GAPPX__", f"{gap}px")
                      .replace("__FOLDLINE__", pal["line"])
                      .replace("__FOLDLINEHOVER__", pal["hover"])
                      .replace("__HEADCSS__", TITLE_SIZE_CSS[title_size]))
    # No endpoint means no heart buttons at all, rather than dead ones.
    hearts_attr = (f' data-hearts="{html.escape(hearts_url, quote=True)}"'
                   if hearts_url else "")
    # Which view the page opens on. The visitor's own last choice, kept in their
    # browser, wins over this - but only for a page they have already opened.
    view_class = " psc-view-folders" if view == "folders" else ""
    return (
        f'<div class="psc-wrap{" psc-bleed" if bleed else ""}{view_class}"'
        f' data-view="{view}"{hearts_attr}>'
        + css +
        '<div class="psc-bar">'
        # Folders first and leftmost: it is the way in, and on a phone the bar
        # wraps, so the control that changes the most has to be the one that
        # stays on the first line.
        '<span class="psc-views">'
        '<button class="psc-vfolders" data-view="folders" type="button" '
        'title="Browse by folder">Folders</button>'
        '<button class="psc-vall" data-view="all" type="button" '
        'title="Every photograph on one page">All photos</button>'
        '</span>'
        '<button data-band="all" class="on" type="button">All</button>'
        '<button data-band="TOP PICK" type="button">Top picks</button>'
        '<button data-band="STRONG" type="button">Strong</button>'
        + ('<button class="psc-likedonly psc-pending" type="button" '
           'title="Show only photographs someone has liked">Liked</button>'
           if hearts_url else '') +
        '<span class="psc-searchwrap">'
        '<span class="psc-chips"></span>'
        '<input class="psc-q" type="search" '
        'placeholder="search name, folder, date, rating or tag">'
        '<span class="psc-tagmenu"></span>'
        '</span>'
        '<select class="psc-sort" title="Sort order">'
        # Score first, so it is what the page opens on. The heart options sit
        # under it rather than above: with them first, a gallery published with
        # a heart service would have defaulted to Most liked.
        '<option value="score-desc">Score, highest first</option>'
        '<option value="score-asc">Score, lowest first</option>'
        + (('<option value="hearts-desc">Most liked</option>'
            '<option value="hearts-asc">Least liked</option>') if hearts_url else '') +
        '<option value="date-desc">Date, newest first</option>'
        '<option value="date-asc">Date, oldest first</option>'
        '<option value="folder-asc">Folder A-Z</option>'
        '<option value="folder-desc">Folder Z-A</option>'
        '<option value="name-asc">File name A-Z</option>'
        '<option value="name-desc">File name Z-A</option>'
        '</select>'
        '<button class="psc-save" type="button" '
        'title="Download your tags as a file">Save tags</button>'
        '<span class="psc-zoom">'
        '<button class="psc-smaller" type="button" aria-label="Smaller thumbnails" '
        'title="Smaller thumbnails, more per row">&minus;</button>'
        '<button class="psc-bigger" type="button" aria-label="Larger thumbnails" '
        'title="Larger thumbnails, fewer per row">+</button>'
        '</span>'
        f'<span class="psc-count">{top + strong} of {top + strong}</span>'
        '</div>'
        '<div class="psc-toast"></div>'
        '<div class="psc-crumb">'
        '<button class="psc-back" type="button">&#8249; All folders</button>'
        '<h3></h3><span class="psc-crumbn"></span>'
        '<button class="psc-link" type="button" '
        'title="Copy a link that opens this folder">Copy link</button>'
        '</div>'
        '<div class="psc-folders"></div>'
        '<div class="psc-empty">Nothing matches that search.</div>'
        '<div class="psc-grid"></div>'
        '<div class="psc-lb">'
        '<button class="x" type="button" aria-label="Close">&times;</button>'
        '<button class="psc-nav psc-prev" type="button" aria-label="Previous">&#8249;</button>'
        '<button class="psc-nav psc-next" type="button" aria-label="Next">&#8250;</button>'
        '<span class="psc-cap"></span><span class="psc-dims-lb"></span>'
        + ('<div class="psc-hearts psc-hearts-lb psc-pending">'
           '<button class="psc-heart" type="button" aria-pressed="false">'
           '\u2764\ufe0f</button><span class="psc-hcount"></span></div>'
           if hearts_url else '') +
        '<span class="psc-count-lb"></span>'
        '<button class="psc-link-lb" type="button" '
        'title="Copy a link that opens this photograph">Copy link</button>'
        '<img alt=""></div>'
        '<div class="psc-foot">Generated by '
        # noopener, deliberately without noreferrer: the latter would strip the
        # Referer header and the project would never see where its visitors came
        # from. noopener is the half that matters for security.
        f'<a href="{html.escape(ps.PROJECT_URL, quote=True)}" target="_blank" '
        'rel="noopener">Photo Scout</a> &middot; scores are model '
        'estimates, not verdicts &mdash; trust your eye.&trade;</div>'
        f'<script type="application/json" class="psc-data">{data_json}</script>'
        + GALLERY_JS +
        '</div>'
    )


def resolve_title(title: str, title_size: Optional[str]) -> tuple[str, str, str]:
    """
    Work out (title to store in Ghost, heading treatment, what to tell the user).

    A blank --title means "no heading on the page". Ghost still needs a name to
    file the page under, so UNTITLED_GHOST_TITLE stands in and the heading is
    hidden instead. Passing --title-size explicitly overrides that, which is the
    way to see the stand-in if you ever want it.
    """
    title = (title or "").strip()
    if title:
        return title, (title_size or "compact"), ""
    return UNTITLED_GHOST_TITLE, (title_size or "hide"), (
        f"No --title given, so the page heading is hidden. Ghost will list this "
        f"page as '{UNTITLED_GHOST_TITLE}'.")


def lexical_with_html_card(inner_html: str) -> str:
    """
    Wrap the gallery in a Lexical document containing a single HTML card.

    Ghost 6's Admin API accepts Lexical only, so raw HTML is delivered as an
    'html' card node rather than as a post body.
    """
    doc = {
        "root": {
            "children": [
                {"type": "html", "version": 1, "html": inner_html}
            ],
            "direction": None, "format": "", "indent": 0,
            "type": "root", "version": 1,
        }
    }
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def resolve_tags(out_dir: Path, items: list[dict]) -> dict:
    """
    Re-key tags.json (which is keyed by absolute local path) onto photo_id.

    The browser stores tags against the path recorded in scores.sqlite3. Here we
    translate to the public identifier so the published page never carries a
    local path.
    """
    raw = load_tags(out_dir)
    if not raw:
        return {}
    by_id: dict = {}
    # Build a lookup from the local path key back to the photo_id.
    conn = None
    for it in items:
        by_id.setdefault(it["photo_id"], [])
    # tags.json keys are the scores.sqlite3 'path' values.
    path_to_id = {}
    for it in items:
        path_to_id[it["rel_path"]] = it["photo_id"]
    for key, tags in raw.items():
        if not isinstance(tags, list):
            continue
        pid = photo_id_for(key)
        # Try the key directly, then as a suffix match against known rel_paths.
        if pid in by_id:
            by_id[pid] = sorted({str(t) for t in tags})
            continue
        norm = key.replace("\\", "/").lower()
        for rel, rid in path_to_id.items():
            if norm.endswith(rel.replace("\\", "/").lower()):
                by_id[rid] = sorted({str(t) for t in tags})
                break
    return {k: v for k, v in by_id.items() if v}


def merge_web_tags(path: Path, tags_by_id: dict, items: list[dict]) -> dict:
    """
    Fold in a psc-web-tags.json downloaded from the published page.

    That file is already keyed by photo_id - the page never sees a local path -
    so it needs no translation, only validation. It WINS over tags.json for any
    photograph it mentions, because it is the more recent edit.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        ps.log(f"Could not read {path.name} ({exc}) - continuing without it.")
        return tags_by_id
    if not isinstance(raw, dict):
        ps.log(f"{path.name} is not a JSON object - ignoring it.")
        return tags_by_id
    known = {it["photo_id"] for it in items}
    merged = dict(tags_by_id)
    used = unknown = 0
    for pid, tags in raw.items():
        if not isinstance(tags, list):
            continue
        # Same character rule the browser enforces, applied again here: never
        # trust a file that has been outside the program.
        clean = sorted({re.sub(r"[^A-Za-z0-9 _-]", "", str(t)).strip()[:40]
                        for t in tags})
        clean = [t for t in clean if t]
        if pid not in known:
            unknown += 1
            continue
        if clean:
            merged[pid] = clean
            used += 1
        else:
            merged.pop(pid, None)
    ps.log(f"Web tags: {used} photographs from {path.name}"
           + (f" ({unknown} no longer in the report)" if unknown else ""))
    return merged


def register_hearts(args, items: list[dict]) -> None:
    """
    Tell the heart service which photographs exist.

    Without this the service rejects every heart, because it will not accept an
    identifier it has never been told about. Failure here is reported but never
    fatal: a gallery that publishes with hearts temporarily unavailable is far
    better than one that does not publish.
    """
    token = args.hearts_token or os.environ.get("HEARTS_ADMIN_TOKEN")
    if not token:
        ps.log("")
        ps.log("!" * 66)
        ps.log("WARNING: no heart admin token, so the allowlist was NOT updated.")
        ps.log("         The heart buttons will appear and every click will fail")
        ps.log("         with 'unknown photo_id'. Set HEARTS_ADMIN_TOKEN (or pass")
        ps.log("         --hearts-token) and run again - the allowlist alone can")
        ps.log("         be fixed without republishing:")
        ps.log("")
        ps.log("             photo_scout_ghost.py --site ... --hearts-url /api/hearts \\")
        ps.log("                 --hearts-token <token> --hearts-register-only")
        ps.log("!" * 66)
        ps.log("")
        return

    base = args.hearts_url
    if base.startswith("/"):
        base = args.site.rstrip("/") + base
    url = base.rstrip("/") + "/_photos"
    body = json.dumps({"photos": [{"photo_id": it["photo_id"],
                                   "rel_path": it["rel_path"]} for it in items]}
                      ).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Admin-Token": token,
        "User-Agent": args.user_agent,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            out = json.loads(resp.read().decode())
        ps.log(f"Hearts: {out.get('registered', 0)} photographs registered, "
               f"{out.get('known_photos', '?')} known to the service")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        ps.log(f"WARNING: could not update the heart allowlist "
               f"({exc.code}) {detail}")
        if exc.code == 401:
            ps.log("         The admin token was rejected. Check "
                   "HEARTS_ADMIN_TOKEN matches the one the service was started with.")
    except Exception as exc:
        ps.log(f"WARNING: could not reach the heart service at {url}: {exc}")
        ps.log("         The gallery will publish; hearts will be absent until "
               "the service is reachable.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True,
                    help="Public site URL, e.g. https://example.com. Used for the "
                         "final link; image paths are stored site-relative.")
    ap.add_argument("--admin-url", metavar="URL",
                    help="Admin API base if it differs from --site, e.g. "
                         "https://admin.example.com. Shown in Ghost as the API URL "
                         "on the integration page. Defaults to --site.")
    ap.add_argument("--key", metavar="ID:SECRET",
                    help="Ghost Admin API key, 89 characters shaped "
                         "'<24-hex id>:<64-hex secret>', from Ghost admin -> "
                         "Settings -> Advanced -> Integrations. NOT the Content "
                         "API key. Set GHOST_ADMIN_KEY instead to keep it out of "
                         "your shell history. Not needed for --dry-run.")
    ap.add_argument("--out", help="photo_scout output directory "
                                  "(default: _photo_scout beside this script)")
    ap.add_argument("--slug", default=DEFAULT_SLUG, help=f"Page slug (default {DEFAULT_SLUG})")
    ap.add_argument("--title", default=DEFAULT_TITLE,
                    help="Heading shown above the gallery. Blank by default, "
                         "which also collapses the theme's heading band so the "
                         "photographs start at the top of the page.")
    ap.add_argument("--status", choices=("draft", "published"), default="draft",
                    help="Publish state of the Ghost page (default draft, so you "
                         "can look before it goes live)")
    ap.add_argument("--manifest", metavar="FILE",
                    help="Upload manifest location (default: publish.sqlite3 beside "
                         "this script). Must NOT live inside the photo_scout output "
                         "directory, which --reset deletes.")
    ap.add_argument("--width", choices=("full", "content"), default="full",
                    help="'full' breaks the grid out of the theme's narrow content "
                         "column and scales it to the browser window (default). "
                         "'content' leaves it inside the column.")
    ap.add_argument("--max-width", type=int, default=1800, metavar="PX",
                    help="Cap on the grid's width in full mode (default 1800)")
    ap.add_argument("--column-width", type=int, default=260, metavar="PX",
                    help="Minimum width of a grid column; smaller means more "
                         "columns (default 260)")
    ap.add_argument("--gap", type=int, default=8, metavar="PX",
                    help="Space above and below the gallery within the host page "
                         "(default 8). Themes often add a large margin here; this "
                         "replaces it. Negative values are allowed, and tuck the "
                         "gallery up closer to the page title.")
    ap.add_argument("--title-size", choices=("compact", "keep", "hide"),
                    default=None,
                    help="What to do with the host page's own title block. "
                         "compact trims the theme's oversized heading band down "
                         "to something the gallery sits under; keep leaves the "
                         "theme alone; hide removes it entirely. Defaults to "
                         "hide when --title is blank, compact otherwise.")
    ap.add_argument("--user-agent", metavar="STRING", default=USER_AGENT,
                    help="Override the User-Agent sent to Ghost. Only needed if a "
                         "CDN in front of your site is fussy about it.")
    ap.add_argument("--web-tags", metavar="FILE",
                    help="A psc-web-tags.json saved from the published page. "
                         "Its tags are baked into the next publish, so what you "
                         "tagged in the browser becomes the default for everyone.")
    ap.add_argument("--hearts-url", metavar="PATH", default="",
                    help="Where the heart service lives, e.g. /api/hearts. "
                         "Same-origin path is strongly preferred - no CORS, no "
                         "second certificate. Omit it and no heart buttons are "
                         "rendered at all.")
    ap.add_argument("--hearts-register-only", action="store_true",
                    help="Register the heart allowlist and stop. Nothing is "
                         "uploaded and the Ghost page is not touched. Use this "
                         "when the buttons are there but clicking one says "
                         "'unknown photo_id'.")
    ap.add_argument("--hearts-token", metavar="TOKEN",
                    help="Admin token for the heart service, so this script can "
                         "register which photographs may be hearted. Or set "
                         "HEARTS_ADMIN_TOKEN.")
    ap.add_argument("--folder-outline", metavar="HEX",
                    default=DEFAULT_FOLDER_OUTLINE,
                    help=f"Outline colour of the folder tiles on the index, as "
                         f"a hex value like {DEFAULT_FOLDER_OUTLINE}. The tiles "
                         f"keep the page's own background; only the folder "
                         f"shape is coloured. A colour too dark to see against "
                         f"that background is lifted until it is.")
    ap.add_argument("--view", choices=("folders", "all"), default="folders",
                    help="Which view the gallery opens on. 'folders' shows an "
                         "index of folders to pick from; 'all' puts every "
                         "photograph on one page. A visitor can switch either "
                         "way, and their choice is remembered.")
    ap.add_argument("--insecure-http", action="store_true",
                    help="Allow a plain http:// site or heart endpoint. Off by "
                         "default: it sends the Admin API key in the clear.")
    ap.add_argument("--limit", type=int, help="Only publish the top N photographs")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build everything and write the HTML locally, but upload "
                         "nothing and touch nothing in Ghost")
    ap.add_argument("--emit-html", metavar="FILE",
                    help="Also write the generated gallery to a local file for inspection")
    args = ap.parse_args(argv)

    # Written into the page's stylesheet, so it is checked before anything is
    # built or uploaded. folder_palette rebuilds the string from parsed
    # numbers, so a value carrying CSS of its own cannot survive the trip -
    # this is only here so a typo reads as a sentence rather than a traceback.
    if not ps.outline_ok(args.folder_outline):
        ps.log(f"ERROR: --folder-outline {args.folder_outline!r} is not a colour.")
        ps.log("       Wanted a hex value like #b09468 or #b96, and nothing else.")
        return 2

    out_dir = Path(args.out).expanduser().resolve() if args.out else ps.DEFAULT_OUT_DIR
    scores = out_dir / "scores.sqlite3"
    if not scores.exists():
        ps.log(f"ERROR: no scores database at {scores}")
        ps.log("       Run photo_scout.py first, or pass --out.")
        return 2

    admin_key = args.key or os.environ.get("GHOST_ADMIN_KEY")
    # --hearts-register-only talks to the heart service and nothing else, so a
    # Ghost key would be pointless ceremony.
    if not admin_key and not args.dry_run and not args.hearts_register_only:
        ps.log("ERROR: an Admin API key is required (--key or GHOST_ADMIN_KEY).")
        ps.log("       Ghost admin -> Settings -> Advanced -> Integrations -> Add")
        ps.log("       custom integration, then copy the ADMIN API key.")
        ps.log("       bash/zsh    export GHOST_ADMIN_KEY='<id>:<secret>'")
        # Named explicitly because 'export' fails silently in PowerShell: the
        # variable is never set and the next run reports this same error.
        ps.log("       PowerShell  $env:GHOST_ADMIN_KEY = \"<id>:<secret>\"")
        ps.log("       Either way it lasts only for that terminal session.")
        ps.log("       Use --dry-run to preview without a key at all.")
        return 2

    # Fail on a malformed key now, rather than after uploading nothing and
    # surfacing a raw ValueError traceback from deep in the request path.
    if admin_key:
        try:
            ghost_jwt(admin_key)
        except ValueError as exc:
            ps.log(f"ERROR: {exc}")
            ps.log("       Wanted: the ADMIN API key, 89 characters, one colon,")
            ps.log("       shaped '<24-hex id>:<64-hex secret>'.")
            ps.log(f"       Got:    {len(admin_key)} characters, "
                   f"{admin_key.count(':')} colon(s).")
            # The two mistakes this catches in practice. Neither is obvious from
            # the key alone, and both look plausible in the Ghost admin panel.
            if ":" not in admin_key and len(admin_key) == 26:
                ps.log("       That looks like the CONTENT API key, which is "
                       "read-only and cannot upload.")
            elif ":" not in admin_key and len(admin_key) == 64:
                ps.log("       That looks like the secret half on its own - the "
                       "'<id>:' prefix is missing.")
            return 2

    admin_url = (args.admin_url or args.site).rstrip("/")

    # Refuse before anything is sent, not after. Both of these carry a
    # credential that grants write access.
    unsafe = []
    if not args.dry_run and not args.hearts_register_only \
            and plaintext_endpoint(admin_url):
        unsafe.append(("the Ghost Admin API key", admin_url))
    hearts_base = args.hearts_url
    if hearts_base.startswith("/"):
        hearts_base = args.site.rstrip("/") + hearts_base
    if hearts_base and (args.hearts_token or os.environ.get("HEARTS_ADMIN_TOKEN")) \
            and plaintext_endpoint(hearts_base):
        unsafe.append(("the heart admin token", hearts_base))
    if unsafe and not args.insecure_http:
        for what, where in unsafe:
            ps.log(f"ERROR: {where} is plain http, and publishing would send "
                   f"{what} over it in the clear.")
        ps.log("       Use https, or --insecure-http if you are certain the "
               "network in between is yours.")
        return 2
    if unsafe:
        for _, where in unsafe:
            ps.log(f"WARNING: sending a credential in the clear to {where} "
                   "(--insecure-http)")

    ps.log(f"Site:   {args.site}")
    if admin_url != args.site.rstrip("/"):
        ps.log(f"Admin:  {admin_url}")
    ps.log(f"Output: {out_dir}")
    if args.dry_run:
        ps.log("DRY RUN - nothing will be uploaded or changed in Ghost")

    items = load_shortlist(scores, out_dir, Path("."))
    if args.limit:
        items = items[: args.limit]
    if not items:
        ps.log("Nothing to publish: no TOP PICK or STRONG photographs in the database.")
        ps.log("If the library is fully scored, the bands may need fitting - "
               "run photo_scout.py --calibrate.")
        return 1

    top = sum(1 for i in items if i["verdict"] == "TOP PICK")
    ps.log(f"Publishing {len(items)} photographs ({top} top picks, {len(items)-top} strong)")

    # Fixing only the allowlist: no uploads, no Ghost call, no page rewritten.
    # Republishing an entire gallery to correct one list is heavy-handed, and
    # this is the failure people actually hit.
    if args.hearts_register_only:
        if not args.hearts_url:
            ps.log("ERROR: --hearts-register-only needs --hearts-url too, so it "
                   "knows where the service is.")
            return 2
        register_hearts(args, items)
        ps.log("Allowlist updated. The Ghost page was not touched.")
        return 0

    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest \
        else HERE / PUBLISH_DB
    if manifest_path.parent == out_dir:
        ps.log(f"WARNING: the manifest at {manifest_path} sits inside the photo_scout")
        ps.log("         output directory, which `photo_scout.py --reset` deletes.")
        ps.log("         Losing it means re-uploading every image as a duplicate.")
    manifest = Manifest(manifest_path)
    ps.log(f"Manifest: {manifest_path} ({manifest.count()} images already uploaded)")
    client = None if args.dry_run else GhostClient(admin_url, admin_key,
                                                   user_agent=args.user_agent)

    stats = publish_images(client, manifest, items, args.dry_run)
    if stats["would_upload"]:
        ps.log(f"Images: {stats['would_upload']} would be uploaded, "
               f"{stats['reused']} already in Ghost"
               + (f", {stats['missing']} missing locally" if stats["missing"] else ""))
    else:
        ps.log(f"Images: {stats['uploaded']} uploaded, {stats['reused']} already in Ghost"
               + (f", {stats['missing']} missing locally" if stats["missing"] else ""))
    if stats["bytes"]:
        ps.log(f"        {stats['bytes']/1e6:.1f} MB transferred")

    tags_by_id = resolve_tags(out_dir, items)
    if args.web_tags:
        wt = Path(args.web_tags).expanduser()
        if not wt.exists():
            ps.log(f"ERROR: no such file: {wt}")
            return 2
        tags_by_id = merge_web_tags(wt, tags_by_id, items)
    if tags_by_id:
        ps.log(f"Tags:   {sum(len(v) for v in tags_by_id.values())} across "
               f"{len(tags_by_id)} photographs")

    if args.hearts_url:
        if args.dry_run:
            # Never contact the service during a dry run - but do say plainly
            # what a real run would or would not do.
            if args.hearts_token or os.environ.get("HEARTS_ADMIN_TOKEN"):
                ps.log(f"Hearts: {len(items)} photographs would be registered "
                       f"at {args.hearts_url}")
            else:
                register_hearts(args, items)     # emits the warning and returns
        else:
            register_hearts(args, items)

    page_title, title_size, title_note = resolve_title(args.title, args.title_size)
    if title_note:
        ps.log(f"Title:  {title_note}")

    gallery = build_gallery_html(items, tags_by_id, bleed=(args.width == "full"),
                                 max_width=args.max_width, col_width=args.column_width,
                                 gap=args.gap, title_size=title_size,
                                 hearts_url=args.hearts_url or "",
                                 view=args.view,
                                 folder_outline=args.folder_outline)
    ps.log(f"Gallery: {len(gallery)/1024:.0f} KB of markup")

    if args.emit_html or args.dry_run:
        # The local page always renders from the files on disk, so it must live
        # in out_dir for thumbs/ and previews/ to resolve beside it.
        dest = Path(args.emit_html) if args.emit_html else out_dir / "ghost_preview.html"
        if dest.parent.resolve() != out_dir.resolve():
            ps.log(f"NOTE: {dest.name} must sit in {out_dir} for its images to "
                   f"resolve; writing it there instead.")
            dest = out_dir / dest.name
        local_gallery = build_gallery_html(items, tags_by_id, local=True,
                                           bleed=(args.width == "full"),
                                           max_width=args.max_width,
                                           col_width=args.column_width,
                                           gap=args.gap,
                                           title_size=title_size,
                                           hearts_url=args.hearts_url or "",
                                           view=args.view,
                                           folder_outline=args.folder_outline)
        dest.write_text(
            "<!doctype html><meta charset='utf-8'>"
            # Ghost's own theme supplies this on the published page. The
            # standalone preview needs its own, or a phone lays it out at 980px
            # and the grid never reflows.
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Ghost gallery preview</title>"
            "<body style='margin:0;background:#111'>" + local_gallery,
            encoding="utf-8")
        ps.log(f"Preview: {dest}")
        ps.log("         renders from your local thumbs/ and previews/ - the "
               "published page looks identical, served by Ghost")

    if args.dry_run:
        ps.log("Dry run complete. Re-run with --key to publish.")
        return 0

    lexical = lexical_with_html_card(gallery)
    existing = client.find_page(args.slug)
    if existing:
        page = client.update_page(existing, page_title, lexical, args.status)
        ps.log(f"Updated existing page /{page['slug']}/ ({page['status']})")
    else:
        page = client.create_page(args.slug, page_title, lexical, args.status)
        ps.log(f"Created page /{page['slug']}/ ({page['status']})")

    ps.log(f"View: {args.site.rstrip('/')}/{page['slug']}/")
    if page["status"] == "draft":
        ps.log("      (draft - publish it from Ghost admin when you're happy)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhostError as exc:
        ps.log(f"Ghost API error:\n{exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        ps.log("Interrupted. Uploads already recorded in the manifest are kept.")
        sys.exit(130)
