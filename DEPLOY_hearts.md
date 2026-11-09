# Deploying the heart service

The heart service lets visitors "like" photographs in a published gallery. It
is optional: leave it out and the gallery works exactly the same, without heart
buttons.

It is one small Python service in its own container. It stores which anonymous
browsers liked which photographs — no IP addresses, no accounts, no raw visitor
identifiers — and your web server passes `/api/hearts` through to it while
everything else on the domain keeps reaching Ghost.

**If it is down, the gallery is unaffected.** The heart buttons simply do not
appear. That is tested, not hoped for.

---

## Before you start

This guide is written for Ghost run with the **official Ghost Docker setup**
([TryGhost/ghost-docker](https://github.com/TryGhost/ghost-docker)): a folder
on your server holding Ghost's `compose.yml`, a `.env`, and a Caddyfile for the
Caddy container in front of Ghost. If you installed Ghost with Ghost-CLI behind
nginx instead, follow the steps below but see
[Ghost behind nginx](#ghost-behind-nginx) for the two that differ.

You need Docker Compose v2 — the `docker compose` command, not the older
`docker-compose`. Allow about fifteen minutes, most of it Docker building.

In every command, `/path/to/ghost` is the folder holding Ghost's `compose.yml`
and `example.com` is your site.

---

## What it costs your server

Sized for a small VPS: the reference deployment is a 2 GB, single-core box
shared with Ghost and MySQL.

| | |
|---|---|
| Memory, steady state | ~40 MB resident |
| Memory, hard ceiling | 160 MB, enforced by Compose |
| CPU | capped at half a core; idle between requests |
| Disk | ~150 MB image, under 1 MB of data |
| Per page view | one request, one indexed SQLite read, ~1 ms |
| Per click | one request, one row inserted or deleted |

If the service ever misbehaves, Docker kills it at the memory ceiling and Ghost
keeps running. Check the real numbers once it is up with
`docker stats --no-stream hearts`; steady-state memory well above 60 MB means
something is wrong, and is worth an issue.

---

## 1. Copy the files to the server

From the machine with your Photo Scout copy, put the `hearts` folder beside
Ghost's `compose.yml`:

```bash
scp -r ./hearts you@your-server:/path/to/ghost/
```

Then, on the server, copy the service definition up next to `compose.yml`:

```bash
cd /path/to/ghost
cp hearts/docker-compose.hearts.yml compose.hearts.yml
```

**Do not paste the service into Ghost's `compose.yml`.** Keeping it in a file of
its own means `compose.yml` stays exactly as Ghost ships it, so updating Ghost
never conflicts with your changes.

---

## 2. Create the admin token and the data folder

```bash
echo "HEARTS_ADMIN_TOKEN=$(openssl rand -hex 32)" >> .env
chmod 600 .env
grep HEARTS_ADMIN_TOKEN .env
```

Keep that value somewhere safe: you need it to publish. It guards the ranked
"most liked" list and the list of photographs that may be liked.

```bash
mkdir -p hearts-data
sudo chown -R 10001:10001 hearts-data
```

**Do not skip the `chown`.** The container deliberately runs unprivileged, as
uid 10001, and Docker creates the folder owned by root. Without the change of
ownership the service cannot create its database and restarts forever.

---

## 3. Tell Compose to load the hearts file

One line in Ghost's `.env` makes every `docker compose` command load both files:

```bash
grep -n '^COMPOSE_FILE' .env          # should print nothing
echo 'COMPOSE_FILE=compose.yml:compose.hearts.yml' >> .env
```

If the `grep` does print a line, add `:compose.hearts.yml` to the end of it
rather than writing a second one.

Check Compose sees the service and the merged files are valid:

```bash
docker compose config --quiet && echo "config OK"
docker compose config --services      # hearts should be listed
```

---

## 4. Start it

```bash
docker compose up -d --build hearts
docker compose ps hearts
curl -s 'http://localhost:8091/api/hearts/_health'
```

That last command should print `{"ok":true,"schema_version":1}`.

**Check it is not exposed to the internet.** From a different machine:

```bash
curl 'https://example.com:8091/api/hearts/_health'
```

That must fail to connect. If it answers, the `127.0.0.1:` prefix is missing
from the `ports:` line in `compose.hearts.yml` — fix that before going on.

---

## 5. Route /api/hearts through Caddy

Caddy reaches the service over Docker's network as `hearts:8091`. Confirm it
can see it:

```bash
docker compose exec caddy wget -qO- 'http://hearts:8091/api/hearts/_health'
```

Expect `{"ok":true,"schema_version":1}`. If you get `bad address 'hearts:8091'`,
the two are on different networks: `compose.hearts.yml` uses `ghost_network`,
the name in the official setup. Change it to whatever the `caddy` service lists
under `networks:` in `compose.yml`, then run `docker compose up -d hearts` and
try again.

Now add the route. Edit your Caddyfile on the host (`caddy/Caddyfile` in the
official setup). In the `{$DOMAIN}` site block only, add these lines just above
the "Default proxy everything else to Ghost" comment:

```
        # Heart service for the photo gallery
        handle /api/hearts* {
                reverse_proxy hearts:8091
        }
```

Leave the `{$ADMIN_DOMAIN}` block alone; the gallery lives on the public site.
`hearts/caddy-hearts.txt` shows the whole block in context. The upstream is
`hearts:8091`, not `127.0.0.1:8091`: inside Caddy's container, localhost is
Caddy.

Apply it. The reload is graceful: nothing drops and Ghost is not restarted.

```bash
docker compose exec caddy caddy validate --config /etc/caddy/Caddyfile
docker compose exec caddy caddy reload  --config /etc/caddy/Caddyfile
curl -s 'https://example.com/api/hearts'
curl -s -o /dev/null -w '%{http_code}\n' 'https://example.com/api/hearts/top'
```

The first should print `{"counts":{},"mine":[],"total":0}`; the second must
print `401`, which confirms the private list is private. (Addresses are in
single quotes throughout this guide so that zsh does not rewrite them as you
paste.)

Standard Caddy has no per-IP rate limiter, so that layer from `SPEC_hearts.md`
is not in place. The service's own limit of 60 writes per visitor per hour
still is, and it is the one that catches a script. If you want a per-IP layer
too, a Cloudflare rate-limiting rule on `/api/hearts*` is far less trouble than
a custom Caddy build.

---

## 6. Publish the gallery with hearts turned on

On the machine that holds your scored library, with the token in the
environment so it never lands in your shell history or in a file:

```bash
export HEARTS_ADMIN_TOKEN="the-token-from-step-2"

python photo_scout_ghost.py \
  --site https://example.com \
  --key <ghost-id>:<ghost-secret> \
  --hearts-url /api/hearts
```

In Windows PowerShell, set it with `$env:HEARTS_ADMIN_TOKEN = "..."` and
continue lines with a backtick rather than a backslash. Either way it lasts only
for that terminal window.

`--key` is your Ghost Admin API key, a separate credential from the heart token;
README section 12, "Getting a Ghost Admin API key", covers where to find it.

The publish now also tells the service which photographs may be liked. Look for:

```
Hearts: 47 photographs registered, 47 known to the service
```

**Re-run the publish whenever the shortlist changes.** A photograph the service
has not been told about cannot be liked. Existing likes are never affected by
republishing. Publishing without `--hearts-url` renders no heart buttons at all,
which is the clean way to turn the feature off.

---

## 7. See the results

Open `https://example.com/api/hearts/top?limit=20` in a browser. It asks for a
username and password: the username is ignored, and the password is the admin
token. Or from a terminal:

```bash
curl -s -u anyone:$HEARTS_ADMIN_TOKEN \
  'https://example.com/api/hearts/top?limit=20' | python3 -m json.tool
```

This private view includes your folder names, which is why it is behind the
token. The public endpoint never exposes a path.

---

## 8. Back it up

The likes are the one thing here that cannot be regenerated. A daily snapshot:

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

Use `.backup` rather than `cp`: it takes a consistent snapshot even while the
service is writing.

---

## Updating

**Ghost.** Update the way the Ghost Docker setup tells you to — no extra steps:

```bash
cd /path/to/ghost
git pull && docker compose pull && docker compose up -d
```

`docker compose pull` skips hearts, which is expected: it is built locally,
not downloaded. Its data in `hearts-data/` is untouched by the update. As with
any Ghost upgrade, export your content first (Ghost Admin → Settings →
Advanced → Export).

**The heart service.** Copy the new `hearts` folder up as in step 1, then
rebuild it:

```bash
docker compose up -d --build hearts
```

Your `compose.hearts.yml` is not overwritten by a new `hearts` folder. If a
release changes `hearts/docker-compose.hearts.yml`, compare the two and carry
the change across.

---

## Moving an earlier install out of compose.yml

Earlier versions of this guide had you paste the `hearts:` block into Ghost's
`compose.yml`. That works, but every Ghost update that touches `compose.yml`
then stops with *"Your local changes to the following files would be
overwritten by merge: compose.yml"*. Nothing is lost when that happens — git
refuses before changing anything. To move hearts into its own file:

```bash
cd /path/to/ghost
cp compose.yml ~/compose.yml.bak                # insurance
cp -a hearts-data ~/hearts-data.bak
git diff compose.yml                            # should show only the hearts block
```

1. Create `compose.hearts.yml` with your existing `hearts:` block in it, under
   its own `services:` key. If you never changed the block, copying
   `hearts/docker-compose.hearts.yml` from a current Photo Scout does the same
   job; if you did — a different network name, say — carry your version over.
2. Add the `COMPOSE_FILE` line to `.env`, as in [step 3](#3-tell-compose-to-load-the-hearts-file).
3. Hand `compose.yml` back to Ghost and check the result before starting
   anything:

   ```bash
   git checkout -- compose.yml
   docker compose config --quiet && echo "config OK"
   docker compose config --services      # hearts should still be listed
   ```

4. Update as normal. The service definition has not changed, so the hearts
   container carries on with its data.

---

## Ghost behind nginx

If Ghost was installed with Ghost-CLI and nginx serves your site, there is no
Ghost `compose.yml` to join. Run hearts as a small Compose project of its own,
and point nginx at it.

- **Steps 1–3:** put the `hearts` folder, `compose.hearts.yml`, a `.env` with
  the token, and `hearts-data/` in a folder of their own, such as
  `/opt/photo-scout-hearts`. In `compose.hearts.yml`, delete the two `networks:`
  lines at the end — there is no Ghost network here. (Forget, and Compose
  stops before starting anything with *"refers to undefined network
  ghost_network"*.) Then set `COMPOSE_FILE=compose.hearts.yml` in that folder's
  `.env`.
- **Step 5:** instead of the Caddy route, add the two `location` blocks from
  `hearts/nginx-hearts.conf` to your site's `server` block, and the
  `limit_req_zone` line to the `http` block. nginx reaches the service at
  `127.0.0.1:8091`, which is why that `ports:` line matters here. Then:

  ```bash
  sudo nginx -t && sudo systemctl reload nginx
  ```

nginx gives you the per-IP rate limit that standard Caddy lacks. Everything
else in this guide applies unchanged.

---

## If something is wrong

**Heart buttons do not appear at all.** View the gallery page's source and look
for `data-hearts`. Missing means the publish ran without `--hearts-url`.
Present, but no buttons, means the browser cannot reach the service: check
`curl -s 'https://example.com/api/hearts'` from anywhere.

**Clicking a heart says the service may be down.** See what a write returns:

```bash
PID=$(curl -s https://example.com/photo-scout/ | grep -o '"id":"[0-9a-f]\{16\}"' | head -1 | cut -d'"' -f4)
curl -i -s -X POST -H "X-Heart-Token: 11111111-2222-3333-4444-5555555555ab" \
  https://example.com/api/hearts/$PID | head -20
```

`{"error":"unknown photo_id"}` means the list of photographs was never
registered, usually because the publish ran without the token. Fix it without
republishing the page:

```bash
HEARTS_ADMIN_TOKEN="<token>" python photo_scout_ghost.py \
  --site https://example.com --hearts-url /api/hearts --hearts-register-only
```

A `403` with an HTML body instead means a firewall such as Cloudflare is
blocking the request.

**`WARNING: could not update the heart allowlist (401)`.** The token where you
ran the publish does not match the one in the server's `.env`.

**The container restarts over and over.** The log explains itself:

```bash
docker compose logs --tail=30 hearts
```

`cannot open /data/hearts.sqlite3` means the data folder is still owned by
root — run the `chown` from step 2 and `docker compose up -d hearts`.

**`/api/hearts` shows Ghost's 404 page.** The heart route is not being matched
before Ghost's catch-all. In the Caddyfile, the `handle /api/hearts*` block must
sit in the same site block as Ghost's `handle`, and both must be `handle`
blocks; a bare `reverse_proxy` mixed with `handle` does not do what it looks
like it does.

**Caddy logs `dial tcp 127.0.0.1:8091: connection refused`.** The upstream is
wrong: use `hearts:8091`.

**A health check answers `405 Method Not Allowed`.** The address has something
stuck to the end, so it matches the route for liking a photograph, which only
accepts POST. The usual cause is zsh: Oh My Zsh escapes special characters in a
pasted web address, so `_health; echo` arrives as `_health\; echo`. Put the
address in single quotes, as the commands in this guide do. The service is fine.

**Counts look stuck.** Something is caching. The service sends `no-store`; if
Cloudflare or another CDN sits in front of your site, add a rule to bypass the
cache for `/api/hearts*`.

**You want it gone.** Remove the route from your Caddyfile (or nginx) and
reload, republish the gallery without `--hearts-url`, then:

```bash
docker compose stop hearts
```

and take `:compose.hearts.yml` back off the `COMPOSE_FILE` line in `.env`, or
the next `docker compose up -d` starts it again. `hearts-data/` still holds
every like if you come back to it.

---

## What is deliberately not defended against

Someone determined, with a script and a pool of addresses, can inflate the
counts. Stopping that properly means CAPTCHAs or logins, which cost more in
visitor friction than this data is worth.

What *is* stopped: accidental double-counting, holding the button down, and
casual gaming from one browser. Counts approximate distinct browsers — treat
them as a popularity hint, not a ballot.
