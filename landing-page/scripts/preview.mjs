import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createLocalContactStore} from './local-contact-store.mjs';
const source=fs.readFileSync('dist/server/index.js','utf8');
const {default:worker}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const port=Number(process.env.PORT||5173);
const host=process.env.HOST||'127.0.0.1';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const mediaTypes={'.jpg':'image/jpeg','.png':'image/png','.mp4':'video/mp4'};
// Stands in for Cloudflare static assets: serves the media copied to dist/public by the build.
const localAssets={async fetch(request){const name=path.basename(new URL(request.url).pathname);const file=path.join(root,'dist','public',name);const type=mediaTypes[path.extname(name)];if(!type||!fs.existsSync(file))return new Response('Page not found. Return to Capture IV at /.',{status:404,headers:{'Content-Type':'text/plain; charset=utf-8'}});return new Response(fs.readFileSync(file),{headers:{'Content-Type':type,'Cache-Control':'public, max-age=3600'}})}};
const inquiryFile=process.env.CAPTUREIV_CONTACT_STORE||path.join(root,'.local','contact-inquiries.json');
const contactStore=createLocalContactStore(path.resolve(inquiryFile));
http.createServer(async(req,res)=>{
 try{
  const chunks=[];let size=0;
  for await(const chunk of req){size+=chunk.length;if(size>32000){res.writeHead(413);res.end('Request too large');return}chunks.push(chunk)}
  const headers=new Headers(req.headers);
  if(!headers.has('CF-Connecting-IP'))headers.set('CF-Connecting-IP',req.socket.remoteAddress||'local-preview');
  const request=new Request(new URL(req.url,`http://localhost:${port}`),{method:req.method,headers,...(!['GET','HEAD'].includes(req.method)?{body:Buffer.concat(chunks)}:{})});
  const result=await worker.fetch(request,{DB:contactStore,ASSETS:localAssets});
  res.writeHead(result.status,Object.fromEntries(result.headers));res.end(Buffer.from(await result.arrayBuffer()));
 }catch{res.writeHead(500);res.end('Preview failed')}
}).listen(port,host,()=>console.log(`Capture IV preview: http://${host==='0.0.0.0'?'127.0.0.1':host}:${port}\nContact inquiries are saved locally in the ignored .local/ folder; production uses the configured D1 database.`));
