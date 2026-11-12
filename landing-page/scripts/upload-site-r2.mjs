// Upload dist/assets to the private R2 bucket the site Worker serves from, then
// remove objects that are no longer part of the build.
// Usage: CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... SITE_BUCKET=capture-iv-site node scripts/upload-site-r2.mjs
import fs from 'node:fs';
import path from 'node:path';

const account = process.env.CLOUDFLARE_ACCOUNT_ID;
const token = process.env.CLOUDFLARE_API_TOKEN;
const bucket = process.env.SITE_BUCKET || 'capture-iv-site';
if (!account || !token) throw Error('Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN.');
const dir = path.resolve('dist/assets');
const api = `https://api.cloudflare.com/client/v4/accounts/${account}/r2/buckets/${bucket}/objects`;
const types = {html: 'text/html; charset=utf-8', css: 'text/css; charset=utf-8', js: 'text/javascript; charset=utf-8',
  svg: 'image/svg+xml', jpg: 'image/jpeg', png: 'image/png', mp4: 'video/mp4',
  xml: 'application/xml; charset=utf-8', txt: 'text/plain; charset=utf-8'};
const auth = {Authorization: `Bearer ${token}`};

const files = fs.readdirSync(dir).filter(f => types[f.split('.').pop()]);
for (const file of files) {
  const body = fs.readFileSync(path.join(dir, file));
  const res = await fetch(`${api}/${encodeURIComponent(file)}`, {method: 'PUT', headers: {...auth, 'Content-Type': types[file.split('.').pop()]}, body});
  if (!res.ok) throw Error(`Upload of ${file} failed: ${res.status} ${await res.text()}`);
  console.log(`uploaded ${file} (${body.length} bytes)`);
}
const listed = await (await fetch(`${api}?per_page=1000`, {headers: auth})).json();
for (const object of listed.result || []) {
  if (!files.includes(object.key)) {
    const res = await fetch(`${api}/${encodeURIComponent(object.key)}`, {method: 'DELETE', headers: auth});
    console.log(`removed stale ${object.key}: ${res.status}`);
  }
}
console.log(`R2 bucket ${bucket} now holds ${files.length} site files.`);
