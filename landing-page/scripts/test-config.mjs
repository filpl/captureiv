import assert from 'node:assert/strict';
import fs from 'node:fs';
import {execFileSync,spawnSync} from 'node:child_process';
const build=(url)=>execFileSync(process.execPath,['scripts/build.mjs'],{env:{...process.env,PLATFORM_LOGIN_URL:url,PLATFORM_SIGNUP_URL:'https://platform.example.com/register',PLATFORM_ENTERPRISE_URL:'https://platform.example.com/enterprise/request'},stdio:'pipe'});
try{
 for(const login of ['https://platform.example.com/login','/login']){
  build(login);
  const source=fs.readFileSync('dist/server/index.js');
  const {default:site}=await import('data:text/javascript;base64,'+source.toString('base64'));
  const page=await (await site.fetch(new Request('https://construct.test/'),{})).text();
  assert.ok(page.includes(`href="${login}"`));
  assert.ok(page.includes('href="https://platform.example.com/register"'));
  assert.ok(!page.includes('{{PLATFORM_LOGIN_URL}}'));
  const redirect=await site.fetch(new Request('https://construct.test/portal'),{});
  assert.equal(redirect.status,302);
  assert.equal(redirect.headers.get('Location'),new URL(login,'https://construct.test').href);
 }
 const invalid=spawnSync(process.execPath,['scripts/build.mjs'],{env:{...process.env,PLATFORM_LOGIN_URL:'javascript:alert(1)'},stdio:'pipe'});
 assert.notEqual(invalid.status,0);
 console.log('Absolute and same-origin platform login links/redirects pass; unsafe URL scheme rejected.');
}finally{
 const env={...process.env};delete env.PLATFORM_LOGIN_URL;
 delete env.PLATFORM_SIGNUP_URL;delete env.PLATFORM_ENTERPRISE_URL;
 execFileSync(process.execPath,['scripts/build.mjs'],{env,stdio:'pipe'});
}
