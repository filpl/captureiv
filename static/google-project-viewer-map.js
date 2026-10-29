(()=>{
  const root=document.querySelector('#viewer-site-map');if(!root)return;
  const $=selector=>document.querySelector(selector),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const init=()=>{
    if(!window.google?.maps)return;
    const center={lat:Number(root.dataset.centerLat)||39.8283,lng:Number(root.dataset.centerLng)||-98.5795};
    const map=new google.maps.Map($('#viewer-live-map'),{center,zoom:Number(root.dataset.zoom)||4,mapTypeId:'roadmap',mapTypeControl:false,streetViewControl:false,fullscreenControl:true});
    window.projectViewerMap=map;
    $('#viewer-layer-toggle')?.addEventListener('click',()=>{const satellite=map.getMapTypeId()!=='satellite';map.setMapTypeId(satellite?'satellite':'roadmap');$('#viewer-layer-toggle').textContent=satellite?'Map':'Satellite'});
    const bounds=new google.maps.LatLngBounds();
    try{const boundary=JSON.parse(root.dataset.boundary||'[]'),boundaryName=root.dataset.boundaryName||'';if(Array.isArray(boundary)&&boundary.length>=3){const polygon=new google.maps.Polygon({map,paths:boundary.map(([lat,lng])=>({lat,lng})),strokeColor:'#d5683b',strokeWeight:3,fillColor:'#e98a55',fillOpacity:.14,clickable:Boolean(boundaryName)});boundary.forEach(([lat,lng])=>bounds.extend({lat,lng}));if(boundaryName){const info=new google.maps.InfoWindow({content:`<strong>${esc(boundaryName)}</strong>`});polygon.addListener('click',event=>{info.setPosition(event.latLng);info.open({map})})}map.fitBounds(bounds,24)}}catch{}
    const paths=JSON.parse($('#viewer-flight-paths')?.textContent||'[]'),colors=['#e17749','#397d59','#4c83c8','#8b66a5'];
    paths.forEach((path,index)=>{if(!Array.isArray(path.points)||path.points.length<2)return;const line=new google.maps.Polyline({map,path:path.points.map(([lat,lng])=>({lat,lng})),strokeColor:colors[index%colors.length],strokeOpacity:.9,strokeWeight:4});line.addListener('click',()=>{if(!path.flight_id)return;const url=new URL(location.href);url.searchParams.set('tab','map');url.searchParams.set('date',root.dataset.date);url.searchParams.set('flight',path.flight_id);location.href=url.toString()});path.points.forEach(([lat,lng])=>bounds.extend({lat,lng}))});
    if(!bounds.isEmpty())map.fitBounds(bounds,24);
    $('#mapAttribution')?.replaceChildren(document.createTextNode('Map data © Google'));
    google.maps.event.addListenerOnce(map,'idle',()=>setTimeout(()=>google.maps.event.trigger(map,'resize'),60));
  };
  window.initProjectViewerGoogleMap=()=>{init();window.initProjectViewerCompareMaps?.()};
  if(window.google?.maps)init();
})();
