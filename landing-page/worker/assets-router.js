// Worker entry for the hosted marketing site. Pages, images, styles and the
// walkthrough video live in a private R2 bucket (binding SITE); this router
// serves them and keeps the contact API, the /portal redirect and security
// headers. Upload the files from dist/assets with scripts/upload-site-r2.mjs.
import {contact} from './contact.js';

const security = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'X-Frame-Options': 'SAMEORIGIN',
  'Strict-Transport-Security': 'max-age=31536000',
  'Content-Security-Policy': "frame-ancestors 'self'; base-uri 'self'; object-src 'none'; form-action 'self'",
};
const aliases = {
  '/': 'index.html', '/platform': 'platform.html', '/solutions': 'solutions.html',
  '/clients': 'clients.html', '/about': 'about.html', '/about-us': 'about.html',
  '/contact': 'contact.html', '/pricing': 'pricing.html',
};
const types = {
  html: 'text/html; charset=utf-8', css: 'text/css; charset=utf-8', js: 'text/javascript; charset=utf-8',
  svg: 'image/svg+xml', jpg: 'image/jpeg', png: 'image/png', mp4: 'video/mp4',
};

function secure(response, extra = {}) {
  const out = new Response(response.body, response);
  for (const [name, value] of Object.entries({...security, ...extra})) out.headers.set(name, value);
  return out;
}
const notFound = () => secure(new Response('Page not found. Return to Capture IV at /.',
  {status: 404, headers: {'Content-Type': 'text/plain; charset=utf-8'}}));

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/portal') {
      return secure(Response.redirect(new URL(env.PLATFORM_LOGIN_URL || '/', request.url).href, 302));
    }
    if (url.pathname === '/api/contact') return secure(await contact(request, env));
    if (!['GET', 'HEAD'].includes(request.method)) {
      return secure(new Response('Method not allowed', {status: 405, headers: {Allow: 'GET, HEAD'}}));
    }
    const path = url.pathname.replace(/\/$/, '') || '/';
    const key = aliases[path] || path.slice(1);
    const ext = key.split('.').pop().toLowerCase();
    // Only flat, known file names are served; nothing else in the bucket is reachable.
    if (!key || key.includes('/') || key.startsWith('.') || !types[ext]) return notFound();

    const object = await env.SITE.get(key, {range: request.headers, onlyIf: request.headers});
    if (object === null) return notFound();
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Content-Type', types[ext]);
    headers.set('ETag', object.httpEtag);
    headers.set('Accept-Ranges', 'bytes');
    headers.set('Cache-Control', ext === 'html' ? 'no-cache' : 'public, max-age=3600');
    if (!('body' in object)) return secure(new Response(null, {status: 304, headers}));
    let status = 200;
    let body = object.body;
    const r = object.range;
    if (r && request.headers.has('range')) {
      // R2 can return the range with empty fields (e.g. suffix: undefined for "bytes=0-").
      const hasSuffix = Number.isFinite(r.suffix);
      const offset = hasSuffix ? object.size - r.suffix : (Number.isFinite(r.offset) ? r.offset : 0);
      const length = hasSuffix ? r.suffix : (Number.isFinite(r.length) ? r.length : object.size - offset);
      if (Number.isFinite(offset) && Number.isFinite(length) && offset >= 0 && length > 0 && offset + length <= object.size) {
        headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${object.size}`);
        headers.set('Content-Length', String(length));
        status = 206;
      } else {
        // Could not express the range safely: send the whole file instead.
        const full = await env.SITE.get(key);
        if (full === null) return notFound();
        body = full.body;
        headers.set('Content-Length', String(full.size));
      }
    } else {
      headers.set('Content-Length', String(object.size));
    }
    return secure(new Response(request.method === 'HEAD' ? null : body, {status, headers}));
  },
};
