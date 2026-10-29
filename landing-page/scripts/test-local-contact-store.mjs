import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {contact} from '../worker/contact.js';
import {createLocalContactStore} from './local-contact-store.mjs';

const directory=await fs.mkdtemp(path.join(os.tmpdir(),'construct-iv-contact-'));
const file=path.join(directory,'private','inquiries.json');
const db=createLocalContactStore(file);
const values={firstName:'Local',lastName:'Preview',email:'preview@example.invalid',company:'Preview Co',
 inquiryType:'General Inquiry',message:'Saved by local preview for review.',website:''};
const makeRequest=(ip)=>new Request('http://127.0.0.1:5173/api/contact',{method:'POST',
 headers:{'Content-Type':'application/json',Origin:'http://127.0.0.1:5173','CF-Connecting-IP':ip},body:JSON.stringify(values)});
try{
 const sequential=[];
 for(let index=0;index<6;index++)sequential.push(await contact(makeRequest('192.0.2.45'),{DB:db}));
 assert.deepEqual(sequential.map(response=>response.status),[201,201,201,201,201,429]);
 const parallel=await Promise.all(Array.from({length:10},()=>contact(makeRequest('192.0.2.46'),{DB:db})));
 assert.equal(parallel.filter(response=>response.status===201).length,5);
 assert.equal(parallel.filter(response=>response.status===429).length,5);
 const rows=JSON.parse(await fs.readFile(file,'utf8'));
 assert.equal(rows.length,10);
 assert.ok(rows.every(row=>row.status==='new'&&row.email_delivery==='not_configured'));
 if(process.platform!=='win32'){
  const permissions=(await fs.stat(file)).mode&0o777;
  assert.equal(permissions,0o600);
 }
 console.log(`Local contact persistence and sequential/concurrent rate limits pass${process.platform==='win32'?' (POSIX file-mode assertion skipped on Windows).':' with private file permissions.'}`);
}finally{
 await fs.rm(directory,{recursive:true,force:true});
}
