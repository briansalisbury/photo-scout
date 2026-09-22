# Link previews

A shared link to a Photo Scout gallery previews as the gallery. This makes it
preview as the thing that was shared: a photograph link shows that photograph, a
folder link shows that folder's highest-scoring frame.

It is a [Cloudflare Worker](https://workers.cloudflare.com/) — about a hundred
lines, no build step, no dependencies, no account of its own. Everything here is
optional: the gallery works the same without it.

| | |
|---|---|
| `preview.js` | what to show for a link, and the tags for it. Plain JavaScript, no Cloudflare in it |
| `preview-worker.js` | the Cloudflare part: fetch the page, substitute, respond |
| `wrangler.toml.example` | a starting configuration. Copy to `wrangler.toml` and fill in your own route |

## Why it is needed

Messages, Slack, Facebook and X each fetch a pasted address with a robot of
their own, read a handful of `og:` tags out of the HTML, and draw a card. None
of them run the page's JavaScript. A Photo Scout gallery decides what to show
once it is running in a browser, which is long after the robot has gone — so
every link, whatever it points at, previews identically.

The Worker reads the page on the way out. The gallery already embeds every
photograph's name, score, folder and image address — that is what it builds
itself from — so the Worker has everything it needs in the response it is
already handling. Nothing is generated, uploaded or stored, and nothing has to
be kept in step: republish the gallery and the previews follow.

## What it does, precisely

- A request with no `?photo=` or `?folder=` is passed straight through. An
  ordinary visitor never touches this code.
- Otherwise the page is fetched **without** those parameters, so every share of
  the same gallery hits one cached copy rather than one per photograph.
- The photograph is looked up, and `og:` and `twitter:` tags are inserted at the
  end of `<head>`, where they take precedence over the page's own.
- Anything unexpected — a link to a photograph since hidden, a response that is
  not HTML, an error of any kind — serves the page exactly as your site would
  have. A preview is worth nothing next to a page that loads.

There is no configuration in either file and no site name anywhere in them. The
address comes from the request, so the same Worker suits any Photo Scout gallery
on any domain.

## Requirements

- Your site is served through Cloudflare (its DNS proxy — the orange cloud).
  This does not have to be where the site is hosted.
- Your gallery has a stable address, for example `https://example.com/photos/`.

## Setup

Roughly five minutes, and reversible in one click.

### With the dashboard

1. **Workers & Pages → Create → Start with Hello World → Deploy.** Name it
   something like `photo-previews`.
2. **Edit code.** Paste `preview.js` and `preview-worker.js` in as two modules
   with those names, `preview-worker.js` being the entry point, and deploy.
3. **Settings → Domains & Routes → Add route.** This is the important step: give
   the route only the page your gallery is on.

   ```
   example.com/photos/*
   ```

   Not `example.com/*`. Everything outside the route is untouched by this
   Worker, which is the point — a Worker in front of a whole site is a Worker in
   front of every mistake it could make.
4. Paste a photograph link into a chat with yourself and check the card.

### With Wrangler

```bash
cp wrangler.toml.example wrangler.toml   # then edit the route
npx wrangler deploy
```

`wrangler.toml` is git-ignored, since it carries your own domain.

## Checking it

A preview robot is just a fetch, so `curl` is enough:

```bash
curl -s 'https://example.com/photos/?photo=3f9c1d84b0e77265' | grep 'og:image'
```

That should name one photograph's image. Without the parameter it should show
whatever your gallery page normally carries. Chat apps cache previews hard, so
use a fresh link, or Facebook's Sharing Debugger and X's Card Validator, rather
than pasting the same address again and expecting it to change.

## Cost and speed

A Worker runs in the same data center that was already serving the request; the
work here is one regular-expression match and a JSON parse. On Cloudflare's free
plan — 100,000 requests a day — link-preview traffic does not come close, and if
you are already on the $5 plan this adds nothing measurable to it. Requests
outside the route do not invoke it at all, so the rest of your site is
unaffected either way.

## Removing it

Delete the route. The Worker stops being consulted immediately and the gallery
behaves as it did before; nothing else on the site or in this project depends on
it.

## Testing

`tests/_selftest_worker.py` in the repository root runs `preview.js` under Node
against pages this project really produces, including folder names carrying
quotes and markup, and cross-checks — with a browser — that the links the
gallery hands out are the ones the Worker resolves. It needs `node` on `PATH`
and skips itself when there is none.

```bash
python tests/_selftest_worker.py
```
