/* Capture IV 3D scan viewer: streams a converted point cloud (CVPC) and renders it with three.js.
 * Usage: <div data-scan-viewer data-src="..." data-units="ft"></div> (auto-mounted), or
 * window.CaptureScanViewer.mount(element, {src, units, paths}) which returns {dispose()}.
 * paths: [{label, color, points: [[x, y], ...]}] in the scan's local coordinates (relative to the
 * cloud's minimum corner); they are drawn as flight lines above the scan. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const RECORD = 10;
const CLASS_COLORS = {
  0: [180, 180, 180], 1: [200, 200, 200], 2: [166, 120, 76], 3: [144, 238, 144], 4: [80, 170, 80], 5: [34, 120, 50],
  6: [235, 110, 90], 7: [255, 0, 0], 9: [70, 130, 255], 10: [130, 130, 130], 11: [100, 100, 100], 13: [255, 210, 0],
  14: [255, 160, 0], 15: [255, 220, 120], 16: [255, 255, 0], 17: [80, 80, 80], 18: [255, 60, 60],
};
const fmt = n => n.toLocaleString(undefined, { maximumFractionDigits: 0 });

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value; else if (key === 'text') node.textContent = value; else node.setAttribute(key, value);
  }
  children.forEach(child => node.appendChild(child));
  return node;
}

function elevationColor(t) {
  // blue -> cyan -> green -> yellow -> red
  const stops = [[0, [40, 70, 200]], [0.25, [40, 180, 230]], [0.5, [60, 190, 90]], [0.75, [245, 210, 60]], [1, [225, 60, 50]]];
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i], k = (t - a) / (b - a || 1);
      return [ca[0] + (cb[0] - ca[0]) * k, ca[1] + (cb[1] - ca[1]) * k, ca[2] + (cb[2] - ca[2]) * k];
    }
  }
  return stops[stops.length - 1][1];
}

function mount(root, { src, units = 'm', paths = [] } = {}) {
  root.classList.add('sv');
  root.textContent = '';
  const canvasHost = el('div', { class: 'sv-canvas' });
  const status = el('div', { class: 'sv-status', role: 'status' }, [el('span', { class: 'sv-status-text', text: 'Loading scan…' }), el('i', { class: 'sv-bar' }, [el('b')])]);
  const readout = el('div', { class: 'sv-readout', hidden: '' });
  const colorSelect = el('select', { 'aria-label': 'Color points by' }, [
    el('option', { value: 'photo', text: 'Photo color' }), el('option', { value: 'elevation', text: 'Elevation' }), el('option', { value: 'class', text: 'Classification' })]);
  const sizeInput = el('input', { type: 'range', min: '1', max: '6', step: '0.5', value: '2', 'aria-label': 'Point size' });
  const btn = (label, title) => el('button', { type: 'button', class: 'sv-btn', title, text: label });
  const resetBtn = btn('Reset', 'Reset view'), topBtn = btn('Top', 'View from above'), measureBtn = btn('Measure', 'Measure between two points');
  const fullBtn = btn('⛶', 'Full screen');
  const validPaths = (Array.isArray(paths) ? paths : []).filter(p => Array.isArray(p.points) && p.points.length > 1);
  const pathsBtn = validPaths.length ? btn('Flight paths', 'Show or hide this date\'s flight paths') : null;
  if (pathsBtn) pathsBtn.classList.add('is-on');
  const toolbar = el('div', { class: 'sv-toolbar' }, [resetBtn, topBtn, measureBtn, ...(pathsBtn ? [pathsBtn] : []),
    el('label', { class: 'sv-field' }, [el('span', { text: 'Color' }), colorSelect]),
    el('label', { class: 'sv-field' }, [el('span', { text: 'Size' }), sizeInput]), fullBtn]);
  const info = el('div', { class: 'sv-info' });
  const legend = validPaths.length ? el('ul', { class: 'sv-legend', 'aria-label': 'Flight paths' }, validPaths.map(p =>
    el('li', {}, [el('i', { style: `background:${/^#[0-9a-f]{3,8}$/i.test(p.color || '') ? p.color : '#e17749'}` }), el('span', { text: String(p.label || 'Route') })]))) : null;
  root.append(canvasHost, toolbar, status, readout, info, ...(legend ? [legend] : []));

  const renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x0b1426, 1);
  canvasHost.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 1e7);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.screenSpacePanning = true;
  const material = new THREE.PointsMaterial({ size: 2, sizeAttenuation: false, vertexColors: true });
  const geometry = new THREE.BufferGeometry();
  const points = new THREE.Points(geometry, material);
  scene.add(points);
  const markers = new THREE.Group();
  scene.add(markers);
  const routes = new THREE.Group();
  scene.add(routes);

  let header = null, positions, photo, colors, classes, loaded = 0, span = [1, 1, 1], dirtyFrom = 0, mode = 'photo';
  let disposed = false, frame = 0, needsRender = true, measuring = false, picks = [], zRange = null;
  const abort = new AbortController();

  const resize = () => {
    const w = canvasHost.clientWidth || 640, h = canvasHost.clientHeight || 400;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    needsRender = true;
  };
  const observer = new ResizeObserver(resize);
  observer.observe(canvasHost);

  const homeView = (top = false) => {
    const size = Math.max(span[0], span[1], span[2] + (validPaths.length ? Math.max(span[0], span[1]) * 0.04 : 0));
    controls.target.set(0, 0, span[2] * 0.15);
    if (top) camera.position.set(0, -size * 0.001, size * 1.25);
    else camera.position.set(size * 0.38, -size * 0.52, size * 0.36);
    camera.near = Math.max(size / 20000, 0.01);
    camera.far = size * 20;
    camera.updateProjectionMatrix();
    controls.update();
    needsRender = true;
  };

  // Elevation colors span the 2nd-98th percentile so a few tall poles or stray returns
  // don't wash the whole site into one color.
  const computeZRange = () => {
    const n = Math.min(loaded, 40000), step = Math.max(1, Math.floor(loaded / n)), zs = [];
    for (let i = 0; i < loaded; i += step) zs.push(positions[i * 3 + 2]);
    zs.sort((a, b) => a - b);
    const lo = zs[Math.floor(zs.length * 0.02)] || 0, hi = zs[Math.floor(zs.length * 0.98)] || span[2] || 1;
    zRange = [lo, Math.max(hi, lo + 1e-6)];
  };
  const recolor = (from, to) => {
    if (!header) return;
    if (mode === 'elevation' && (!zRange || (loaded > 20000 && from === 0))) computeZRange();
    for (let i = from; i < to; i++) {
      let c;
      if (mode === 'photo') { colors[i * 3] = photo[i * 3]; colors[i * 3 + 1] = photo[i * 3 + 1]; colors[i * 3 + 2] = photo[i * 3 + 2]; continue; }
      if (mode === 'elevation') c = elevationColor(Math.min(1, Math.max(0, (positions[i * 3 + 2] - zRange[0]) / (zRange[1] - zRange[0]))));
      else c = CLASS_COLORS[classes[i]] || [190, 190, 190];
      colors[i * 3] = c[0]; colors[i * 3 + 1] = c[1]; colors[i * 3 + 2] = c[2];
    }
  };

  const flush = () => {
    if (!header || dirtyFrom >= loaded) return;
    const pos = geometry.getAttribute('position'), col = geometry.getAttribute('color');
    pos.addUpdateRange(dirtyFrom * 3, (loaded - dirtyFrom) * 3); pos.needsUpdate = true;
    col.addUpdateRange(dirtyFrom * 3, (loaded - dirtyFrom) * 3); col.needsUpdate = true;
    geometry.setDrawRange(0, loaded);
    dirtyFrom = loaded;
    needsRender = true;
  };

  const animate = () => {
    if (disposed) return;
    frame = requestAnimationFrame(animate);
    flush();
    if (controls.update() || needsRender) { renderer.render(scene, camera); needsRender = false; }
  };
  controls.addEventListener('change', () => { needsRender = true; });

  // Flight paths fly above the scan: each line sits a little over the highest point of the cloud,
  // with a dot at every waypoint and a drop line from the start to the ground.
  function drawRoutes() {
    routes.clear();
    if (!validPaths.length) return;
    const lift = span[2] + Math.max(span[0], span[1]) * 0.04, radius = Math.max(span[0], span[1]) / 450;
    validPaths.forEach(path => {
      const color = new THREE.Color(/^#[0-9a-f]{3,8}$/i.test(path.color || '') ? path.color : '#e17749');
      const pts = path.points.map(([x, y]) => new THREE.Vector3(x - span[0] / 2, y - span[1] / 2, lift));
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color, depthTest: false }));
      line.renderOrder = 3; routes.add(line);
      const dotGeometry = new THREE.SphereGeometry(radius, 10, 10), dotMaterial = new THREE.MeshBasicMaterial({ color, depthTest: false });
      pts.forEach((p, i) => { const dot = new THREE.Mesh(dotGeometry, dotMaterial); dot.position.copy(p); if (i === 0) dot.scale.setScalar(1.8); dot.renderOrder = 3; routes.add(dot); });
      const drop = new THREE.Line(new THREE.BufferGeometry().setFromPoints([pts[0], new THREE.Vector3(pts[0].x, pts[0].y, 0)]),
        new THREE.LineDashedMaterial({ color, dashSize: radius * 3, gapSize: radius * 2, depthTest: false }));
      drop.computeLineDistances(); drop.renderOrder = 3; routes.add(drop);
    });
    needsRender = true;
  }
  pathsBtn?.addEventListener('click', () => {
    routes.visible = !routes.visible;
    pathsBtn.classList.toggle('is-on', routes.visible);
    if (legend) legend.hidden = !routes.visible;
    needsRender = true;
  });

  const setStatus = (text, progress) => {
    status.hidden = false;
    status.querySelector('.sv-status-text').textContent = text;
    status.querySelector('b').style.width = `${Math.round((progress || 0) * 100)}%`;
  };

  async function load() {
    resize();
    animate();
    let response;
    try {
      response = await fetch(src, { credentials: 'same-origin', signal: abort.signal });
    } catch (error) { if (!disposed) setStatus('The scan could not be loaded. Check your connection and reload.', 0); return; }
    if (!response.ok || !response.body) { setStatus('This scan is not available right now.', 0); return; }
    const reader = response.body.getReader();
    let pending = new Uint8Array(0);
    while (true) {
      let chunk;
      try { chunk = await reader.read(); }
      catch (error) {
        if (!disposed) setStatus('The scan stopped loading. Check your connection and reload.', header ? loaded / header.count : 0);
        return;
      }
      const { done, value } = chunk;
      if (done || disposed) break;
      const merged = new Uint8Array(pending.length + value.length);
      merged.set(pending); merged.set(value, pending.length);
      pending = merged;
      if (!header) {
        if (pending.length < 12) continue;
        const view = new DataView(pending.buffer, pending.byteOffset, pending.byteLength);
        const magic = String.fromCharCode(...pending.subarray(0, 4));
        if (magic !== 'CVPC') { setStatus('This file is not a converted scan.', 0); return; }
        const length = view.getUint32(8, true);
        if (pending.length < 12 + length) continue;
        header = JSON.parse(new TextDecoder().decode(pending.subarray(12, 12 + length)));
        pending = pending.slice(12 + length);
        span = header.max;
        positions = new Float32Array(header.count * 3);
        photo = new Uint8Array(header.count * 3);
        colors = new Uint8Array(header.count * 3);
        classes = new Uint8Array(header.count);
        geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage));
        geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3, true).setUsage(THREE.DynamicDrawUsage));
        geometry.setDrawRange(0, 0);
        if (!header.has_rgb) { mode = 'elevation'; colorSelect.value = 'elevation'; }
        units = header.units || units;
        // Bounds come from the header, so they needn't be recomputed as points stream in.
        geometry.boundingBox = new THREE.Box3(new THREE.Vector3(-span[0] / 2, -span[1] / 2, 0), new THREE.Vector3(span[0] / 2, span[1] / 2, span[2]));
        geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, 0, span[2] / 2), Math.hypot(span[0], span[1], span[2]) / 2);
        drawRoutes();
        homeView();
      }
      const whole = Math.min(Math.floor(pending.length / RECORD), header.count - loaded);
      if (whole > 0) {
        const view = new DataView(pending.buffer, pending.byteOffset, whole * RECORD);
        const sx = span[0] / 65535, sy = span[1] / 65535, sz = span[2] / 65535, hx = span[0] / 2, hy = span[1] / 2;
        for (let k = 0; k < whole; k++) {
          const o = k * RECORD, i = loaded + k;
          positions[i * 3] = view.getUint16(o, true) * sx - hx;
          positions[i * 3 + 1] = view.getUint16(o + 2, true) * sy - hy;
          positions[i * 3 + 2] = view.getUint16(o + 4, true) * sz;
          photo[i * 3] = view.getUint8(o + 6); photo[i * 3 + 1] = view.getUint8(o + 7); photo[i * 3 + 2] = view.getUint8(o + 8);
          classes[i] = view.getUint8(o + 9);
        }
        const before = loaded;
        loaded += whole;
        if (mode === 'elevation' && !zRange && loaded > 5000) computeZRange();
        recolor(before, loaded);
        pending = pending.slice(whole * RECORD);
        setStatus(`Loading ${fmt(loaded)} of ${fmt(header.count)} points…`, loaded / header.count);
      }
    }
    if (!header) { setStatus('This scan could not be read.', 0); return; }
    status.hidden = true;
    info.textContent = `${fmt(loaded)} points shown${header.source_points > loaded ? ` of ${fmt(header.source_points)} scanned` : ''} · units: ${units === 'ft' ? 'feet' : 'metres'}`;
  }

  colorSelect.addEventListener('change', () => { mode = colorSelect.value; recolor(0, loaded); dirtyFrom = 0; });
  sizeInput.addEventListener('input', () => { material.size = Number(sizeInput.value); needsRender = true; });
  resetBtn.addEventListener('click', () => homeView(false));
  topBtn.addEventListener('click', () => homeView(true));
  fullBtn.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen?.(); else root.requestFullscreen?.();
  });

  const clearMeasure = () => { picks = []; markers.clear(); readout.hidden = true; needsRender = true; };
  measureBtn.addEventListener('click', () => {
    measuring = !measuring;
    measureBtn.classList.toggle('is-on', measuring);
    root.classList.toggle('is-measuring', measuring);
    clearMeasure();
    if (measuring) { readout.hidden = false; readout.textContent = 'Click the first point'; }
  });
  const raycaster = new THREE.Raycaster();
  let down = null;
  renderer.domElement.addEventListener('pointerdown', event => { down = [event.clientX, event.clientY]; });
  renderer.domElement.addEventListener('pointerup', event => {
    if (!measuring || !down || Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 5 || !loaded) return;
    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    raycaster.params.Points.threshold = Math.max(span[0], span[1]) / 1500 * Math.max(1, camera.position.distanceTo(controls.target) / Math.max(span[0], span[1]));
    const hit = raycaster.intersectObject(points, false)[0];
    if (!hit) { readout.textContent = 'No point there. Try clicking on the scan.'; return; }
    if (picks.length === 2) clearMeasure();
    const p = new THREE.Vector3(positions[hit.index * 3], positions[hit.index * 3 + 1], positions[hit.index * 3 + 2]);
    picks.push(p);
    const dot = new THREE.Mesh(new THREE.SphereGeometry(Math.max(span[0], span[1]) / 400, 12, 12), new THREE.MeshBasicMaterial({ color: 0xffd34d, depthTest: false }));
    dot.position.copy(p); dot.renderOrder = 2; markers.add(dot);
    readout.hidden = false;
    if (picks.length === 1) readout.textContent = 'Click the second point';
    else {
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(picks), new THREE.LineBasicMaterial({ color: 0xffd34d, depthTest: false }));
      line.renderOrder = 2; markers.add(line);
      const d = picks[0].distanceTo(picks[1]), dz = Math.abs(picks[1].z - picks[0].z), dh = Math.hypot(picks[1].x - picks[0].x, picks[1].y - picks[0].y);
      const u = units === 'ft' ? 'ft' : 'm';
      readout.textContent = `Distance ${d.toFixed(2)} ${u} · horizontal ${dh.toFixed(2)} ${u} · height ${dz.toFixed(2)} ${u}`;
    }
    needsRender = true;
  });

  load();
  return {
    dispose() {
      disposed = true; abort.abort(); cancelAnimationFrame(frame); observer.disconnect();
      controls.dispose(); geometry.dispose(); material.dispose(); renderer.dispose();
      routes.traverse(node => { node.geometry?.dispose?.(); node.material?.dispose?.(); });
      root.textContent = '';
    },
  };
}

window.CaptureScanViewer = { mount };
document.querySelectorAll('[data-scan-viewer]').forEach(node => {
  let options = { src: node.dataset.src, units: node.dataset.units };
  const config = node.dataset.config && document.getElementById(node.dataset.config);
  if (config) { try { options = { ...options, ...JSON.parse(config.textContent) }; } catch { /* keep defaults */ } }
  mount(node, options);
});
window.dispatchEvent(new Event('capture-scan-viewer-ready'));
