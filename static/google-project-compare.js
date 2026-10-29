(()=>{
  const root=document.querySelector('#viewer-site-map');
  const toggle=document.querySelector('#compare-toggle');
  const panel=document.querySelector('#viewer-compare');
  if(!root||!toggle||!panel)return;

  let maps=[];
  let overlays=[[],[]];
  let routesByDate={};
  try{routesByDate=JSON.parse(document.querySelector('#viewer-flight-paths-by-date')?.textContent||'{}')}catch{}
  const colors=['#e17749','#397d59','#4c83c8','#8b66a5','#d1a22e'];
  const pointPairs=value=>Array.isArray(value)?value.filter(p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite)):[];
  const selectedDates=()=>[
    document.querySelector('#compare-date-left')?.value,
    document.querySelector('#compare-date-right')?.value,
  ];

  function renderRoutes(map,date,index){
    const bounds=new google.maps.LatLngBounds();
    let hasBounds=false;
    let boundary=[];
    try{boundary=pointPairs(JSON.parse(root.dataset.boundary||'[]'))}catch{}
    if(boundary.length>=3){
      const polygon=new google.maps.Polygon({map,paths:boundary.map(([lat,lng])=>({lat,lng})),strokeColor:'#d5683b',strokeWeight:3,fillColor:'#e98a55',fillOpacity:.12,clickable:false});
      overlays[index].push(polygon);
      boundary.forEach(([lat,lng])=>bounds.extend({lat,lng}));
      hasBounds=true;
    }
    const paths=routesByDate[date]||[];
    paths.forEach((route,n)=>{
      const points=pointPairs(route.points);
      if(points.length<2)return;
      const line=new google.maps.Polyline({map,path:points.map(([lat,lng])=>({lat,lng})),strokeColor:colors[(n+index)%colors.length],strokeOpacity:.92,strokeWeight:4});
      overlays[index].push(line);
      line.addListener('click',()=>{
        if(!route.flight_id)return;
        const url=new URL(location.href);
        url.searchParams.set('tab','map');url.searchParams.set('date',date);url.searchParams.set('flight',route.flight_id);
        location.assign(url.toString());
      });
      points.forEach(([lat,lng])=>bounds.extend({lat,lng}));
      hasBounds=true;
    });
    if(hasBounds)map.fitBounds(bounds,24);
    else {map.setCenter({lat:Number(root.dataset.centerLat)||39.8283,lng:Number(root.dataset.centerLng)||-98.5795});map.setZoom(Number(root.dataset.zoom)||4)}
  }

  function initialize(){
    if(!window.google?.maps)return;
    maps=[0,1].map(index=>new google.maps.Map(document.querySelector(index?'#viewer-compare-map-right':'#viewer-compare-map-left'),{
      center:{lat:Number(root.dataset.centerLat)||39.8283,lng:Number(root.dataset.centerLng)||-98.5795},
      zoom:Number(root.dataset.zoom)||4,mapTypeId:window.projectViewerMap?.getMapTypeId?.()||'roadmap',
      mapTypeControl:false,streetViewControl:false,fullscreenControl:true,
    }));
    update();
  }
  function update(){
    if(!maps.length)return;
    const dates=selectedDates();
    dates.forEach((date,index)=>{
      overlays[index].forEach(overlay=>overlay.setMap(null));overlays[index]=[];
      renderRoutes(maps[index],date,index);
    });
  }
  toggle.addEventListener('click',()=>{
    const opening=panel.hidden;
    panel.hidden=!opening;
    document.querySelector('#viewer-live-map').hidden=opening;
    toggle.setAttribute('aria-expanded',String(opening));
    toggle.textContent=opening?'Hide comparison':'Compare dates';
    if(opening){if(!maps.length)initialize();else update();setTimeout(()=>maps.forEach(map=>google.maps.event.trigger(map,'resize')),80)}
  });
  document.querySelector('#compare-close')?.addEventListener('click',()=>toggle.click());
  ['#compare-date-left','#compare-date-right'].forEach(selector=>document.querySelector(selector)?.addEventListener('change',update));
  window.initProjectViewerCompareMaps=()=>{if(!panel.hidden&&window.google?.maps&&!maps.length)initialize()};
})();
