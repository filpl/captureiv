import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
const root=process.cwd();const web=path.join(root,'web');const dist=path.resolve(root,'dist');
if(!dist.startsWith(root+path.sep))throw Error('Build output is outside the website.');
const config=JSON.parse(fs.readFileSync('site.config.json','utf8'));
const loginURL=process.env.PLATFORM_LOGIN_URL || config.platformLoginUrl;
const signupURL=process.env.PLATFORM_SIGNUP_URL || config.platformSignupUrl;
const enterpriseURL=process.env.PLATFORM_ENTERPRISE_URL || config.platformEnterpriseUrl;
function validatePlatformURL(value,label){const parsed=new URL(value,'https://construct.example');if(!((value.startsWith('/')&&!value.startsWith('//'))||parsed.protocol==='https:'||parsed.protocol==='http:'&&['localhost','127.0.0.1','[::1]'].includes(parsed.hostname)))throw Error(`Use an HTTPS ${label} URL or a same-origin path.`);return value.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const htmlURL=validatePlatformURL(loginURL,'login'),htmlSignupURL=validatePlatformURL(signupURL,'signup'),htmlEnterpriseURL=validatePlatformURL(enterpriseURL,'enterprise request');
const assets={};const types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.jpg':'image/jpeg','.png':'image/png'};
const versions=Object.fromEntries(['site.css','site.js'].map(file=>[file,createHash('sha256').update(fs.readFileSync(path.join(web,file))).digest('hex').slice(0,12)]));
for(const file of fs.readdirSync(web)){
 const type=types[path.extname(file)];if(!type)continue;
 let bytes=fs.readFileSync(path.join(web,file));
 if(file.endsWith('.html')){let html=bytes.toString('utf8').replaceAll('{{PLATFORM_LOGIN_URL}}',htmlURL).replaceAll('{{PLATFORM_SIGNUP_URL}}',htmlSignupURL).replaceAll('{{PLATFORM_ENTERPRISE_URL}}',htmlEnterpriseURL);html=html.replace('<head>','<head><meta name="application-name" content="Capture IV"><meta property="og:site_name" content="Capture IV">');for(const [asset,hash] of Object.entries(versions))html=html.replaceAll('"/'+asset+'"','"/'+asset+'?v='+hash+'"');bytes=Buffer.from(html)}
 assets['/'+file]={type,base64:bytes.toString('base64')};
}
const contactSource=fs.readFileSync('worker/contact.js','utf8').replace('export async function contact','async function contact');
const router=`
const platformLoginURL=${JSON.stringify(loginURL)};
const aliases={'/':'/index.html','/platform':'/platform.html','/solutions':'/solutions.html','/clients':'/clients.html','/about':'/about.html','/about-us':'/about.html','/contact':'/contact.html','/pricing':'/pricing.html','/portal':'/portal.html'};
export default {async fetch(request,env){
 const url=new URL(request.url);
 if(url.pathname==='/portal')return Response.redirect(new URL(platformLoginURL,request.url).href,302);
 if(url.pathname==='/api/contact')return contact(request,env);
 if(!['GET','HEAD'].includes(request.method))return new Response('Method not allowed',{status:405,headers:{Allow:'GET, HEAD'}});
 let key=aliases[url.pathname.replace(/\\/$/,'')||'/']||url.pathname;
 const asset=assets[key];
 if(!asset)return new Response('Page not found. Return to Capture IV at /.',{status:404,headers:{'Content-Type':'text/plain; charset=utf-8'}});
 const bytes=Uint8Array.from(atob(asset.base64),c=>c.charCodeAt(0));
 return new Response(request.method==='HEAD'?null:bytes,{headers:{'Content-Type':asset.type,'Cache-Control':key.endsWith('.html')?'no-cache':'public, max-age=3600','X-Content-Type-Options':'nosniff','Referrer-Policy':'strict-origin-when-cross-origin','X-Frame-Options':'SAMEORIGIN'}});
}};
`;
const worker='const assets='+JSON.stringify(assets)+';\n'+contactSource+'\n'+router;

// The output is generated exclusively from this website's tracked web/ and worker/ sources.
fs.rmSync(dist,{recursive:true,force:true});
fs.mkdirSync(path.join(dist,'server'),{recursive:true});
fs.writeFileSync(path.join(dist,'server/index.js'),worker);

console.log('Built Capture IV Worker, eight pages, assets and contact migrations.');
