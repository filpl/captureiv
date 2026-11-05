(()=>{
  const root=document.querySelector('#viewer-site-map');if(!root||!window.L)return;
  const $=s=>document.querySelector(s),module=root.dataset.module||'map',date=root.dataset.date;
  const initialCenter=L.latLng(Number(root.dataset.centerLat)||40.2744,Number(root.dataset.centerLng)||-76.8871),initialZoom=Number(root.dataset.zoom)||16;
  const map=L.map('viewer-live-map',{zoomControl:false,scrollWheelZoom:true,zoomSnap:.5,zoomDelta:.5}).setView(initialCenter,initialZoom);
  L.control.zoom({position:'topright'}).addTo(map);
  map.createPane('basemapPane');map.getPane('basemapPane').style.zIndex=200;map.createPane('referencePane');map.getPane('referencePane').style.zIndex=250;
  const streets=L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'© OpenStreetMap contributors',pane:'basemapPane'}).addTo(map);
  const satellite=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles © Esri',pane:'basemapPane'});
  let satelliteOn=false;
  $('#viewer-layer-toggle')?.addEventListener('click',()=>{if(satelliteOn){map.removeLayer(satellite);streets.addTo(map);satelliteOn=false;$('#viewer-layer-toggle').textContent='Satellite'}else{map.removeLayer(streets);satellite.addTo(map);satelliteOn=true;$('#viewer-layer-toggle').textContent='Map'}});
  let imageBounds=null,imageOverlay=null;const imagePinMarkers=[];
  if(module==='image'){
    map.getPane('basemapPane').style.visibility='hidden';
    if(root.dataset.imageUrl){imageBounds=map.getBounds();imageOverlay=L.imageOverlay(root.dataset.imageUrl,imageBounds,{pane:'referencePane'}).addTo(map)}else $('#viewer-map-empty').hidden=false;
  }
  const paths=JSON.parse($('#viewer-flight-paths')?.textContent||'[]'),colors=['#e17749','#397d59','#4c83c8','#8b66a5'];
  const pathsByDate=JSON.parse($('#viewer-flight-paths-by-date')?.textContent||'{}');
  let selectedRouteLine=null,selectedRouteMarker=null;
  const selectRoute=(path,line,marker,latlng)=>{
    selectedRouteLine?.setStyle({weight:4,opacity:.92});
    selectedRouteMarker?.getElement()?.classList.remove('selected');
    selectedRouteLine=line;selectedRouteMarker=marker;line.setStyle({weight:7,opacity:1});marker.getElement()?.classList.add('selected');
    const name=String(path.name||path.label||path.flight_title||'Flight route').trim();
    const safeName=escapeRouteLabel(name),safeDirection=escapeRouteLabel(path.direction||'Custom path');
    let videoLink='';
    if(path.flight_id){const url=new URL(location.href);url.searchParams.set('tab','map');url.searchParams.set('date',date);url.searchParams.set('flight',path.flight_id);videoLink=`<a class="viewer-route-popup-link" href="${escapeRouteLabel(url.toString())}">View linked flight →</a>`}
    const content=`<div class="viewer-route-popup-content"><strong>${safeName}</strong><span>${safeDirection}</span>${videoLink||'<small>No video is linked to this route.</small>'}</div>`;
    L.popup({maxWidth:240,autoPan:true,className:'viewer-route-popup'}).setLatLng(latlng||line.getBounds().getCenter()).setContent(content).openOn(map);
  };
  paths.forEach((path,index)=>{
    if(!Array.isArray(path.points)||path.points.length<2)return;
    const color=colors[index%colors.length],line=L.polyline(path.points,{color,weight:4,opacity:.92,dashArray:'9 8',lineCap:'butt'}).addTo(map);
    const label=String(path.name||path.label||path.flight_title||`Route ${index+1}`).trim();
    const safeLabel=label.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const html=`<span style="--route-color:${color}"><i aria-hidden="true"></i><b>${safeLabel}</b></span>`;
    const icon=L.divIcon({className:'viewer-route-start',html,iconSize:[Math.min(210,Math.max(82,label.length*7+38)),30],iconAnchor:[0,15]});
    const marker=L.marker(path.points[0],{icon,keyboard:true,title:label}).addTo(map);
    line.on('click',event=>selectRoute(path,line,marker,event.latlng));
    line.on('mouseover',()=>{if(selectedRouteLine!==line)line.setStyle({weight:6,opacity:1})});
    line.on('mouseout',()=>{if(selectedRouteLine!==line)line.setStyle({weight:4,opacity:.92})});
    marker.on('click',event=>selectRoute(path,line,marker,event.latlng));
  });
  const imageLatLng=(x,y,scale)=>[imageBounds.getNorth()-(imageBounds.getNorth()-imageBounds.getSouth())*Number(y)/scale,imageBounds.getWest()+(imageBounds.getEast()-imageBounds.getWest())*Number(x)/scale];
  if(module==='image'&&imageBounds){(window.PROJECT_MAP_PINS||[]).forEach(pin=>{const marker=L.marker(imageLatLng(pin.x,pin.y,100),{icon:L.divIcon({className:'viewer-photo-pin',html:`<span>${String(pin.direction||'•').replace(/[&<>"']/g,'')}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}).addTo(map).bindTooltip(pin.label||pin.direction);imagePinMarkers.push({marker,x:pin.x,y:pin.y,scale:100})})}
  if(module==='image'&&imageBounds){
    const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    (window.PROJECT_PHOTO_PINS||[]).forEach(pin=>{
      const x=Number(pin.x),y=Number(pin.y);if(!Number.isFinite(x)||!Number.isFinite(y))return;
      const marker=L.marker(imageLatLng(x,y,1000),{icon:L.divIcon({className:'viewer-photo-pin viewer-photo-pin-gallery',html:'<span aria-hidden="true">▧</span>',iconSize:[24,30],iconAnchor:[12,27]}),keyboard:true,title:pin.label||'Mapped photos'}).addTo(map);
      imagePinMarkers.push({marker,x,y,scale:1000});
      const photos=Array.isArray(pin.photos)?pin.photos:[];
      const gallery=photos.length?`<div class="viewer-photo-gallery">${photos.map(photo=>`<a href="${escapeHTML(photo.url)}" target="_blank" rel="noopener"><img src="${escapeHTML(photo.url)}" alt="${escapeHTML(photo.name)}" loading="lazy"><span>${escapeHTML(photo.name)}</span></a>`).join('')}</div>`:'<p class="viewer-photo-empty">No pictures have been added to this location yet.</p>';
      marker.bindPopup(`<div class="viewer-photo-popup"><strong>${escapeHTML(pin.label||'Site photos')}</strong>${gallery}</div>`,{
        maxWidth:Math.max(180,Math.min(320,map.getSize().x-32)),
        maxHeight:Math.max(140,Math.min(360,window.innerHeight-120)),
        autoPan:true,
        autoPanPaddingTopLeft:[20,20],
        autoPanPaddingBottomRight:[20,20],
        keepInView:true,
      });
    });
  }
  let orthoBounds=null;
  if(module==='ortho'&&root.dataset.orthoUrl){map.getPane('basemapPane').style.visibility='hidden';fetch(root.dataset.orthoUrl).then(r=>{if(!r.ok)throw Error('Orthomosaic unavailable');return r.arrayBuffer()}).then(bytes=>window.parseGeoraster(bytes)).then(georaster=>{const raster=new window.GeoRasterLayer({georaster,resolution:256,pane:'referencePane'}).addTo(map);const b=raster.getBounds();if(b?.isValid()){orthoBounds=b;map.fitBounds(b,{padding:[24,24],maxZoom:20})}}).catch(()=>{$('#viewer-map-empty').textContent='This orthomosaic could not be loaded.';$('#viewer-map-empty').hidden=false})}
  if(module==='ortho'&&!root.dataset.orthoUrl){map.getPane('basemapPane').style.visibility='hidden';$('#viewer-map-empty').hidden=false}
  const fitSiteImage=()=>{
    map.invalidateSize({pan:false});
    if(module==='image'&&imageOverlay){
      const image=imageOverlay.getElement(),size=map.getSize();
      if(image?.naturalWidth&&image?.naturalHeight&&size.x&&size.y){
        // Keep the source photo's proportions and cover the map viewport. A
        // wide photo extends past the map's sides (or a tall one past top/bottom)
        // instead of being stretched or leaving gray bands around it.
        const imageRatio=image.naturalWidth/image.naturalHeight,viewRatio=size.x/size.y;
        let width=size.x,height=size.y;
        if(imageRatio>viewRatio)width=size.y*imageRatio;else height=size.x/imageRatio;
        const left=(size.x-width)/2,top=(size.y-height)/2;
        const northWest=map.containerPointToLatLng([left,top]);
        const southEast=map.containerPointToLatLng([left+width,top+height]);
        imageBounds=L.latLngBounds(southEast,northWest);
        imageOverlay.setBounds(imageBounds);
        imagePinMarkers.forEach(({marker,x,y,scale})=>marker.setLatLng(imageLatLng(x,y,scale)));
      }
    }
    const popup=map._popup;if(popup?.isOpen())map.panInside(popup.getLatLng(),{padding:[24,24]});
  };
  const fitControl=L.control({position:'topright'});
  fitControl.onAdd=()=>{
    const button=L.DomUtil.create('button','leaflet-control-fit-view');
    button.type='button';button.title='Fit site to screen';button.setAttribute('aria-label','Fit site to screen');
    button.innerHTML='<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 3H3v4M13 3h4v4M17 13v4h-4M7 17H3v-4M3 7l5-5M17 7l-5-5M17 13l-5 5M3 13l5 5"/></svg>';
    L.DomEvent.disableClickPropagation(button);
    L.DomEvent.on(button,'click',()=>{
      if(module==='ortho'&&orthoBounds){map.fitBounds(orthoBounds,{padding:[24,24],maxZoom:20,animate:true});return}
      const alreadyAtStart=map.getCenter().equals(initialCenter)&&map.getZoom()===initialZoom;
      if(module==='image'&&!alreadyAtStart)map.once('moveend',fitSiteImage);
      map.setView(initialCenter,initialZoom,{animate:true});
      if(module==='image'&&alreadyAtStart)fitSiteImage();
    });
    return button;
  };
  fitControl.addTo(map);
  const compareRoot=$('#viewer-compare'),compareToggle=$('#compare-toggle'),compareClose=$('#compare-close');
  let compareMaps=[],compareInitialized=false;
  const escapeRouteLabel=value=>String(value||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const addCompareRoutes=(targetMap,routeLayer,routeDate)=>{
    routeLayer.clearLayers();
    (pathsByDate[routeDate]||[]).forEach((path,index)=>{
      if(!Array.isArray(path.points)||path.points.length<2)return;
      const color=colors[index%colors.length],route=L.polyline(path.points,{color,weight:4,opacity:.92,dashArray:'9 8',lineCap:'butt'}).addTo(routeLayer);
      const openVideo=()=>{if(path.flight_id){const u=new URL(location.href);u.searchParams.set('tab','map');u.searchParams.set('date',routeDate);u.searchParams.set('flight',path.flight_id);location.href=u.toString()}};
      route.on('click',openVideo);
      const label=String(path.label||path.name||path.flight_title||`Route ${index+1}`).trim();
      const html=`<span style="--route-color:${color}"><i aria-hidden="true"></i><b>${escapeRouteLabel(label)}</b></span>`;
      const icon=L.divIcon({className:'viewer-route-start',html,iconSize:[Math.min(210,Math.max(82,label.length*7+38)),30],iconAnchor:[0,15]});
      L.marker(path.points[0],{icon,keyboard:true,title:label}).addTo(routeLayer).on('click',openVideo);
    });
  };
  const createCompareMap=(elementId,routeDate)=>{
    const paneMap=L.map(elementId,{zoomControl:false,scrollWheelZoom:true,zoomSnap:.5,zoomDelta:.5}).setView(map.getCenter(),map.getZoom());
    L.control.zoom({position:'topright'}).addTo(paneMap);
    paneMap.createPane('basemapPane');paneMap.getPane('basemapPane').style.zIndex=200;
    paneMap.createPane('referencePane');paneMap.getPane('referencePane').style.zIndex=250;
    const routeLayer=L.layerGroup().addTo(paneMap);
    if(module==='image'){
      if(root.dataset.imageUrl)L.imageOverlay(root.dataset.imageUrl,imageBounds||map.getBounds(),{pane:'referencePane'}).addTo(paneMap);
    }else if(module==='ortho'){
      if(root.dataset.orthoUrl)fetch(root.dataset.orthoUrl).then(response=>{if(!response.ok)throw Error('Orthomosaic unavailable');return response.arrayBuffer()}).then(bytes=>window.parseGeoraster(bytes)).then(georaster=>{
        const raster=new window.GeoRasterLayer({georaster,resolution:256,pane:'referencePane'}).addTo(paneMap),bounds=raster.getBounds();
        if(bounds?.isValid())paneMap.fitBounds(bounds,{padding:[24,24],maxZoom:20});
      }).catch(()=>{});
    }else{
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'© OpenStreetMap contributors',pane:'basemapPane'}).addTo(paneMap);
      if(satelliteOn){paneMap.eachLayer(layer=>{if(layer instanceof L.TileLayer)paneMap.removeLayer(layer)});L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles © Esri',pane:'basemapPane'}).addTo(paneMap)}
    }
    if(module==='image'){
      const bounds=imageBounds||map.getBounds();
      (window.PROJECT_MAP_PINS||[]).forEach(pin=>{
        const lat=bounds.getNorth()-(bounds.getNorth()-bounds.getSouth())*Number(pin.y)/100,lng=bounds.getWest()+(bounds.getEast()-bounds.getWest())*Number(pin.x)/100;
        L.marker([lat,lng],{icon:L.divIcon({className:'viewer-photo-pin',html:`<span>${String(pin.direction||'•').replace(/[&<>"']/g,'')}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}).addTo(paneMap).bindTooltip(pin.label||pin.direction);
      });
    }
    addCompareRoutes(paneMap,routeLayer,routeDate);
    return {map:paneMap,routeLayer};
  };
  const refreshCompareDates=()=>{
    const leftDate=$('#compare-date-left')?.value,rightDate=$('#compare-date-right')?.value;
    if(compareMaps.length===2){
      addCompareRoutes(compareMaps[0].map,compareMaps[0].routeLayer,leftDate);
      addCompareRoutes(compareMaps[1].map,compareMaps[1].routeLayer,rightDate);
      $('#compare-empty-left').hidden=!(pathsByDate[leftDate]||[]).some(path=>Array.isArray(path.points)&&path.points.length>1);
      $('#compare-empty-right').hidden=!(pathsByDate[rightDate]||[]).some(path=>Array.isArray(path.points)&&path.points.length>1);
    }
  };
  const initializeCompare=()=>{
    if(compareInitialized)return;
    compareMaps=[createCompareMap('viewer-compare-map-left',$('#compare-date-left').value),createCompareMap('viewer-compare-map-right',$('#compare-date-right').value)];
    let syncing=false;
    compareMaps.forEach((source,index)=>source.map.on('moveend zoomend',()=>{
      if(syncing)return;
      const target=compareMaps[index===0?1:0].map;
      if(source.map.getCenter().equals(target.getCenter())&&source.map.getZoom()===target.getZoom())return;
      syncing=true;target.setView(source.map.getCenter(),source.map.getZoom(),{animate:false});syncing=false;
    }));
    compareInitialized=true;
    requestAnimationFrame(()=>compareMaps.forEach(item=>item.map.invalidateSize({pan:false})));
  };
  const setCompareOpen=open=>{
    if(!compareRoot||!compareToggle)return;
    compareRoot.hidden=!open;$('#viewer-live-map').hidden=open;root.classList.toggle('comparison-active',open);compareToggle.setAttribute('aria-expanded',String(open));
    if(open){initializeCompare();refreshCompareDates();requestAnimationFrame(()=>compareMaps.forEach(item=>item.map.invalidateSize({pan:false})));}
    else{
      if(compareMaps.length===2)map.setView(compareMaps[0].map.getCenter(),compareMaps[0].map.getZoom(),{animate:false});
      map.invalidateSize({pan:false});
    }
  };
  compareToggle?.addEventListener('click',()=>setCompareOpen(compareRoot.hidden));
  compareClose?.addEventListener('click',()=>setCompareOpen(false));
  $('#compare-date-left')?.addEventListener('change',refreshCompareDates);
  $('#compare-date-right')?.addEventListener('change',refreshCompareDates);
  const divider=$('#compare-divider'),comparePanes=$('#compare-panes');
  if(divider)L.DomEvent.disableClickPropagation(divider);
  const isStackedCompare=()=>window.matchMedia('(max-width:700px)').matches;
  const updateDividerOrientation=()=>{if(divider){const stacked=isStackedCompare();divider.setAttribute('aria-orientation',stacked?'horizontal':'vertical');divider.setAttribute('aria-label',stacked?'Drag to resize comparison panes':'Drag to resize comparison panes')}};
  updateDividerOrientation();
  let splitPercent=50,draggingDivider=false;
  const setSplit=value=>{
    splitPercent=Math.max(20,Math.min(80,value));
    comparePanes?.style.setProperty('--split',`${splitPercent}%`);
    divider?.setAttribute('aria-valuenow',String(Math.round(splitPercent)));
    requestAnimationFrame(()=>compareMaps.forEach(item=>item.map.invalidateSize({pan:false})));
  };
  divider?.addEventListener('pointerdown',event=>{draggingDivider=true;divider.setPointerCapture(event.pointerId);event.preventDefault()});
  divider?.addEventListener('pointermove',event=>{
    if(!draggingDivider||!comparePanes)return;
    const bounds=comparePanes.getBoundingClientRect();
    setSplit(isStackedCompare()?((event.clientY-bounds.top)/bounds.height)*100:((event.clientX-bounds.left)/bounds.width)*100);
  });
  divider?.addEventListener('pointerup',()=>{draggingDivider=false});
  divider?.addEventListener('pointercancel',()=>{draggingDivider=false});
  divider?.addEventListener('keydown',event=>{
    const delta=event.shiftKey?10:3;
    if(event.key==='ArrowLeft'||event.key==='ArrowUp'){setSplit(splitPercent-delta);event.preventDefault()}
    if(event.key==='ArrowRight'||event.key==='ArrowDown'){setSplit(splitPercent+delta);event.preventDefault()}
  });
  window.addEventListener('resize',updateDividerOrientation);
  imageOverlay?.on('load',fitSiteImage);
  map.whenReady(()=>requestAnimationFrame(fitSiteImage));
  let fitFrame=0;
  const scheduleImageFit=()=>{if(fitFrame)return;fitFrame=requestAnimationFrame(()=>{fitFrame=0;fitSiteImage()})};
  window.addEventListener('resize',scheduleImageFit);
  if(module==='image'&&window.ResizeObserver){
    const resizeObserver=new ResizeObserver(scheduleImageFit);
    resizeObserver.observe(map.getContainer());
  }
})();
