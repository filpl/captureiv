(()=>{
  const root=document.querySelector('#viewer-site-map');if(!root||!window.L)return;
  const $=s=>document.querySelector(s),module=root.dataset.module||'map',date=root.dataset.date;
  const map=L.map('viewer-live-map',{zoomControl:true,scrollWheelZoom:true}).setView([Number(root.dataset.centerLat)||40.2744,Number(root.dataset.centerLng)||-76.8871],Number(root.dataset.zoom)||16);
  map.createPane('basemapPane');map.getPane('basemapPane').style.zIndex=200;map.createPane('referencePane');map.getPane('referencePane').style.zIndex=250;
  const streets=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'© OpenStreetMap contributors',pane:'basemapPane'}).addTo(map);
  const satellite=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles © Esri',pane:'basemapPane'});
  let satelliteOn=false;
  $('#viewer-layer-toggle')?.addEventListener('click',()=>{if(satelliteOn){map.removeLayer(satellite);streets.addTo(map);satelliteOn=false;$('#viewer-layer-toggle').textContent='Satellite'}else{map.removeLayer(streets);satellite.addTo(map);satelliteOn=true;$('#viewer-layer-toggle').textContent='Map'}});
  let imageBounds=null;
  if(module==='image'){
    map.getPane('basemapPane').style.visibility='hidden';
    if(root.dataset.imageUrl){imageBounds=map.getBounds();L.imageOverlay(root.dataset.imageUrl,imageBounds,{pane:'referencePane'}).addTo(map)}else $('#viewer-map-empty').hidden=false;
  }
  const paths=JSON.parse($('#viewer-flight-paths')?.textContent||'[]'),colors=['#e17749','#397d59','#4c83c8','#8b66a5'];
  paths.forEach((path,index)=>{
    if(!Array.isArray(path.points)||path.points.length<2)return;
    const color=colors[index%colors.length],line=L.polyline(path.points,{color,weight:4,opacity:.88}).addTo(map);
    const openVideo=()=>{if(path.flight_id){const u=new URL(location.href);u.searchParams.set('tab','map');u.searchParams.set('date',date);u.searchParams.set('flight',path.flight_id);location.href=u.toString()}};
    line.on('click',openVideo);line.on('mouseover',()=>line.setStyle({weight:7,opacity:1}));line.on('mouseout',()=>line.setStyle({weight:4,opacity:.88}));
    const label=String(path.label||'').trim(),html=label?`<span>${label.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}</span>`:'<span>▶</span>';
    const icon=L.divIcon({className:'viewer-route-start',html,iconSize:[label?Math.min(170,Math.max(66,label.length*7+22)):38,32],iconAnchor:[19,16]});
    const marker=L.marker(path.points[0],{icon,keyboard:true,title:path.flight_title||path.name}).addTo(map);if(path.flight_id)marker.on('click',openVideo);else marker.bindTooltip(path.name);
  });
  if(module==='image'&&imageBounds){(window.PROJECT_MAP_PINS||[]).forEach(pin=>{const lat=imageBounds.getNorth()-(imageBounds.getNorth()-imageBounds.getSouth())*Number(pin.y)/100,lng=imageBounds.getWest()+(imageBounds.getEast()-imageBounds.getWest())*Number(pin.x)/100;L.marker([lat,lng],{icon:L.divIcon({className:'viewer-photo-pin',html:`<span>${String(pin.direction||'•').replace(/[&<>"']/g,'')}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}).addTo(map).bindTooltip(pin.label||pin.direction)})}
  if(module==='image'&&imageBounds){
    const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    (window.PROJECT_PHOTO_PINS||[]).forEach(pin=>{
      const x=Number(pin.x),y=Number(pin.y);if(!Number.isFinite(x)||!Number.isFinite(y))return;
      const lat=imageBounds.getNorth()-(imageBounds.getNorth()-imageBounds.getSouth())*(y/620),lng=imageBounds.getWest()+(imageBounds.getEast()-imageBounds.getWest())*(x/1000);
      const marker=L.marker([lat,lng],{icon:L.divIcon({className:'viewer-photo-pin viewer-photo-pin-gallery',html:'<span aria-hidden="true">▧</span>',iconSize:[24,30],iconAnchor:[12,27]}),keyboard:true,title:pin.label||'Mapped photos'}).addTo(map);
      const photos=Array.isArray(pin.photos)?pin.photos:[];
      const gallery=photos.length?`<div class="viewer-photo-gallery">${photos.map(photo=>`<a href="${escapeHTML(photo.url)}" target="_blank" rel="noopener"><img src="${escapeHTML(photo.url)}" alt="${escapeHTML(photo.name)}" loading="lazy"><span>${escapeHTML(photo.name)}</span></a>`).join('')}</div>`:'<p class="viewer-photo-empty">No pictures have been added to this location yet.</p>';
      marker.bindPopup(`<div class="viewer-photo-popup"><strong>${escapeHTML(pin.label||'Site photos')}</strong>${gallery}</div>`,{maxWidth:300});
    });
  }
  if(module==='ortho'&&root.dataset.orthoUrl){map.getPane('basemapPane').style.visibility='hidden';fetch(root.dataset.orthoUrl).then(r=>{if(!r.ok)throw Error('Orthomosaic unavailable');return r.arrayBuffer()}).then(bytes=>window.parseGeoraster(bytes)).then(georaster=>{const raster=new window.GeoRasterLayer({georaster,resolution:256,pane:'referencePane'}).addTo(map);const b=raster.getBounds();if(b?.isValid())map.fitBounds(b,{padding:[12,12],maxZoom:20})}).catch(()=>{$('#viewer-map-empty').textContent='This orthomosaic could not be loaded.';$('#viewer-map-empty').hidden=false})}
  if(module==='ortho'&&!root.dataset.orthoUrl){map.getPane('basemapPane').style.visibility='hidden';$('#viewer-map-empty').hidden=false}
  map.whenReady(()=>setTimeout(()=>map.invalidateSize(),100));window.addEventListener('resize',()=>map.invalidateSize());
})();
