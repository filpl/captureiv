// Build for the Workers static-assets deployment (worker/assets-router.js).
// Copies web/ into dist/assets with the platform URLs filled in, so large files
// such as the walkthrough video are served as assets instead of being packed
// into the Worker script (which has a size limit).
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';

const root = process.cwd();
const web = path.join(root, 'web');
const out = path.resolve(root, 'dist/assets');
if (!out.startsWith(root + path.sep)) throw Error('Build output is outside the website.');
const config = JSON.parse(fs.readFileSync('site.config.json', 'utf8'));
const urls = {
  '{{PLATFORM_LOGIN_URL}}': process.env.PLATFORM_LOGIN_URL || config.platformLoginUrl,
  '{{PLATFORM_SIGNUP_URL}}': process.env.PLATFORM_SIGNUP_URL || config.platformSignupUrl,
  '{{PLATFORM_ENTERPRISE_URL}}': process.env.PLATFORM_ENTERPRISE_URL || config.platformEnterpriseUrl,
};
for (const [key, value] of Object.entries(urls)) {
  const parsed = new URL(value, 'https://construct.example');
  if (!((value.startsWith('/') && !value.startsWith('//')) || parsed.protocol === 'https:')) {
    throw Error(`Use an HTTPS URL or same-origin path for ${key}.`);
  }
}
const escape = value => value.replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const allowed = new Set(['.html', '.css', '.js', '.svg', '.jpg', '.png', '.mp4']);
// Public address and company facts for search engines (structured data, canonical URLs, sitemap).
const SITE = 'https://www.captureiv.com';
const routes = {'index.html': '/', 'platform.html': '/platform', 'solutions.html': '/solutions', 'clients.html': '/clients',
  'about.html': '/about', 'contact.html': '/contact', 'pricing.html': '/pricing', 'network.html': '/network'};
const organization = {
  '@context': 'https://schema.org', '@type': 'Organization', '@id': `${SITE}/#organization`,
  name: 'Capture IV', alternateName: 'Capture IV — Intelligence Visuals', url: `${SITE}/`,
  logo: `${SITE}/favicon.svg`, foundingDate: '2025', slogan: 'Intelligence Visuals.',
  description: 'Capture IV is a construction visual-intelligence platform founded in 2025. It brings drone flights, 360° site walkthroughs, site photos and project documents into one secure record for contractors, owners and field teams.',
  email: 'support@captureiv.com',
  contactPoint: {'@type': 'ContactPoint', contactType: 'customer support', email: 'support@captureiv.com', url: `${SITE}/contact`},
};
const website = {'@context': 'https://schema.org', '@type': 'WebSite', '@id': `${SITE}/#website`, name: 'Capture IV',
  url: `${SITE}/`, publisher: {'@id': `${SITE}/#organization`}};
const jsonLd = data => `<script type="application/ld+json">${JSON.stringify(data).replace(/</g, '\\u003c')}</script>`;
const attr = (html, re) => (html.match(re) || [])[1] || '';
const versions = Object.fromEntries(['site.css', 'site.js'].map(file =>
  [file, createHash('sha256').update(fs.readFileSync(path.join(web, file))).digest('hex').slice(0, 12)]));

fs.rmSync(out, {recursive: true, force: true});
fs.mkdirSync(out, {recursive: true});
let count = 0;
for (const file of fs.readdirSync(web)) {
  if (!allowed.has(path.extname(file))) continue;
  let bytes = fs.readFileSync(path.join(web, file));
  if (file.endsWith('.html')) {
    let html = bytes.toString('utf8');
    for (const [key, value] of Object.entries(urls)) html = html.replaceAll(key, escape(value));
    if (html.includes('{{')) throw Error(`Unfilled placeholder left in ${file}.`);
    let head = '<meta name="application-name" content="Capture IV"><meta property="og:site_name" content="Capture IV">';
    const route = routes[file];
    if (route) {
      const url = SITE + route, title = attr(html, /<title>([^<]*)<\/title>/), description = attr(html, /<meta name="description" content="([^"]*)"/);
      head += `<link rel="canonical" href="${url}"><meta property="og:type" content="website"><meta property="og:url" content="${url}">` +
        `<meta property="og:title" content="${title}"><meta property="og:description" content="${description}">` +
        `<meta property="og:image" content="${SITE}/software-walkthrough.png"><meta name="twitter:card" content="summary_large_image">`;
      head += jsonLd(file === 'about.html' ? {...organization, mainEntityOfPage: url} : organization);
      if (file === 'index.html') head += jsonLd(website);
      if (file === 'about.html') head += jsonLd({'@context': 'https://schema.org', '@type': 'AboutPage', url, name: title,
        about: {'@id': `${SITE}/#organization`}});
    } else {
      head += '<meta name="robots" content="noindex">';
    }
    html = html.replace('<head>', '<head>' + head);
    for (const [asset, hash] of Object.entries(versions)) html = html.replaceAll(`"/${asset}"`, `"/${asset}?v=${hash}"`);
    bytes = Buffer.from(html);
  }
  fs.writeFileSync(path.join(out, file), bytes);
  count++;
}
const today = new Date().toISOString().slice(0, 10);
fs.writeFileSync(path.join(out, 'sitemap.xml'), '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
  Object.values(routes).map(r => `  <url><loc>${SITE}${r}</loc><lastmod>${today}</lastmod></url>`).join('\n') + '\n</urlset>\n');
fs.writeFileSync(path.join(out, 'robots.txt'), `User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: ${SITE}/sitemap.xml\n`);
count += 2;
console.log(`Built ${count} static assets into dist/assets for the static-assets Worker.`);
