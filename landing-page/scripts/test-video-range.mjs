import assert from 'node:assert/strict';
import fs from 'node:fs';
const source=fs.readFileSync('dist/server/index.js','utf8');
const {default:site}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
// Mock Cloudflare static-asset store: returns the full file as a plain 200 (no range support), like production.
const SIZE=2000;
const fileBytes=new Uint8Array(SIZE); for(let i=0;i<SIZE;i++)fileBytes[i]=i%256;
const env={ASSETS:{async fetch(){return new Response(fileBytes,{status:200,headers:{'Content-Type':'video/mp4'}});}}};
const vurl='https://construct.test/capture-iv-product-tour.mp4';
const req=(headers={})=>new Request(vurl,{headers});

// Full GET: 200, advertises Accept-Ranges
let r=await site.fetch(req(),env);
assert.equal(r.status,200,'full GET status');
assert.equal(r.headers.get('Accept-Ranges'),'bytes','full GET advertises ranges');
assert.equal(r.headers.get('Content-Length'),String(SIZE));
assert.equal((await r.arrayBuffer()).byteLength,SIZE);

// Mid-file range: 206 with correct slice
r=await site.fetch(req({Range:'bytes=100-199'}),env);
assert.equal(r.status,206,'mid range status');
assert.equal(r.headers.get('Content-Range'),'bytes 100-199/'+SIZE);
assert.equal(r.headers.get('Content-Length'),'100');
assert.equal(r.headers.get('Accept-Ranges'),'bytes');
const body=new Uint8Array(await r.arrayBuffer());
assert.equal(body.length,100);
assert.equal(body[0],100%256);assert.equal(body[99],199%256);

// Open-ended range the browser sends first (bytes=0-): 206 spanning whole file
r=await site.fetch(req({Range:'bytes=0-'}),env);
assert.equal(r.status,206,'open range status');
assert.equal(r.headers.get('Content-Range'),'bytes 0-'+(SIZE-1)+'/'+SIZE);

// Suffix range (last 50 bytes)
r=await site.fetch(req({Range:'bytes=-50'}),env);
assert.equal(r.status,206,'suffix range status');
assert.equal(r.headers.get('Content-Range'),'bytes '+(SIZE-50)+'-'+(SIZE-1)+'/'+SIZE);

// Unsatisfiable range: 416
r=await site.fetch(req({Range:'bytes=5000-6000'}),env);
assert.equal(r.status,416,'unsatisfiable range status');
assert.equal(r.headers.get('Content-Range'),'bytes */'+SIZE);

// HEAD with range: 206, empty body
r=await site.fetch(new Request(vurl,{method:'HEAD',headers:{Range:'bytes=0-99'}}),env);
assert.equal(r.status,206,'HEAD range status');
assert.equal((await r.arrayBuffer()).byteLength,0,'HEAD has no body');

// Non-video asset still passes straight through to ASSETS (image → the mock returns 200 full)
r=await site.fetch(new Request('https://construct.test/hero-site.jpg',{headers:{Range:'bytes=0-9'}}),env);
assert.equal(r.status,200,'image passthrough unchanged');

console.log('Video range: full GET, mid/open/suffix ranges, 416, HEAD, and image passthrough all pass.');
