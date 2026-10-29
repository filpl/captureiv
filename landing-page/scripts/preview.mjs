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
const inquiryFile=process.env.CAPTUREIV_CONTACT_STORE||path.join(root,'.local','contact-inquiries.json');
const contactStore=createLocalContactStore(path.resolve(inquiryFile));
http.createServer(async(req,res)=>{
 try{
  const chunks=[];let size=0;
  for await(const chunk of req){size+=chunk.length;if(size>32000){res.writeHead(413);res.end('Request too large');return}chunks.push(chunk)}
  const headers=new Headers(req.headers);
  if(!headers.has('CF-Connecting-IP'))headers.set('CF-Connecting-IP',req.socket.remoteAddress||'local-preview');
  const request=new Request(new URL(req.url,`http://localhost:${port}`),{method:req.method,headers,...(!['GET','HEAD'].includes(req.method)?{body:Buffer.concat(chunks)}:{})});
  const result=await worker.fetch(request,{DB:contactStore});
  res.writeHead(result.status,Object.fromEntries(result.headers));res.end(Buffer.from(await result.arrayBuffer()));
 }catch{res.writeHead(500);res.end('Preview failed')}
}).listen(port,host,()=>console.log(`Capture IV preview: http://${host==='0.0.0.0'?'127.0.0.1':host}:${port}\nContact inquiries are saved locally in the ignored .local/ folder; production uses the configured D1 database.`));
