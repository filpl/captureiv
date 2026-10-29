const inquiryTypes=new Set(['Platform Demo','Plans & Pricing','Project Support','Capture & Scanning','General Inquiry']);
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});

export async function contact(request,env){
  if(request.method!=='POST')return json({error:'Method not allowed.'},405);
  const origin=request.headers.get('Origin');
  if(origin&&origin!==new URL(request.url).origin)return json({error:'Please submit the form from this website.'},403);
  if(!request.headers.get('Content-Type')?.startsWith('application/json'))return json({error:'JSON request required.'},415);
  if(Number(request.headers.get('Content-Length'))>32000)return json({error:'The message is too large.'},413);
  let body;
  try{
    const reader=request.body?.getReader();if(!reader)return json({error:'Please complete the form.'},400);
    let size=0;const chunks=[];
    for(;;){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>32000){await reader.cancel();return json({error:'The message is too large.'},413)}chunks.push(value)}
    const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length}
    body=JSON.parse(new TextDecoder().decode(bytes));
  }catch{return json({error:'Please check the form and try again.'},400)}
  if(!body||Array.isArray(body)||typeof body!=='object')return json({error:'Please complete the form.'},400);
  if(body.website)return json({error:'Unable to accept this inquiry.'},400);
  const limits={firstName:80,lastName:80,email:254,company:180,inquiryType:80,message:5000};const fields={};
  for(const [key,max] of Object.entries(limits)){
    if(typeof body[key]!=='string'||!body[key].trim()||body[key].trim().length>max)return json({error:'Please complete all fields within their length limits.'},400);
    fields[key]=body[key].trim();
  }
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(fields.email)||fields.message.length<10||!inquiryTypes.has(fields.inquiryType))return json({error:'Please enter a valid email, inquiry type and message of at least 10 characters.'},400);
  if(!env.DB)return json({error:'Inquiry saving is temporarily unavailable. Please try again later.'},503);
  const rateSource=request.headers.get('CF-Connecting-IP')||'unknown';
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode('construct-iv-contact:'+rateSource));
  const rateKey=Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');
  const id=crypto.randomUUID();const now=Date.now();
  try{
    const result=await env.DB.prepare(`INSERT INTO contact_inquiries
      (id,first_name,last_name,email,company,inquiry_type,message,status,email_delivery,created_at,rate_key)
      SELECT ?,?,?,?,?,?,?,'new','not_configured',?,?
      WHERE (SELECT COUNT(*) FROM contact_inquiries WHERE rate_key=? AND created_at>?)<5`)
      .bind(id,fields.firstName,fields.lastName,fields.email,fields.company,fields.inquiryType,fields.message,now,rateKey,rateKey,now-3600000).run();
    if(!result.success)throw new Error('storage unavailable');
    if(!result.meta?.changes)return json({error:'Too many inquiries have been submitted recently. Please try again later.'},429);
    return json({id,saved:true},201);
  }catch{return json({error:'Your inquiry could not be saved. Please try again later.'},503)}
}
