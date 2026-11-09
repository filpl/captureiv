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
    html = html.replace('<head>', '<head><meta name="application-name" content="Capture IV"><meta property="og:site_name" content="Capture IV">');
    for (const [asset, hash] of Object.entries(versions)) html = html.replaceAll(`"/${asset}"`, `"/${asset}?v=${hash}"`);
    bytes = Buffer.from(html);
  }
  fs.writeFileSync(path.join(out, file), bytes);
  count++;
}
console.log(`Built ${count} static assets into dist/assets for the static-assets Worker.`);
