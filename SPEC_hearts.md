# Heart service — specification

A minimal public counter for "which photographs do visitors like." Runs beside
Ghost on the same host, serves one JSON API, stores nothing but hearts.

**Status: BUILT.** The service is `hearts/app.py`, the gallery side is in
`photo_scout_ghost.py`, and `DEPLOY_hearts.md` walks through installing it.
Tested by `_selftest_hearts.py` and `_selftest_hearts_browser.py`.

---

## 1. Scope

**In scope**

- Anonymous visitors heart / un-heart individual photographs in the gallery
- Aggregate counts readable by the gallery and by you
- A ranked "most hearted" view for your own use

**Explicitly out of scope**

- Accounts, logins, or Ghost Members integration
- Tags — those stay browser-side, per your decision
- Comments, sharing, any other social feature
- Precise or tamper-proof counting (see §6 — this is a popularity signal, not a ballot)

**Design constraint carried from the rest of the project:** if this service is
down or unreachable, the gallery must still render perfectly. Hearts are an
enhancement, never a dependency.

---

## 2. Why a service at all

Tags live in the browser because they are one person's private annotations.
Hearts are an aggregate across strangers — inherently shared state, which cannot
live in visitors' browsers. Ghost has no writable per-image store (its Audience
Feedback, ActivityPub likes and comments are all post-scoped), so a small service
is the only option. It is, however, genuinely small.

---

## 3. Photo identity

The gallery needs a stable identifier per photograph that is safe to expose
publicly.

```
photo_id = sha256(relative_path_using_forward_slashes).hexdigest()[:16]
```

Example: `2011-06-28 Yellowstone/DSC_0989.NEF`
→ `a3f9c1d84b0e7726`

**Why relative, not absolute** — an absolute path leaks your filesystem layout to
every visitor and breaks the moment the library moves or is served from another
machine. The relative path is stable across machines.

**Why not the Ghost image URL** — it changes if an image is ever re-uploaded,
which would silently orphan every heart on that photo.

**Why not the perceptual hash** — near-duplicates hash close together by design;
that is exactly what we do *not* want for identity.

**Why 16 hex characters** — 64 bits. For a few thousand photographs, collision
probability is negligible, and it keeps the markup compact.

The publish script computes these and embeds them in the gallery as
`data-photo-id`. It also writes the full set to the service (§5.5) so unknown IDs
can be rejected.

---

## 4. Data model

A single SQLite file, `hearts.sqlite3`, in WAL mode. **Never** Ghost's MySQL
database — that schema belongs to Ghost and changes on upgrade.

```sql
PRAGMA journal_mode = WAL;

-- One row per (photograph, voter). Counts are derived, never stored, so a
-- toggle can never drift out of sync with a counter column.
CREATE TABLE hearts (
    photo_id   TEXT    NOT NULL,
    voter      TEXT    NOT NULL,   -- salted hash, see §6.1
    created_at INTEGER NOT NULL,   -- unix seconds
    PRIMARY KEY (photo_id, voter)
);
CREATE INDEX idx_hearts_photo ON hearts(photo_id);

-- Allowlist of photographs that may be hearted. Populated at publish time.
-- Without this, anyone can POST arbitrary IDs and bloat the database.
CREATE TABLE photos (
    photo_id  TEXT PRIMARY KEY,
    rel_path  TEXT NOT NULL,       -- for your reference only, never served
    added_at  INTEGER NOT NULL
);

-- Per-voter throttle, coarse. Rows older than the window are pruned.
CREATE TABLE actions (
    voter      TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_actions ON actions(voter, created_at);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- meta rows: 'salt' (generated once on first run), 'schema_version'
```

Storing individual rows rather than an integer counter costs nothing at this
scale and buys three things: toggling works correctly, dedupe is enforced by the
primary key rather than by application logic, and counts can always be recomputed
if anything looks wrong.

Expected size: a few hundred photographs and a few thousand hearts is well under
a megabyte.

---

## 5. API

Base path `/api/hearts`, served same-origin from the site — no CORS, no
second port, no certificate of its own.

All responses are JSON. All errors use the shape `{"error": "message"}`.

### 5.1 `GET /api/hearts`

Returns every count, plus which ones this browser has hearted.

Request header (optional): `X-Heart-Token: <uuid>`

```json
{
  "counts": { "a3f9c1d84b0e7726": 42, "7b21e0c9aa145d3f": 7 },
  "mine":   [ "a3f9c1d84b0e7726" ],
  "total":  49
}
```

Photographs with zero hearts are omitted. Without a token, `mine` is `[]`.

### 5.2 `POST /api/hearts/<photo_id>`

Adds this voter's heart. Idempotent — hearting twice is not an error and does not
double-count.

Request header (required): `X-Heart-Token: <uuid>`

```json
{ "photo_id": "a3f9c1d84b0e7726", "count": 43, "hearted": true }
```

### 5.3 `DELETE /api/hearts/<photo_id>`

Removes it. Also idempotent.

```json
{ "photo_id": "a3f9c1d84b0e7726", "count": 42, "hearted": false }
```

### 5.4 `GET /api/hearts/top?limit=50`

Ranked, for the site owner's review. Includes `rel_path` so results are legible.

```json
{ "top": [ { "photo_id": "a3f9…", "rel_path": "2011-06-28 - Wyoming…/DSC_0989.NEF", "count": 42 } ] }
```

**Decision needed** (§10): whether this endpoint is public or authenticated.
It exposes relative paths, which the public endpoints deliberately do not. It exposes relative paths, which the
public endpoints deliberately do not.

### 5.5 `POST /api/hearts/_photos` — admin, authenticated

Called by the publish script to register the allowlist. Requires the admin token (§5.6).

```json
{ "photos": [ { "photo_id": "a3f9…", "rel_path": "…/DSC_0989.NEF" } ] }
```

Additive by default. Removing a photograph from the gallery does **not** delete
its hearts — they stay, dormant, in case it returns.

### 5.6 Status codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 400 | Malformed `photo_id`, or missing token on a write |
| 401 | Failed basic auth on an admin endpoint |
| 404 | `photo_id` not in the allowlist |
| 429 | Rate limited |
| 503 | Database unavailable |

### 5.7 Validation

`photo_id` must match `^[0-9a-f]{16}$`, checked before any database access. It
must then exist in `photos`. Combined, these mean the only values ever reaching
SQL are 16-character hex strings drawn from a known set.

All queries use bound parameters. No string interpolation into SQL, ever.

---

## 6. Abuse handling

The counting is trivial. This section is the actual work.

### 6.1 Voter identity

The browser generates a UUIDv4 on first visit and keeps it in `localStorage`.
It is sent as `X-Heart-Token`.

Stored as `voter = sha256(salt + token)`, where `salt` is generated once on first
run and kept in `meta`. The raw token never touches disk, so the database cannot
be used to correlate a visitor across anything else.

**IP addresses are never stored**, not even hashed, as identity. They are used
only transiently for rate limiting (§6.3).

This is deliberately weak identity. Clearing site data yields a fresh token and a
second heart. That is an acceptable trade for demanding nothing of visitors.

### 6.2 Toggle, not increment

Hearting is a set-membership operation, not a counter bump. Holding down the
button does nothing after the first click. Counts therefore approximate *distinct
browsers*, which is a far more meaningful signal than clicks.

### 6.3 Rate limiting, two layers

**nginx**, by IP, before the request reaches Python:

```nginx
limit_req_zone $binary_remote_addr zone=hearts:10m rate=30r/m;
```

**Application**, by voter token: at most 60 write actions per hour, enforced via
the `actions` table. Catches a single token scripted across many IPs.

### 6.4 Residual risk, stated plainly

A determined person with a script and a proxy pool can inflate counts. Defending
properly would mean CAPTCHAs or accounts, which would cost more in visitor
friction than the data is worth. The mitigations above stop casual gaming and
accidental double-counting, which is the realistic threat for a personal
photography site.

### 6.5 Visible or hidden counts

**Recommendation: hidden.** Render the heart as a filled/unfilled outline so a
visitor can see their own choice, but do not show the tally publicly. A visible
leaderboard is an invitation to game it; a private one removes nearly all the
incentive while giving you the same data through `/api/hearts/top`.

Easy to reverse later — it is a rendering decision, not a schema one.

---

## 7. Deployment

Ghost 6 self-hosted runs on Docker Compose, Ubuntu 24, Node 22 and MySQL 8, so a
sidecar container is the natural shape.

### 7.1 Compose service

```yaml
  hearts:
    build: ./hearts
    restart: unless-stopped
    ports:
      - "127.0.0.1:8091:8091"     # localhost only, never 0.0.0.0
    volumes:
      - ./hearts-data:/data       # hearts.sqlite3 lives here
    environment:
      - HEARTS_DB=/data/hearts.sqlite3
    read_only: true               # the image itself is immutable
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
```

Waitress inside the container, bound to `0.0.0.0:8091` within its own namespace,
published only on the host's loopback.

### 7.2 nginx

```nginx
limit_req_zone $binary_remote_addr zone=hearts:10m rate=30r/m;

location /api/hearts {
    limit_req zone=hearts burst=10 nodelay;
    client_max_body_size 16k;          # nothing here is large
    proxy_pass http://127.0.0.1:8091;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
}

location /api/hearts/_photos {
    auth_basic "photo-scout admin";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8091;
}
```

Everything else on the domain continues to reach Ghost untouched.

### 7.3 If you would rather not use Docker

A systemd unit with `DynamicUser=yes`, `ProtectSystem=strict`,
`ReadWritePaths=/var/lib/hearts`, `PrivateTmp=yes`, `NoNewPrivileges=yes` gives
comparable isolation. Dependencies are `python3-flask` and `python3-waitress`,
both in the Ubuntu archive.

---

## 8. Client integration

The gallery already renders one card per photograph. Each gains a heart button
carrying `data-photo-id`.

On load: one `GET /api/hearts` populates every button's state.

On click: optimistic toggle in the UI, then `POST` or `DELETE`. If the request
fails, revert the button and show the existing toast. No spinner — the round trip
is a few milliseconds on the same host.

**Degradation is the important part.** The whole block is wrapped so that any
failure — service down, network blocked, adblocker — leaves the gallery fully
functional with the hearts simply absent. This is the same discipline used for
`localStorage` in the current report.

Because the service is same-origin, there is no CORS preflight and no cookie or
credential handling.

---

## 9. Operations

**Backup** — `sqlite3 hearts.sqlite3 ".backup /backups/hearts-$(date +%F).sqlite3"`
on a daily timer. The file is small enough to keep months of copies.

**Retention** — hearts are kept indefinitely. `actions` rows are pruned beyond
the rate-limit window on each write; no separate job needed.

**Schema changes** — `meta.schema_version`, migrated in place on startup, the
same pattern already used in `photo_scout.py`'s `Cache._migrate()`.

**Monitoring** — `GET /api/hearts` returning 200 is a sufficient health check.
Docker `healthcheck` or a cron `curl` that emails on failure.

**Republishing the gallery does not touch hearts.** They key on `photo_id`, which
is derived from the relative path, so re-running the publish script — or
`photo_scout.py --reset` and a full rescore — leaves every heart intact. This is
the same principle as keeping tags out of `scores.sqlite3`.

---

## 10. Decisions made

1. **Counts are visible to visitors.** Chosen over hidden; §6.5
   argued the other way, and the trade is real - a public tally is an
   invitation to inflate it - but the counting was never meant to be
   authoritative, and showing the number is what makes the button feel worth
   pressing. Reversing it later is a rendering change, not a schema one.
2. **`/api/hearts/top` is authenticated.** It exposes folder names.
3. **Docker sidecar**, to match how Ghost already runs.
4. **Port 8091**, published on the host's loopback only.

One thing implemented differently from this document: authentication for the
admin endpoints is a token the SERVICE checks (`HEARTS_ADMIN_TOKEN`), accepted
either as `X-Admin-Token` or as the password half of HTTP Basic. §5.5 and §7.2
originally put that job on nginx alone. Moving it into the application means a
mistake in the nginx configuration cannot silently open the endpoint, and one
credential covers both a script and a browser. The nginx `auth_basic` block is
still supplied, commented out, as a second layer if wanted.

---

## 11. Test plan

Following the project's existing norm — prove it, don't assert it.

**API suite** — every endpoint and status code; malformed IDs (`../`, SQL
fragments, oversized strings, non-hex); unknown IDs rejected with 404; writes
without a token rejected with 400; idempotency of double POST and double DELETE;
counts recomputed correctly after a toggle sequence.

**Abuse suite** — the per-voter hourly cap actually blocks; two tokens count as
two hearts; the same token twice counts as one; raw tokens and IP addresses
appear nowhere in the database file (grep the raw bytes).

**Persistence suite** — hearts survive a service restart; hearts survive a
`photo_scout.py --reset` and full rescore; removing a photograph from the
allowlist preserves its hearts.

**Browser suite** — Playwright, as with the tag UI: clicking hearts, state
restored on reload, and — most importantly — **the gallery renders correctly with
the service stopped**.

---

## 12. Effort

Roughly 150 lines of Python for the service, 40 lines of JavaScript in the
gallery, plus the nginx and Compose fragments. The tests will be longer than the
implementation, which has been true of everything else in this project and has
repeatedly been worth it.
