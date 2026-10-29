const menu=document.querySelector('.menu'),nav=document.querySelector('.nav-links');menu?.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')!=='true';menu.setAttribute('aria-expanded',String(open));menu.setAttribute('aria-label',open?'Close menu':'Open menu');nav.classList.toggle('open',open)});const form=document.getElementById('contact-form'),status=document.getElementById('form-status');if(form){const preset=new URLSearchParams(location.search).get('inquiry');if(preset&&[...form.inquiryType.options].some(o=>o.value===preset))form.inquiryType.value=preset;form.addEventListener('submit',async e=>{e.preventDefault();if(!form.reportValidity())return;const button=form.querySelector('button[type=submit]');button.disabled=true;button.textContent='Saving…';status.className='form-status';status.textContent='';try{const values=Object.fromEntries(new FormData(form));const response=await fetch('/api/contact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});const result=await response.json();if(!response.ok)throw new Error(result.error||'Your inquiry could not be saved. Please try again.');status.classList.add('success');status.textContent='Thank you. Your inquiry has been saved for review. Reference: '+result.id;form.reset()}catch(error){status.classList.add('error');status.textContent=error.message||'Unable to save your inquiry. Please try again.'}finally{button.disabled=false;button.textContent='Submit ↗'}})}
const teamSize=document.getElementById('team-size');
if(teamSize){
 let billing='monthly';
 const summary=document.getElementById('team-summary'),teamError=document.getElementById('team-error'),demo=document.getElementById('pricing-demo'),enterpriseURL=demo.dataset.enterpriseUrl||demo.href;
 function updatePricing(){
  const raw=teamSize.value.trim(),size=Number(raw),valid=raw!==''&&teamSize.validity.valid&&Number.isInteger(size)&&size>=1&&size<=100000;
  const invalid=raw!==''&&!valid;
  teamSize.setAttribute('aria-invalid',String(invalid));teamError.hidden=!invalid;
  document.getElementById('price-period').textContent=billing==='annual'?'Your annual price':'Your monthly price';
  summary.textContent=valid?`Request ${billing} pricing for ${size.toLocaleString()} team ${size===1?'member':'members'}.`:invalid?'Check your team size to continue.':'Enter your team size to get started.';
  const params=new URLSearchParams();
  if(valid){params.set('team',String(size));params.set('billing',billing)}
  demo.href=enterpriseURL+(params.size?'?'+params.toString():'');
 }
 teamSize.addEventListener('input',updatePricing);
 document.querySelectorAll('[data-billing]').forEach(button=>button.addEventListener('click',()=>{billing=button.dataset.billing;document.querySelectorAll('[data-billing]').forEach(other=>{const selected=other===button;other.classList.toggle('selected',selected);other.setAttribute('aria-pressed',String(selected))});updatePricing()}));
 updatePricing();
}
if(form){const params=new URLSearchParams(location.search),size=Number(params.get('team')),billing=params.get('billing');if(Number.isInteger(size)&&size>0&&size<=100000&&['monthly','annual'].includes(billing))form.message.value=`I’d like a demo and ${billing} pricing for a team of ${size.toLocaleString()}.\n\nProject details: `;}
document.querySelectorAll('[data-carousel]').forEach(carousel=>{
 const track=carousel.querySelector('[data-carousel-track]'),prev=carousel.querySelector('[data-carousel-prev]'),next=carousel.querySelector('[data-carousel-next]'),count=carousel.querySelector('.carousel-count');
 if(!track||!prev||!next)return;
 const cards=[...track.children];
 const update=()=>{const step=cards[0]?.getBoundingClientRect().width+(parseFloat(getComputedStyle(track).columnGap)||0)||1;const index=Math.min(cards.length,Math.max(1,Math.round(track.scrollLeft/step)+1));if(count)count.innerHTML=String(index).padStart(2,'0')+' <i>/ '+String(cards.length).padStart(2,'0')+'</i>';prev.disabled=track.scrollLeft<4;next.disabled=track.scrollLeft+track.clientWidth>=track.scrollWidth-4};
 prev.addEventListener('click',()=>track.scrollBy({left:-track.clientWidth*.82,behavior:'smooth'}));
 next.addEventListener('click',()=>track.scrollBy({left:track.clientWidth*.82,behavior:'smooth'}));
 track.addEventListener('scroll',update,{passive:true});
 window.addEventListener('resize',update,{passive:true});
 update();
});

// Keep the glass navigation compact and legible as visitors move through the page.
const siteHeader=document.querySelector('.nav');
if(siteHeader){
 const updateHeader=()=>siteHeader.classList.toggle('is-scrolled',window.scrollY>24);
 window.addEventListener('scroll',updateHeader,{passive:true});
 updateHeader();
}

// Reveal page sections and content cards softly as they enter the viewport.
const revealTargets=document.querySelectorAll('.page-intro,.compact-section > article,.section,.workflow,.statement,.viewer-demo-copy,.viewer-demo,.platform-viewer-showcase,.software-story,.feature-showcase,.feature-card,.page-end,.cta,.client-card,.price-card,.package-card,.capability,.audience,.two-column > article');
const reducePageMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if(revealTargets.length&&!reducePageMotion&&'IntersectionObserver'in window){
 const revealObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('is-visible');revealObserver.unobserve(entry.target)}}),{threshold:.08,rootMargin:'0px 0px -5% 0px'});
 revealTargets.forEach((element,index)=>{element.classList.add('motion-reveal');element.style.setProperty('--reveal-delay',`${Math.min(index%5*60,240)}ms`);revealObserver.observe(element)});
}

// Animated visitor walkthrough: enter the code, move into the viewer, select a route, show its media.
const viewerDemo=document.querySelector('[data-viewer-demo]');
if(viewerDemo){
 const frame=viewerDemo.querySelector('[data-demo-frame]'),access=viewerDemo.querySelector('[data-demo-access]'),enterButton=viewerDemo.querySelector('[data-demo-enter]'),viewer=viewerDemo.querySelector('[data-demo-viewer]'),letters=[...viewerDemo.querySelectorAll('[data-demo-letter]')],status=viewerDemo.querySelector('[data-demo-status]'),message=viewerDemo.querySelector('[data-demo-message]'),title=viewerDemo.querySelector('[data-demo-title]'),popup=viewerDemo.querySelector('[data-demo-popup]'),popupTitle=viewerDemo.querySelector('[data-demo-popup-title]'),closePopup=viewerDemo.querySelector('[data-demo-close]'),cursor=viewerDemo.querySelector('.demo-cursor'),routes=[...viewerDemo.querySelectorAll('[data-demo-route]')],environmentButton=viewerDemo.querySelector('[data-demo-environment]'),environmentLayer=viewerDemo.querySelector('[data-demo-environment-layer]'),environmentHint=viewerDemo.querySelector('[data-demo-note="environment"]'),compareButton=viewerDemo.querySelector('[data-demo-compare-toggle]'),compareHint=viewerDemo.querySelector('[data-demo-note="compare"]'),compareLayer=viewerDemo.querySelector('[data-demo-compare-layer]'),mapCanvas=viewerDemo.querySelector('[data-demo-map]'),compareSlider=viewerDemo.querySelector('[data-demo-compare-slider]'),replayButton=viewerDemo.querySelector('[data-demo-replay]'),code='DEMO';
 const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 let timers=[],dragFrame=0,environmentTimer=0,compareInteracted=false,environmentCompleted=false;const later=(fn,ms)=>timers.push(setTimeout(fn,ms));const clear=()=>{timers.forEach(clearTimeout);timers=[];cancelAnimationFrame(dragFrame);dragFrame=0;environmentTimer=0};
 const setComparePosition=value=>{const position=Math.max(15,Math.min(85,Number(value)));mapCanvas.style.setProperty('--compare-position',position+'%');compareSlider.value=String(position);compareSlider.setAttribute('aria-valuetext',Math.round(position)+'% reveal of Sep 25, 2026');};
 const placeCursor=(x,y)=>{const parent=viewerDemo.getBoundingClientRect(),left=((x-parent.left-4)/parent.width*100),top=((y-parent.top-2)/parent.height*100);cursor.style.left=left+'%';cursor.style.top=top+'%';viewerDemo.style.setProperty('--demo-click-x',left+'%');viewerDemo.style.setProperty('--demo-click-y',top+'%');};
 const moveCursorTo=target=>{const rect=target.getBoundingClientRect();placeCursor(rect.left+rect.width/2,rect.top+rect.height/2);cursor.style.opacity='1';};
 const placeCursorAtDivider=position=>{const map=mapCanvas.getBoundingClientRect();placeCursor(map.left+map.width*position/100,map.top+map.height*.52);};
 routes.forEach(route=>route.addEventListener('click',()=>{clear();routes.forEach(item=>item.classList.remove('is-guided'));route.classList.add('is-active');compareButton.disabled=false;environmentButton.disabled=true;compareInteracted=false;if(compareHint){compareHint.hidden=false;compareHint.textContent='Click Compare to review another date'}if(environmentHint)environmentHint.hidden=true;moveCursorTo(route);viewerDemo.classList.add('demo-pointer-click');popupTitle.textContent=route.textContent.trim();popup.hidden=false;frame.classList.add('demo-popup-open');replayButton.hidden=false;status.textContent='Flight video opened · '+route.textContent.trim()+'. Press Compare when ready.'}));
 environmentButton?.addEventListener('click',()=>{if(environmentButton.disabled)return;const open=environmentButton.getAttribute('aria-pressed')!=='true';environmentButton.setAttribute('aria-pressed',String(open));environmentLayer.hidden=!open;frame.classList.toggle('environment-active',open);if(compareHint)compareHint.hidden=true;if(environmentHint){environmentHint.hidden=false;environmentHint.textContent=open?'Mock 3D site model · illustrative only':'Click Environment to open the 3D model'}if(open){environmentCompleted=true;status.textContent='Mock 3D environment preview · walkthrough complete.';environmentTimer=setTimeout(runDemo,4800);timers.push(environmentTimer)}else{clearTimeout(environmentTimer);if(environmentCompleted){status.textContent='Walkthrough complete · restarting shortly.';environmentTimer=setTimeout(runDemo,1500);timers.push(environmentTimer)}else status.textContent='Environment preview closed · press Environment to continue.'}});
 compareButton?.addEventListener('click',()=>{if(compareButton.disabled)return;const open=compareButton.getAttribute('aria-pressed')!=='true';compareButton.setAttribute('aria-pressed',String(open));compareLayer.hidden=!open;mapCanvas.classList.toggle('demo-comparing',open);popup.hidden=true;frame.classList.remove('demo-popup-open');compareInteracted=false;environmentButton.disabled=true;if(open)setComparePosition(50);if(environmentHint)environmentHint.hidden=true;if(compareHint){compareHint.hidden=false;compareHint.textContent=open?'Drag the blue handle to compare dates':'Click Compare to reveal the date split'}status.textContent=open?'Compare view active · drag the divider to continue.':'Compare view closed · press Compare when ready.'});
 compareSlider?.addEventListener('input',()=>{setComparePosition(compareSlider.value);if(!compareInteracted&&compareButton?.getAttribute('aria-pressed')==='true'){compareInteracted=true;environmentButton.disabled=false;if(compareHint)compareHint.hidden=true;if(environmentHint){environmentHint.hidden=false;environmentHint.textContent='Press Environment to open the 3D site model'}status.textContent='Dates compared · press Environment for the 3D preview.'}});
 closePopup?.addEventListener('click',()=>{popup.hidden=true;frame.classList.remove('demo-popup-open');status.textContent='Flight video ready · press Compare when you want to continue.'});
 const reset=()=>{clear();frame.classList.remove('demo-map-active','demo-clicking','demo-popup-open','environment-active');viewerDemo.classList.remove('demo-pointer-click');access.hidden=false;viewer.hidden=false;later(()=>viewer.hidden=true,650);popup.hidden=true;environmentLayer.hidden=true;compareLayer.hidden=true;compareButton?.setAttribute('aria-pressed','false');if(compareButton)compareButton.disabled=true;if(environmentButton)environmentButton.disabled=true;compareInteracted=false;environmentCompleted=false;mapCanvas?.classList.remove('demo-comparing');mapCanvas?.style.removeProperty('--compare-position');environmentButton?.setAttribute('aria-pressed','false');if(compareHint){compareHint.hidden=true;compareHint.textContent='Click to compare capture dates'}if(environmentHint){environmentHint.hidden=true;environmentHint.textContent='Open the illustrative 3D site model'}replayButton.hidden=true;cursor.classList.remove('is-dragging');routes.forEach(route=>route.classList.remove('is-active','is-guided'));letters.forEach(letter=>letter.textContent='');cursor.style.opacity='0';title.innerHTML='Welcome to your<br>project site.';message.textContent='Enter the access code shared by your project team.';status.textContent='Waiting for project code…';};
 const showViewer=()=>{
  frame.classList.remove('demo-clicking');
  access.hidden=false;viewer.hidden=false;frame.classList.add('demo-map-active');
  later(()=>access.hidden=true,650);
  title.textContent='Project access granted.';
  message.textContent='Choose a route to preview its flight video.';
  status.textContent='Move the pointer to a route · click to continue';
  // Align the illustrated pointer to the live route position at every viewport size.
  moveCursorTo(routes[0]);
  later(()=>routes[0].classList.add('is-guided'),reducedMotion?0:700);
 };
 enterButton?.addEventListener('click',showViewer);
 const runDemo=()=>{
  reset();
  const typeCode=()=>letters.forEach((letter,index)=>{later(()=>{letter.textContent=code[index];if(index===code.length-1)status.textContent='Code entered · click Access Project to continue'},reducedMotion?0:650+index*250)});
  status.textContent='Entering the example access code…';typeCode();
 };
 replayButton?.addEventListener('click',runDemo);
 if('IntersectionObserver'in window){const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){runDemo();observer.disconnect()}},{threshold:.35});observer.observe(viewerDemo)}else runDemo();
}

// Platform site-view demo: animate a cursor to a capture date and open its video card.
const platformDemo=document.querySelector('[data-platform-demo]');
if(platformDemo){
 const canvas=platformDemo.querySelector('.platform-map-canvas'),dateButton=platformDemo.querySelector('[data-platform-date]'),modal=platformDemo.querySelector('[data-platform-modal]'),closeButton=platformDemo.querySelector('[data-platform-close]'),environmentButton=platformDemo.querySelector('[data-platform-environment]'),environmentLayer=platformDemo.querySelector('[data-platform-environment-layer]'),reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 environmentButton?.addEventListener('click',()=>{const open=environmentButton.getAttribute('aria-pressed')!=='true';environmentButton.setAttribute('aria-pressed',String(open));environmentLayer.hidden=!open;canvas.classList.toggle('environment-active',open)});
 let timer,observer;
 const openVideo=()=>{canvas.classList.remove('demo-running');canvas.classList.add('demo-modal-open');modal.hidden=false;dateButton.setAttribute('aria-expanded','true');if(!reducedMotion)timer=setTimeout(()=>{closeVideo();runDateDemo()},3800)};
 const closeVideo=()=>{clearTimeout(timer);canvas.classList.remove('demo-modal-open');modal.hidden=true;dateButton.setAttribute('aria-expanded','false')};
 const runDateDemo=()=>{if(reducedMotion)return;closeVideo();canvas.classList.remove('demo-running');void canvas.offsetWidth;canvas.classList.add('demo-running');timer=setTimeout(()=>dateButton.click(),1770)};
 dateButton.addEventListener('click',openVideo);closeButton.addEventListener('click',()=>{closeVideo();if(!reducedMotion)timer=setTimeout(runDateDemo,1200)});
 if(!reducedMotion){if('IntersectionObserver'in window){observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){runDateDemo();observer.disconnect()}},{threshold:.35});observer.observe(platformDemo)}else runDateDemo()}
}
