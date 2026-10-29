import assert from 'node:assert/strict';
import fs from 'node:fs';
import {contact} from '../worker/contact.js';
const source=fs.readFileSync('dist/server/index.js','utf8');
const {default:site}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
for(const route of ['/','/platform','/solutions','/clients','/about','/contact','/pricing']){
 const response=await site.fetch(new Request('https://construct.test'+route),{});
 assert.equal(response.status,200,route);const html=await response.text();
 assert.match(html,/Capture IV/);assert.doesNotMatch(html,/>[^<]*SKYBUILT/i);
 assert.match(html,/support@captureiv.com/);assert.doesNotMatch(html,/Illustrative photography:/);
 for(const link of html.matchAll(/href="(\/[^"]*)"/g)){
  const target=await site.fetch(new Request(new URL(link[1],'https://construct.test')),{});
  assert.equal(target.status,200,'Broken internal link '+link[1]);
 }
}
assert.equal((await site.fetch(new Request('https://construct.test/missing'),{})).status,404);
const values={firstName:'Quality',lastName:'Check',email:'qa@example.invalid',company:'Capture IV QA',inquiryType:'General Inquiry',message:'Synthetic local test inquiry; no email is sent.',website:''};
const req=(body=values,origin='https://construct.test')=>new Request('https://construct.test/api/contact',{method:'POST',headers:{'Content-Type':'application/json',Origin:origin,'CF-Connecting-IP':'192.0.2.10'},body:JSON.stringify(body)});
assert.equal((await contact(req({...values,email:'invalid'}),{})).status,400);
assert.equal((await contact(req({...values,inquiryType:'unrecognized'}),{})).status,400);
assert.equal((await contact(req({...values,website:'spam.example'}),{})).status,400);
assert.equal((await contact(req(values,'https://other.example'),{})).status,403);
assert.equal((await contact(req(),{})).status,503);
assert.equal((await contact(new Request('https://construct.test/api/contact'),{})).status,405);
let recorded;const db={prepare(sql){assert.match(sql,/INSERT INTO contact_inquiries/);return {bind(...args){recorded=args;return {run:async()=>({success:true,meta:{changes:1}})}}}}};
const saved=await contact(req(),{DB:db});assert.equal(saved.status,201);const result=await saved.json();assert.equal(result.saved,true);assert.match(result.id,/^[a-f0-9-]{36}$/);assert.equal(recorded[3],values.email);assert.equal(recorded[6],values.message);
const limited={prepare(){return {bind(){return {run:async()=>({success:true,meta:{changes:0}})}}}}};
assert.equal((await contact(req(),{DB:limited})).status,429);
const broken={prepare(){throw Error('synthetic unavailable storage')}};
assert.equal((await contact(req(),{DB:broken})).status,503);
console.log('Eight routes and internal links pass. Contact validation, origin checks, persistence calls, rate limiting and storage failures pass.');

const portal=await site.fetch(new Request('https://construct.test/portal'),{});assert.equal(portal.status,302);assert.ok(portal.headers.get('Location'));console.log('Legacy /portal redirects directly to the configured platform login.');
