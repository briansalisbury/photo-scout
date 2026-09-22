// Link previews for a Photo Scout gallery: the part that decides what to show.
//
// Paste a link into Messages, Slack or Facebook and the app fetches it with a
// robot of its own, reads a few tags out of the HTML, and draws a card. The
// robot does not run the page's JavaScript, so a gallery that opens a
// photograph in the browser previews as the gallery and nothing more.
//
// This reads the gallery page that is being served anyway. The page already
// carries every photograph's score, folder and image address - that is what it
// builds itself from - so nothing has to be registered, published or kept in
// step. Rebuild the gallery and the previews follow.
//
// Kept apart from the Cloudflare wrapper so it can be tested on its own, with
// no Cloudflare in the room. See preview-worker.js.

// The page's own list, embedded as JSON. script_json escapes '</' on the way
// in, which JSON.parse unescapes for us.
export function payloadFrom(html) {
  const m = /<script[^>]*class="psc-data"[^>]*>([\s\S]*?)<\/script>/i.exec(html);
  if (!m) return null;
  try {
    const data = JSON.parse(m[1]);
    return Array.isArray(data.p) && Array.isArray(data.g) ? data : null;
  } catch (e) {
    return null;
  }
}

// Folder links are spelled the way the gallery spells them, so a link the page
// produced resolves here. Any change to one belongs in both.
export function slugify(name) {
  let s = String(name);
  if (s.normalize) s = s.normalize('NFKD').replace(/[̀-ͯ]/g, '');
  s = s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return s || encodeURIComponent(String(name).toLowerCase());
}

// Encoding normalised, so a link survives a chat app rewriting it.
export function linkKey(raw) {
  try {
    return encodeURIComponent(decodeURIComponent(raw)).toLowerCase();
  } catch (e) {
    return '';
  }
}

// Two folders can come down to the same words once punctuation goes; the
// second gets a number, exactly as the gallery does it.
export function folderSlugs(groups) {
  const slugs = [], taken = Object.create(null);
  groups.forEach((name) => {
    let s = slugify(name);
    const base = s;
    let n = 2;
    while (linkKey(s) in taken) s = base + '-' + n++;
    taken[linkKey(s)] = true;
    slugs.push(s);
  });
  return slugs;
}

// Which link this is, if any. Nothing else in the address concerns us.
export function readTarget(searchParams) {
  for (const kind of ['photo', 'folder']) {
    const v = searchParams.get(kind);
    if (v) return { kind, key: linkKey(v) };
  }
  return null;
}

const num = (v) => (typeof v === 'number' ? v : parseFloat(v) || 0);

// The photograph a link should show: the one asked for, or a folder's best.
export function choose(payload, target) {
  // Object.create(null): the key comes from a link, and on an ordinary object
  // '?photo=constructor' would find something.
  if (target.kind === 'photo') {
    const byId = Object.create(null);
    payload.p.forEach((p, i) => { byId[String(p.id).toLowerCase()] = i; });
    if (!(target.key in byId)) return null;
    const p = payload.p[byId[target.key]];
    return { photo: p, title: p.n || 'Photograph',
             description: [p.f, p.d ? String(p.d).slice(0, 10) : '', p.r]
                            .filter(Boolean).join(' · ') };
  }
  const slugs = folderSlugs(payload.g);
  const gi = slugs.findIndex((s) => linkKey(s) === target.key);
  if (gi < 0) return null;
  // The folder's highest-scoring photograph stands for it - the same one the
  // gallery shows first inside it. No image is made, so nothing is uploaded
  // and nothing can go stale.
  let best = null, count = 0;
  for (const p of payload.p) {
    if (+p.g !== gi) continue;
    count++;
    if (!best || num(p.s) > num(best.s)) best = p;
  }
  if (!best) return null;
  return { photo: best, title: payload.g[gi],
           description: count === 1 ? '1 photograph' : count + ' photographs' };
}

const escapeAttr = (v) => String(v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// Absolute, because a preview robot will not resolve a relative address.
function absolute(src, pageUrl) {
  try {
    return new URL(src, pageUrl).href;
  } catch (e) {
    return '';
  }
}

// The tags themselves. og:* is what most apps read; twitter:* is what X reads.
export function tagsFor(choice, pageUrl) {
  const p = choice.photo;
  const img = absolute(p.pv || p.th || '', pageUrl);
  if (!img) return '';
  const tags = [
    ['og:type', 'article'],
    ['og:title', choice.title],
    ['og:description', choice.description],
    ['og:image', img],
    ['og:image:alt', choice.title],
    ['og:url', pageUrl],
  ].map(([k, v]) => `<meta property="${escapeAttr(k)}" content="${escapeAttr(v)}">`);
  tags.push('<meta name="twitter:card" content="summary_large_image">');
  tags.push(`<meta name="twitter:title" content="${escapeAttr(choice.title)}">`);
  tags.push(`<meta name="twitter:description" content="${escapeAttr(choice.description)}">`);
  tags.push(`<meta name="twitter:image" content="${escapeAttr(img)}">`);
  return tags.join('');
}

// Everything above, in one call: page in, tags out, or '' to leave it alone.
export function previewTags(html, searchParams, pageUrl) {
  const target = readTarget(searchParams);
  if (!target) return '';
  const payload = payloadFrom(html);
  if (!payload) return '';
  const choice = choose(payload, target);
  return choice ? tagsFor(choice, pageUrl) : '';
}

// A whole <meta> tag, with quoted values so a '>' inside one cannot end the
// match early and leave half a tag behind in the head.
const META = /<meta\b(?:[^>"']|"[^"]*"|'[^']*')*>/gi;
// The ones we are replacing. og:site_name, and anything else a theme sets, is
// left where it is.
const REPLACED =
  /(?:property|name)\s*=\s*["'](?:og:(?:type|title|description|url|image(?::[a-z_]+)?)|twitter:(?:card|title|description|image(?::[a-z_]+)?))["']/i;

// A page already carries its own og: and twitter: tags. Leaving both sets in
// place is not safe: robots disagree about which wins - some take the first
// og:image, some the last - so the page's own are removed rather than competed
// with, and ours go in at the end of the head.
export function withPreview(html, tags) {
  if (!tags) return html;
  const i = html.toLowerCase().lastIndexOf('</head>');
  if (i < 0) return html;
  const head = html.slice(0, i).replace(META, (m) => (REPLACED.test(m) ? '' : m));
  return head + tags + html.slice(i);
}
