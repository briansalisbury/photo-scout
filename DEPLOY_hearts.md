# Deploying the heart service

Everything here happens on the server that runs Ghost. Nothing touches your photo
library except the final publish command, which reads it.

Roughly fifteen minutes, most of it waiting for Docker to build.

---

## What you are installing

A single small Python service that answers four URLs and stores one thing:
which anonymous browsers liked which photographs. It runs in its own container
beside Ghost, listens only on localhost, and nginx passes `/api/hearts` through
to it. Everything else on the domain continues to reach Ghost untouched.

It stores no IP addresses, no accounts, and no raw visitor identifiers.

**If it is down, the gallery is completely unaffected** — the heart buttons
simply do not appear. That is tested, not hoped for.

---

## What it costs your server

Sized for a small VPS - the reference deployment is a 2 GB, single-core box
shared with Ghost and MySQL.

| | |
|---|---|
| Memory, steady state | ~40 MB resident |
| Memory, hard ceiling | 160 MB, enforced by Compose |
| CPU | capped at half a core; idle between requests |
| Disk | ~150 MB image, under 1 MB of data |
| Per page view | one request, one indexed SQLite read, ~1 ms |
| Per click | one request, one row inserted or deleted |

Hundreds of visitors a day is roughly hundreds of requests a day. This is far
below anything the box would notice — Ghost serving one page does more work
than the heart service does in a day.

Three deliberate choices for a small box:

- **Four worker threads**, not the usual eight or sixteen. On one core, extra
  threads add memory and context switching without adding throughput.
- **A hard 160 MB memory limit** in the Compose file. If the service ever
  misbehaves, Docker kills it and Ghost keeps running — far better than the two
  of them fighting over RAM and the box starting to swap.
- **A short accept queue.** Under a sudden burst the service refuses quickly
  rather than queueing indefinitely, which is what keeps a spike from turning
  into swap pressure for Ghost.

Confirm the real numbers once it is running:

```bash
docker stats --no-stream hearts
```

If steady-state memory is much above 60 MB, tell me — that would mean something
is holding data it should not.

---

## 1. Copy the files up

From your Windows machine, copy the `hearts` folder to wherever
`docker-compose.yml` lives on the server:

```bash
scp -r ./hearts you@your-server:/path/to/ghost/
```

You should end up with `hearts/` sitting next to `docker-compose.yml`.

---

## 2. Make the admin token

On the server, in the same folder as `docker-compose.yml`:

```bash
echo "HEARTS_ADMIN_TOKEN=$(openssl rand -hex 32)" >> .env
chmod 600 .env
grep HEARTS_ADMIN_TOKEN .env
```

Copy that value somewhere safe — you need it on the Windows side to publish.
It is the only credential the service has, and it guards two things: the ranked
"most hearted" list, and the ability to say which photographs may be hearted.

---

## 3. Add the service to Docker Compose

Open `docker-compose.yml` and paste in the `hearts:` block from
`hearts/docker-compose.hearts.yml`, under the existing `services:` key,
alongside Ghost and MySQL. Watch the indentation — YAML cares.

Now create the data directory and give it to the container's user. **Do not
skip this.** Docker creates a bind-mounted directory owned by `root`, and this
container deliberately runs unprivileged as uid 10001, so without the change of
ownership the service cannot create its database and will restart forever:

```bash
mkdir -p hearts-data
sudo chown -R 10001:10001 hearts-data
```

Then:

```bash
docker compose up -d --build hearts
docker compose ps hearts
curl -s localhost:8091/api/hearts/_health
```

That last command should print `{"ok":true,"schema_version":1}`.

**Check it is not exposed publicly.** From another machine:

```bash
curl https://example.com:8091/api/hearts/_health
```

That must FAIL to connect. If it answers, the `127.0.0.1:` prefix is missing
from the `ports:` line and the service is open to the internet — fix that
before going further.

---

## 4. Route /api/hearts to it

Your stack puts **Caddy** in front of Ghost, in its own container. So the route
goes in the Caddyfile, and the upstream is `hearts:8091` over the Docker
network — *not* `127.0.0.1:8091`, which inside Caddy's container would mean
Caddy itself.

### 4a. Put hearts on Caddy's network

First confirm the two containers can see each other:

```bash
docker compose exec caddy wget -qO- http://hearts:8091/api/hearts/_health; echo
```

If that prints `wget: bad address 'hearts:8091'`, Docker's DNS cannot see the
service: the Ghost stack declares its networks explicitly, and the `hearts`
block does not, so Compose put it on the default network instead. Find what
Caddy is on:

```bash
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' <caddy-container>
```

Give the `hearts` service the same `networks:` list the `caddy` service has in
`docker-compose.yml`, then:

```bash
docker compose up -d hearts
docker compose exec caddy wget -qO- http://hearts:8091/api/hearts/_health; echo
```

Do not go on until that prints `{"ok":true,"schema_version":1}`.

### 4b. Add the route

Edit the Caddyfile **on the host** — the file bind-mounted into the container,
not the copy inside it. In the `{$DOMAIN}` site block only, add these five
lines just above the "Default proxy everything else to Ghost" comment:

```
        # Heart service for the photo gallery
        handle /api/hearts* {
                reverse_proxy hearts:8091
        }
```

Leave the `{$ADMIN_DOMAIN}` block alone — the gallery lives on the public site.

Your file already uses `handle` for the Ghost catch-all, which is what makes
this safe: `handle` blocks are mutually exclusive and Caddy evaluates the more
specific path first, so the heart route is matched before everything else falls
through to Ghost. `hearts/caddy-hearts.txt` shows the whole block in context.

```bash
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec caddy caddy reload  --config /etc/caddy/Caddyfile
curl -s https://example.com/api/hearts
```

You should get `{"counts":{},"mine":[],"total":0}`. `caddy reload` is graceful:
nothing drops, and Ghost is not restarted.

Confirm the private endpoint really is private:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://example.com/api/hearts/top
```

That must print `401`.

### One thing you do not get with Caddy

Standard Caddy has no rate limiter — the module needs a custom build — so the
per-IP throttle described in the spec is not in place. Still active:

- the per-voter cap inside the service, 60 writes an hour, which is the limit
  that actually catches a script since it survives a change of address
- hearting is a toggle, so holding the button down does nothing

If you want a per-IP layer, a Cloudflare rate limiting rule on `/api/hearts*`
is far less trouble than rebuilding Caddy with a plugin.

`hearts/nginx-hearts.conf` is still in the folder for reference if you ever
move to nginx. Ignore it for now.

---

## 5. Publish the gallery with hearts turned on

Back on the machine that holds your scored library. Set the token in the
environment so it never lands in your shell history or in a file:

```bash
export HEARTS_ADMIN_TOKEN="the-token-from-step-2"

python photo_scout_ghost.py \
  --site https://example.com \
  --admin-url https://admin.example.com \
  --key <ghost-id>:<ghost-secret> \
  --slug photo-scout \
  --hearts-url /api/hearts
```

On Windows PowerShell, the equivalent is `$env:HEARTS_ADMIN_TOKEN = "..."`, with
backticks rather than backslashes to continue lines. Note that it lasts only for
that terminal window. (`export` is not a PowerShell command and will silently do
nothing.)

`--key` is your Ghost Admin API key, which is a separate credential from the heart
token above — README section 11, "Getting a Ghost Admin API key", covers where to
find it and how to pass it on each platform. It can equally be set as
`GHOST_ADMIN_KEY`, which keeps both secrets off the command line.

The publish does two extra things now: it registers which photographs may be
hearted, and it bakes `/api/hearts` into the page so the buttons know where to
go. You should see a line like:

```
Hearts: 47 photographs registered, 47 known to the service
```

**Leave `--hearts-url` off and no heart buttons are rendered at all**, which is
the clean way to turn the feature off.

**Re-run the publish whenever the shortlist changes.** A photograph the service
has never been told about cannot be hearted — it answers 404, and the button
reverts. Existing hearts are never affected by republishing.

---

## 6. Look at the results

```bash
curl -s -u anyone:$HEARTS_ADMIN_TOKEN \
  'https://example.com/api/hearts/top?limit=20' | python3 -m json.tool
```

Or just open `https://example.com/api/hearts/top?limit=20` in a browser: it
will prompt for a username and password. The username is ignored; the password
is the admin token.

This is the private view — it includes your folder names, which is exactly why
it is behind a password. The public endpoint never exposes a path.

---

## 7. Back it up

The hearts are the one thing here that cannot be regenerated. Add a daily copy:

```bash
sudo tee /etc/cron.daily/hearts-backup >/dev/null <<'SH'
#!/bin/sh
D=/path/to/ghost/hearts-data
B=/var/backups/hearts
mkdir -p "$B"
sqlite3 "$D/hearts.sqlite3" ".backup '$B/hearts-$(date +%F).sqlite3'"
find "$B" -name 'hearts-*.sqlite3' -mtime +90 -delete
SH
sudo chmod +x /etc/cron.daily/hearts-backup
```

Use `.backup` rather than `cp` — it takes a consistent snapshot even while the
service is writing.

---

## If something is wrong

**Heart buttons do not appear at all.** Open the page and check the view source
for `data-hearts`. Missing means the publish ran without `--hearts-url`.
Present, but no buttons, means the browser could not reach the service — check
`curl https://example.com/api/hearts` from anywhere.

**Buttons appear, but clicking one says the service may be down.** Check what
the write actually returns:

```bash
PID=$(curl -s https://example.com/photo-scout/ | grep -o '"id":"[0-9a-f]\{16\}"' | head -1 | cut -d'"' -f4)
curl -i -s -X POST -H "X-Heart-Token: 11111111-2222-3333-4444-5555555555ab" \
  https://example.com/api/hearts/$PID | head -20
```

`{"error":"unknown photo_id"}` means the allowlist was never registered — the
publish ran without a token. Fix it without republishing the page:

```bash
HEARTS_ADMIN_TOKEN="<token>" python photo_scout_ghost.py \
  --site https://example.com --hearts-url /api/hearts --hearts-register-only
```

That uploads nothing and leaves the Ghost page alone. Confirm on the server:

```bash
docker compose exec hearts python -c "import sqlite3;print(sqlite3.connect('/data/hearts.sqlite3').execute('select count(*) from photos').fetchone()[0])"
```

A `403` with an HTML body instead means Cloudflare is blocking the POST — the
same family of problem as the 1010 on image uploads.

**`WARNING: could not update the heart allowlist (401)`.** The token where you ran
the publish does not match the one in the server's `.env`. Remember that a token set
with `export` or `$env:` lasts only for that terminal session.

**The container restarts over and over.** Read the log first — it explains
itself:

```bash
docker compose logs --tail=30 hearts
```

If it says `cannot open /data/hearts.sqlite3`, the data directory is owned by
root and the container is not:

```bash
sudo chown -R 10001:10001 hearts-data
docker compose up -d hearts
```

**`/api/hearts` returns Ghost's 404 page.** The heart route is not being
matched before the catch-all. In the Caddyfile the `handle /api/hearts*` block
must sit inside the same site block as the Ghost `handle`, and both must be
`handle` blocks — mixing a bare `reverse_proxy` with a `handle` does not do
what it looks like it does.

**Caddy logs `dial tcp 127.0.0.1:8091: connection refused`.** The upstream is
wrong: inside Caddy's container that address is Caddy. Use `hearts:8091`.

**Counts look stuck.** Something is caching. The service sends `no-store` and
Caddy passes it through; if Cloudflare sits in front of your site, add a
rule to bypass cache for `/api/hearts*`.

**Everything is broken and you want it gone.**

```bash
docker compose stop hearts
```

Remove the two nginx blocks, reload nginx, and republish without
`--hearts-url`. The gallery is unchanged and `hearts-data/` still holds every
heart if you come back to it.

---

## What is deliberately not defended against

Someone determined, with a script and a pool of addresses, can inflate the
counts. Stopping that properly means CAPTCHAs or logins, which cost more in
visitor friction than this data is worth.

What *is* stopped: accidental double-counting, holding the button down, and
casual gaming from one browser. Counts approximate distinct browsers, which is
the useful signal — treat them as a popularity hint, not a ballot.
