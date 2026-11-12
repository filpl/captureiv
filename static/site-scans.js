/* Site capture: laser-scan background under the date's flight paths (Leaflet or Google map),
 * scan uploads (direct-to-cloud when available) and status refresh while a scan is prepared. */
(() => {
  const box = document.getElementById('site-scans');
  if (!box) return;
  let scans = [];
  try { scans = JSON.parse(document.getElementById('site-scan-data')?.textContent || '[]'); } catch { scans = []; }
  const byId = new Map(scans.map(scan => [String(scan.id), scan]));
  const overlays = new Map();

  // ---------------------------------------------------------------- map layer
  const whenMap = callback => {
    let tries = 0;
    const wait = () => {
      const map = window.siteCaptureMap;
      if (map) callback(map); else if (tries++ < 200) setTimeout(wait, 150);
    };
    wait();
  };
  const isLeaflet = map => !!(window.L && map instanceof window.L.Map);
  const addOverlay = (map, scan, opacity) => {
    const [[south, west], [north, east]] = scan.bounds;
    if (isLeaflet(map)) {
      if (!map.getPane('scanPane')) { map.createPane('scanPane'); map.getPane('scanPane').style.zIndex = 320; map.getPane('scanPane').style.pointerEvents = 'none'; }
      return { kind: 'leaflet', layer: window.L.imageOverlay(scan.overlay, [[south, west], [north, east]], { pane: 'scanPane', opacity, interactive: false }).addTo(map) };
    }
    if (window.google?.maps?.GroundOverlay) {
      const layer = new google.maps.GroundOverlay(scan.overlay, { south, west, north, east }, { opacity, clickable: false });
      layer.setMap(map);
      return { kind: 'google', layer };
    }
    return null;
  };
  const setVisible = (map, entry, on) => {
    if (!entry) return;
    if (entry.kind === 'leaflet') { if (on) entry.layer.addTo(map); else entry.layer.remove(); }
    else entry.layer.setMap(on ? map : null);
  };
  const setOpacity = (entry, value) => { if (entry) entry.layer.setOpacity(value); };
  const zoomTo = (map, scan) => {
    const [[south, west], [north, east]] = scan.bounds;
    if (isLeaflet(map)) map.fitBounds([[south, west], [north, east]], { padding: [24, 24], maxZoom: 20 });
    else map.fitBounds({ south, west, north, east });
  };

  whenMap(map => {
    box.querySelectorAll('.scan-item').forEach(item => {
      const scan = byId.get(item.dataset.scanId);
      if (!scan || !scan.bounds || !scan.overlay) return;
      const entry = addOverlay(map, scan, 1);
      overlays.set(item.dataset.scanId, entry);
      item.querySelector('[data-scan-show]')?.addEventListener('change', event => setVisible(map, entry, event.target.checked));
      item.querySelector('[data-scan-opacity]')?.addEventListener('input', event => setOpacity(entry, Number(event.target.value) / 100));
      item.querySelector('[data-scan-zoom]')?.addEventListener('click', () => zoomTo(map, scan));
    });
  });

  box.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));

  // ------------------------------------------------- refresh while preparing
  const pending = scans.filter(scan => scan.status === 'processing');
  if (pending.length) {
    let rounds = 0;
    const poll = async () => {
      rounds += 1;
      for (const scan of pending) {
        try {
          const response = await fetch(`/admin/projects/scans/${scan.id}/status`, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
          const data = await response.json();
          if (data.status && data.status !== 'processing') { location.reload(); return; }
        } catch { /* try again next round */ }
      }
      setTimeout(poll, Math.min(15000, 3000 + rounds * 1000));
    };
    setTimeout(poll, 3000);
  }

  // ------------------------------------------------------------------ upload
  const form = document.getElementById('scan-upload-form');
  if (!form) return;
  const fileInput = form.querySelector('input[name=file]');
  const progress = form.querySelector('.scan-progress'), bar = progress.querySelector('span'), pct = progress.querySelector('b');
  const errorBox = form.querySelector('.scan-upload-error'), button = form.querySelector('button[type=submit]');
  const maxBytes = Number(form.dataset.maxBytes) || 0;
  const gb = bytes => (bytes / 1073741824).toFixed(bytes >= 10737418240 ? 0 : 1);
  const showError = message => { errorBox.textContent = message; errorBox.hidden = false; button.disabled = false; progress.hidden = true; };
  const setProgress = (done, total) => { const p = total ? Math.round(done / total * 100) : 0; bar.style.width = `${p}%`; pct.textContent = `${p}%`; };
  const extOk = file => /\.(las|laz|e57)$/i.test(file?.name || '');
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file && !extOk(file)) showError('Choose a LAS, LAZ or E57 scan.');
    else if (file && maxBytes && file.size > maxBytes) showError(`This file is ${gb(file.size)} GB. The limit is ${gb(maxBytes)} GB.`);
    else errorBox.hidden = true;
  });
  form.addEventListener('submit', async event => {
    const file = fileInput.files[0];
    if (!file || !window.FormData || !window.XMLHttpRequest) return;
    event.preventDefault();
    if (!extOk(file)) { showError('Choose a LAS, LAZ or E57 scan.'); return; }
    if (maxBytes && file.size > maxBytes) { showError(`This file is ${gb(file.size)} GB. The limit is ${gb(maxBytes)} GB.`); return; }
    errorBox.hidden = true; progress.hidden = false; button.disabled = true; setProgress(0, 1);
    const value = name => (form.querySelector(`[name=${name}]`) || {}).value || '';
    const done = result => {
      const target = new URL((result && result.redirect) || location.href, location.href);
      if (target.pathname + target.search === location.pathname + location.search) location.reload();
      else location.href = target.toString();
    };
    if (document.body.dataset.r2Multipart === 'true' && window.uploadR2Multipart) {
      try {
        const result = await window.uploadR2Multipart(file, { project: value('project'), date: value('date'), name: value('name') },
          { csrf: box.dataset.csrf, baseUrl: form.dataset.r2Base, onProgress: setProgress });
        done(result);
      } catch (error) { showError(error.message || 'Upload failed. Press Upload scan again to resume.'); }
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', form.action);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.upload.onprogress = e => { if (e.lengthComputable) setProgress(e.loaded, e.total); };
    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && data.redirect) { done(data); return; }
      showError(data.error || 'Upload failed. Please try again.');
    };
    xhr.onerror = () => showError('The connection dropped. Please try again.');
    xhr.send(new FormData(form));
  });
})();
