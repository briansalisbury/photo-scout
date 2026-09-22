// Cloudflare Worker: gives a shared Photo Scout link its own preview card.
//
// Point it at the ONE page your gallery lives on - see worker/README.md. It
// does nothing to any other address, and nothing to an ordinary visit: a
// request with no ?photo= or ?folder= is passed straight through, untouched.
//
// There is no configuration in this file and no site name anywhere in it. The
// address comes from the request, so the same Worker suits any Photo Scout
// gallery on any domain.
//
// Every failure falls back to serving the page exactly as the site would have
// served it. A preview is worth nothing next to a page that loads.

import { previewTags, withPreview, readTarget } from './preview.js';

export default {
  async fetch(request, env, ctx) {
    try {
      return await handle(request);
    } catch (e) {
      return fetch(request);
    }
  },
};

async function handle(request) {
  // Only GET, and only HTML: a preview robot asks for the page itself.
  if (request.method !== 'GET' && request.method !== 'HEAD') return fetch(request);

  const url = new URL(request.url);
  if (!readTarget(url.searchParams)) return fetch(request);

  // Fetched WITHOUT our own parameters, so every share of the same gallery
  // hits one cached copy of the page rather than one per photograph.
  const origin = new URL(url);
  origin.searchParams.delete('photo');
  origin.searchParams.delete('folder');
  const resp = await fetch(new Request(origin.toString(), request));

  const type = resp.headers.get('content-type') || '';
  if (!resp.ok || !type.includes('text/html')) return resp;

  const html = await resp.text();
  const tags = previewTags(html, url.searchParams, url.toString());
  if (!tags) return new Response(html, resp);      // unknown link: page as-is

  const headers = new Headers(resp.headers);
  headers.delete('content-length');                // the body just changed
  return new Response(withPreview(html, tags), {
    status: resp.status,
    statusText: resp.statusText,
    headers,
  });
}
