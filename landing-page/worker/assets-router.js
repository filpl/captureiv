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
  // Only this site's own scripts (plus Cloudflare Web Analytics) run; fonts come from Google Fonts; nothing else is loaded.
  'Content-Security-Policy': "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
    "font-src 'self' https://fonts.gstatic.com; img-src 'self'; media-src 'self'; connect-src 'self' https://cloudflareinsights.com; frame-src 'none'; " +
    "frame-ancestors 'self'; base-uri 'self'; object-src 'none'; form-action 'self'; upgrade-insecure-requests",
  'Permissions-Policy': 'accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()',
  'Cross-Origin-Opener-Policy': 'same-origin',
};
// RFC 9116 security contact, served at /.well-known/security.txt.
const SECURITY_TXT = 'Contact: mailto:support@captureiv.com\nExpires: 2027-11-12T00:00:00.000Z\nPreferred-Languages: en\n' +
  'Canonical: https://www.captureiv.com/.well-known/security.txt\nPolicy: https://www.captureiv.com/contact\n';
const aliases = {
  '/': 'index.html', '/platform': 'platform.html', '/solutions': 'solutions.html',
  '/clients': 'clients.html', '/about': 'about.html', '/about-us': 'about.html',
  '/contact': 'contact.html', '/pricing': 'pricing.html',
};
const types = {
  html: 'text/html; charset=utf-8', css: 'text/css; charset=utf-8', js: 'text/javascript; charset=utf-8',
  svg: 'image/svg+xml', jpg: 'image/jpeg', png: 'image/png', mp4: 'video/mp4',
  xml: 'application/xml; charset=utf-8', txt: 'text/plain; charset=utf-8',
};

function secure(response, extra = {}) {
  const out = new Response(response.body, response);
  for (const [name, value] of Object.entries({...security, ...extra})) out.headers.set(name, value);
  return out;
}
const notFound = () => secure(new Response('Page not found. Return to Capture IV at /.',
  {status: 404, headers: {'Content-Type': 'text/plain; charset=utf-8'}}));

// The site's one public address. The bare domain and plain http are sent here,
// keeping the path and query, so visitors always see https://www.captureiv.com.
const CANONICAL_HOST = 'www.captureiv.com';
const REDIRECT_HOSTS = new Set(['captureiv.com', CANONICAL_HOST]);

function canonicalRedirect(url, method) {
  if (!REDIRECT_HOSTS.has(url.hostname)) return null; // workers.dev and previews are left alone
  if (url.hostname === CANONICAL_HOST && url.protocol === 'https:') return null;
  const target = `https://${CANONICAL_HOST}${url.pathname}${url.search}`;
  // 301 for page views; 308 keeps the method and body for anything else (e.g. the contact form).
  return secure(Response.redirect(target, ['GET', 'HEAD'].includes(method) ? 301 : 308));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const redirect = canonicalRedirect(url, request.method);
    if (redirect) return redirect;
    // Legal pages live on the platform; old portal page retired.
    const legalPaths = {'/privacy': 'privacy', '/terms': 'terms', '/cookies': 'cookies', '/legal': '', '/accessibility': 'accessibility'};
    if (url.pathname in legalPaths) {
      return secure(Response.redirect(`https://app.captureiv.com/legal${legalPaths[url.pathname] ? '/' + legalPaths[url.pathname] : ''}`, 301));
    }
    if (url.pathname === '/portal.html') return secure(Response.redirect(new URL(env.PLATFORM_LOGIN_URL || '/', request.url).href, 301));
    if (url.pathname === '/portal') {
      return secure(Response.redirect(new URL(env.PLATFORM_LOGIN_URL || '/', request.url).href, 302));
    }
    if (url.pathname === '/.well-known/security.txt' || url.pathname === '/security.txt') {
      return secure(new Response(request.method === 'HEAD' ? null : SECURITY_TXT,
        {headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'public, max-age=86400'}}));
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
