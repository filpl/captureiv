// Capture IV site walkthrough viewer.
// 2D drawings (PDF or image) with capture routes, a floating 360°/photo/video
// window, a capture-date timeline with compare, and a 3D (GLB) model mode.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const cfg = JSON.parse(document.getElementById('wt-config').textContent);
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids) if (kid != null) node.append(kid);
  return node;
};
const SVG = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs = {}) => { const n = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
const fmtDate = (iso) => new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const KIND_LABEL = { pano: '360°', photo: 'Photo', video: 'Video' };

const state = {
  data: null, sheetId: null, date: null, mode: '2d', edit: false,
  adding: false, placing3d: false, repositionId: null,
  editSessionId: null, hiddenSessions: new Set(), hiddenModels: new Set(),
  activePointId: null, mediaIndex: 0, compare: false,
  showPaths: true, showMarkers: true, markerSize: 11, pathWidth: 4, syncCompare: true,
  view: { s: 1, x: 0, y: 0, r: 0 }, planW: 0, planH: 0,
};

// ---------------------------------------------------------------- helpers
function toast(message, isError = false) {
  const t = $('wt-toast');
  t.textContent = message; t.classList.toggle('err', isError); t.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove('show'), 3200);
}

async function api(path, body, isForm = false) {
  const headers = { 'X-CSRF-Token': window.WT_CSRF };
  if (!isForm) headers['Content-Type'] = 'application/json';
  const response = await fetch(cfg.apiBase + path, { method: 'POST', headers, credentials: 'same-origin',
    body: isForm ? body : JSON.stringify(body || {}) });
  let payload = {};
  try { payload = await response.json(); } catch { /* non-JSON error page */ }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

const sheets = () => state.data?.sheets || [];
const currentSheet = () => sheets().find((s) => s.id === state.sheetId) || null;
const allSessions = () => state.data?.sessions || [];
const sheetSessions = () => allSessions().filter((s) => s.sheet_id === state.sheetId);
const visibleSessions = () => sheetSessions().filter((s) => !state.hiddenSessions.has(s.id) && (!state.date || s.date === state.date));
function findPoint(pointId) {
  for (const session of allSessions()) {
    const point = session.points.find((p) => p.id === pointId);
    if (point) return { session, point };
  }
  return null;
}

// ------------------------------------------------------------------- data
async function load({ keepPoint = true } = {}) {
  const response = await fetch(cfg.dataUrl, { credentials: 'same-origin' });
  if (!response.ok) { toast('Could not load this project.', true); return; }
  state.data = await response.json();
  if (!sheets().some((s) => s.id === state.sheetId)) {
    const withSessions = sheets().find((s) => allSessions().some((x) => x.sheet_id === s.id));
    state.sheetId = (withSessions || sheets()[0] || {}).id ?? null;
    state.date = null; await chooseLatestDate();
  }
  if (!sheetSessions().some((s) => s.id === state.editSessionId)) {
    const latest = sheetSessions().slice(-1)[0];
    state.editSessionId = latest ? latest.id : null;
  }
  renderSidebar(); renderDates(); await renderPlan(); render3DMarkers(); updateEditStatus();
  if (keepPoint && state.activePointId && findPoint(state.activePointId)) openPoint(state.activePointId, { keepMedia: true });
  else if (state.activePointId) closeWindow();
}

async function chooseLatestDate() {
  const dates = [...new Set(sheetSessions().map((s) => s.date))].sort();
  state.date = dates.length ? dates[dates.length - 1] : null;
}

// ---------------------------------------------------------------- sidebar
function renderSidebar() {
  const sheetBox = $('wt-sheets'); sheetBox.replaceChildren();
  if (!sheets().length) sheetBox.append(el('p', { class: 'wt-floor', text: cfg.canEdit ? 'No drawings yet. Turn on Edit and upload one.' : 'No drawings yet.' }));
  let lastFloor = null;
  for (const sheet of sheets()) {
    if (sheet.floor !== lastFloor) { sheetBox.append(el('div', { class: 'wt-floor', text: sheet.floor || 'Unassigned floor' })); lastFloor = sheet.floor; }
    const count = allSessions().filter((s) => s.sheet_id === sheet.id).length;
    const item = el('button', { class: 'wt-item' + (sheet.id === state.sheetId ? ' is-active' : ''), type: 'button',
      onclick: async () => { state.sheetId = sheet.id; state.hiddenSessions.clear(); await chooseLatestDate();
        const latest = sheetSessions().slice(-1)[0]; state.editSessionId = latest ? latest.id : null;
        closeWindow(); renderSidebar(); renderDates(); await renderPlan(); render3DMarkers(); } },
      el('span', { text: '▦' }), el('span', { text: sheet.name }), el('small', { text: `${count} walk${count === 1 ? '' : 's'}` }));
    if (state.edit && cfg.canDelete) item.append(el('span', { class: 'wt-del', role: 'button', title: 'Delete drawing', text: '✕',
      onclick: (e) => { e.stopPropagation(); remove(`/sheets/${sheet.id}/delete`, `Delete drawing "${sheet.name}"?`); } }));
    sheetBox.append(item);
  }

  const sessionBox = $('wt-sessions'); sessionBox.replaceChildren();
  const list = sheetSessions();
  if (!list.length) sessionBox.append(el('p', { class: 'wt-floor', text: currentSheet() ? 'No capture sessions on this drawing.' : '—' }));
  for (const s of [...list].reverse()) {
    const checkbox = el('input', { type: 'checkbox', 'aria-label': `Show ${s.name}`, onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? state.hiddenSessions.delete(s.id) : state.hiddenSessions.add(s.id); drawOverlay(); render3DMarkers(); } });
    checkbox.checked = !state.hiddenSessions.has(s.id);
    const item = el('div', { class: 'wt-item' + (s.id === state.editSessionId ? ' is-active' : ''), role: 'button', tabindex: '0',
      title: 'Select this session', onclick: () => { state.editSessionId = s.id; if (state.date && state.date !== s.date) { state.date = s.date; renderDates(); }
        renderSidebar(); drawOverlay(); updateEditStatus(); } },
      checkbox, el('span', { class: 'wt-swatch', style: `background:${s.color}` }),
      el('span', { text: s.name }), el('small', { text: `${fmtDate(s.date)} · ${s.points.length} pts` }));
    if (state.edit && cfg.canDelete) item.append(el('span', { class: 'wt-del', role: 'button', title: 'Delete session', text: '✕',
      onclick: (e) => { e.stopPropagation(); remove(`/sessions/${s.id}/delete`, `Delete session "${s.name}", its ${s.points.length} points and their media?`); } }));
    sessionBox.append(item);
  }

  const layers = $('wt-model-layers'); layers.replaceChildren();
  for (const m of state.data?.models || []) {
    const box = el('input', { type: 'checkbox', onchange: (e) => { e.target.checked ? state.hiddenModels.delete(m.id) : state.hiddenModels.add(m.id); three?.applyVisibility(); } });
    box.checked = !state.hiddenModels.has(m.id);
    const row = el('label', { class: 'wt-check' }, box, el('span', { text: `3D: ${m.name}${m.floor ? ' · ' + m.floor : ''}` }));
    if (state.edit && cfg.canDelete) row.append(el('span', { class: 'wt-del', role: 'button', title: 'Delete model', text: '✕',
      onclick: (e) => { e.preventDefault(); remove(`/models/${m.id}/delete`, `Delete 3D model "${m.name}"? Points keep their 2D positions.`); } }));
    layers.append(row);
  }
}

async function remove(path, question) {
  if (!confirm(question)) return;
  try { await api(path); toast('Deleted.'); await load(); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------- dates / timeline
function renderDates() {
  const dates = [...new Set(sheetSessions().map((s) => s.date))].sort();
  const select = $('wt-date'); select.replaceChildren(el('option', { value: '', text: 'All dates' }));
  for (const d of dates) select.append(el('option', { value: d, text: fmtDate(d) }));
  select.value = state.date || '';
  const tl = $('wt-timeline'); tl.replaceChildren();
  if (!dates.length) return;
  tl.append(el('span', { class: 'wt-tl-label', text: 'Timeline' }));
  tl.append(el('button', { class: 'wt-chip' + (!state.date ? ' is-active' : ''), type: 'button', text: 'All', onclick: () => setDate(null) }));
  for (const d of dates) tl.append(el('button', { class: 'wt-chip' + (state.date === d ? ' is-active' : ''), type: 'button', text: fmtDate(d), onclick: () => setDate(d) }));
}
function setDate(date) {
  state.date = date || null;
  const inDate = sheetSessions().filter((s) => !state.date || s.date === state.date);
  if (inDate.length && !inDate.some((s) => s.id === state.editSessionId)) state.editSessionId = inDate[inDate.length - 1].id;
  renderDates(); renderSidebar(); drawOverlay(); render3DMarkers();
  const active = state.activePointId && findPoint(state.activePointId);
  if (active && state.date && active.session.date !== state.date) closeWindow();
}
$('wt-date').addEventListener('change', (e) => setDate(e.target.value));

// --------------------------------------------------------------- 2D plan
const plan = $('wt-plan'), inner = $('wt-plan-inner'), canvas = $('wt-plan-canvas'), overlay = $('wt-overlay');
let pdfjsPromise = null;
let renderToken = 0;
const sheetCache = new Map();

async function rasterizeSheet(sheet) {
  if (sheetCache.has(sheet.id)) return sheetCache.get(sheet.id);
  let bitmap;
  if (sheet.kind === 'pdf') {
    pdfjsPromise ||= import('pdfjs').then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
      return lib;
    });
    const pdfjs = await pdfjsPromise;
    const doc = await pdfjs.getDocument({ url: sheet.url, withCredentials: true, isEvalSupported: false }).promise;
    const page = await doc.getPage(Math.min(sheet.page || 1, doc.numPages));
    const base = page.getViewport({ scale: 1 });
    // Render crisp enough to zoom in, within a safe canvas size.
    const scale = Math.min(4000 / Math.max(base.width, base.height), Math.sqrt(24e6 / (base.width * base.height)));
    const viewport = page.getViewport({ scale });
    const c = document.createElement('canvas'); c.width = Math.floor(viewport.width); c.height = Math.floor(viewport.height);
    await page.render({ canvasContext: c.getContext('2d'), viewport }).promise;
    bitmap = c; doc.destroy();
  } else {
    bitmap = await new Promise((resolve, reject) => { const img = new Image(); img.onload = () => resolve(img); img.onerror = reject; img.src = sheet.url; });
  }
  sheetCache.set(sheet.id, bitmap);
  return bitmap;
}

async function renderPlan() {
  const sheet = currentSheet();
  const empty = $('wt-empty');
  if (!sheet) {
    plan.hidden = true; empty.hidden = state.mode !== '2d';
    empty.replaceChildren(el('div', {}, el('strong', { text: 'No floor plans yet' }),
      el('span', { text: cfg.canEdit ? 'Turn on Edit, then upload a drawing under “Set up”.' : 'This project does not have any drawings yet.' })));
    return;
  }
  empty.hidden = true; plan.hidden = state.mode !== '2d';
  if (plan.dataset.sheet === String(sheet.id)) { drawOverlay(); return; }
  const token = ++renderToken;
  try {
    const bitmap = await rasterizeSheet(sheet);
    if (token !== renderToken) return;
    const w = bitmap.naturalWidth || bitmap.width, h = bitmap.naturalHeight || bitmap.height;
    const scaleDown = Math.min(1, 6000 / Math.max(w, h));
    canvas.width = Math.round(w * scaleDown); canvas.height = Math.round(h * scaleDown);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    state.planW = canvas.width; state.planH = canvas.height;
    inner.style.width = state.planW + 'px'; inner.style.height = state.planH + 'px';
    overlay.setAttribute('viewBox', `0 0 ${state.planW} ${state.planH}`);
    plan.dataset.sheet = String(sheet.id);
    state.view.r = 0; state.touched = false; fitView(); drawOverlay();
  } catch (err) {
    console.error(err); toast('This drawing could not be displayed.', true);
  }
}

function matrix(v = state.view) {
  return new DOMMatrix().translate(v.x, v.y).scale(v.s).translate(state.planW / 2, state.planH / 2).rotate(v.r).translate(-state.planW / 2, -state.planH / 2);
}
function touch() { state.touched = true; }
function applyView() { inner.style.transform = matrix().toString(); sizeMarkers(); }
function fitView() {
  const rect = plan.getBoundingClientRect();
  const rotated = state.view.r % 180 !== 0;
  const bw = rotated ? state.planH : state.planW, bh = rotated ? state.planW : state.planH;
  if (!bw || !bh || !rect.width) return;
  const pad = 120;
  const s = Math.min((rect.width - 40) / bw, (rect.height - pad) / bh);
  state.view.s = s; state.view.x = (rect.width - state.planW * s) / 2; state.view.y = (rect.height - pad + 40 - state.planH * s) / 2;
  applyView();
}
function screenToPlan(clientX, clientY) {
  const rect = plan.getBoundingClientRect();
  const p = matrix().inverse().transformPoint(new DOMPoint(clientX - rect.left, clientY - rect.top));
  return { x: p.x / state.planW, y: p.y / state.planH };
}
function zoomAt(factor, clientX, clientY) {
  const rect = plan.getBoundingClientRect();
  const sx = clientX - rect.left, sy = clientY - rect.top;
  const before = matrix().inverse().transformPoint(new DOMPoint(sx, sy));
  touch(); state.view.s = Math.min(Math.max(state.view.s * factor, 0.02), 40);
  const after = matrix().transformPoint(before);
  state.view.x += sx - after.x; state.view.y += sy - after.y;
  applyView();
}

function drawOverlay() {
  overlay.replaceChildren();
  if (!currentSheet() || !state.planW) return;
  const W = state.planW, H = state.planH;
  for (const s of visibleSessions()) {
    const pts = s.points;
    if (state.showPaths && pts.length > 1) {
      overlay.append(svg('polyline', { class: 'wt-path', points: pts.map((p) => `${p.x * W},${p.y * H}`).join(' '),
        stroke: s.color, 'stroke-width': state.pathWidth + (s.id === state.editSessionId && state.edit ? 1 : 0) }));
    }
    if (!state.showMarkers) continue;
    for (const p of pts) {
      const g = svg('g', { class: 'wt-marker' + (p.id === state.activePointId ? ' is-active' : ''), 'data-id': p.id, tabindex: '0', role: 'button',
        'aria-label': `${s.name} point ${p.seq}${p.media.length ? '' : ' (no media yet)'}` });
      g.dataset.cx = p.x * W; g.dataset.cy = p.y * H;
      if (p.id === state.activePointId) g.append(svg('circle', { class: 'wt-halo', cx: p.x * W, cy: p.y * H }));
      g.append(svg('circle', { class: 'wt-dot', cx: p.x * W, cy: p.y * H, fill: p.media.length ? s.color : '#64748b', stroke: '#fff', 'stroke-width': 2 }));
      const t = svg('text', { x: p.x * W, y: p.y * H }); t.textContent = p.seq; g.append(t);
      g.addEventListener('click', (e) => { e.stopPropagation(); if (!dragMoved) openPoint(p.id); });
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPoint(p.id); } });
      g.addEventListener('pointerdown', (e) => { if (state.repositionId === p.id) startMarkerDrag(e, p); });
      overlay.append(g);
    }
  }
  sizeMarkers();
}
function sizeMarkers() {
  const k = 1 / state.view.s;
  const r = state.markerSize * k;
  for (const g of overlay.querySelectorAll('.wt-marker')) {
    const dot = g.querySelector('.wt-dot'), halo = g.querySelector('.wt-halo'), text = g.querySelector('text');
    dot.setAttribute('r', r); if (halo) halo.setAttribute('r', r * 2.1);
    text.setAttribute('font-size', r * 1.05);
    // Keep numbers upright when the drawing is rotated.
    text.setAttribute('transform', `rotate(${-state.view.r} ${g.dataset.cx} ${g.dataset.cy})`);
  }
}

// Pan, pinch and wheel zoom.
const pointers = new Map();
let dragMoved = false, panStart = null, pinchStart = null;
plan.addEventListener('pointerdown', (e) => {
  if (e.button !== 0 && e.pointerType === 'mouse') return;
  plan.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  dragMoved = false;
  if (pointers.size === 1) { panStart = { x: e.clientX, y: e.clientY, vx: state.view.x, vy: state.view.y }; plan.classList.add('is-panning'); }
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinchStart = { d: Math.hypot(a.x - b.x, a.y - b.y), s: state.view.s }; panStart = null;
  }
});
plan.addEventListener('pointermove', (e) => {
  if (!pointers.has(e.pointerId) || markerDrag) return;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2 && pinchStart) {
    const [a, b] = [...pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    zoomAt((pinchStart.s * d / pinchStart.d) / state.view.s, (a.x + b.x) / 2, (a.y + b.y) / 2); dragMoved = true;
  } else if (panStart) {
    const dx = e.clientX - panStart.x, dy = e.clientY - panStart.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) dragMoved = true;
    if (dragMoved) touch(); state.view.x = panStart.vx + dx; state.view.y = panStart.vy + dy; applyView();
  }
});
const endPointer = (e) => { pointers.delete(e.pointerId); if (pointers.size < 2) pinchStart = null; if (!pointers.size) { panStart = null; plan.classList.remove('is-panning'); } };
plan.addEventListener('pointerup', endPointer); plan.addEventListener('pointercancel', endPointer);
plan.addEventListener('wheel', (e) => { e.preventDefault(); zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY); }, { passive: false });
plan.addEventListener('click', async (e) => {
  if (dragMoved || e.target.closest('.wt-marker')) return;
  const p = screenToPlan(e.clientX, e.clientY);
  if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) return;
  if (state.repositionId) {
    const id = state.repositionId; state.repositionId = null; updateEditStatus();
    try { await api(`/points/${id}`, { x: p.x, y: p.y }); toast('Point moved.'); await load(); } catch (err) { toast(err.message, true); }
    return;
  }
  if (!state.adding) return;
  if (!state.editSessionId) { toast('Create or select a capture session first.', true); return; }
  try {
    const res = await api('/points', { session_id: state.editSessionId, x: p.x, y: p.y });
    toast(`Point ${res.seq} added.`); await load({ keepPoint: false });
  } catch (err) { toast(err.message, true); }
});
plan.addEventListener('keydown', (e) => {
  const step = 60;
  if (e.key === '+' || e.key === '=') zoomAt(1.25, ...centerOfPlan());
  else if (e.key === '-') zoomAt(0.8, ...centerOfPlan());
  else if (e.key === 'ArrowLeft' && !windowOpen()) { state.view.x += step; applyView(); }
  else if (e.key === 'ArrowRight' && !windowOpen()) { state.view.x -= step; applyView(); }
  else if (e.key === 'ArrowUp') { state.view.y += step; applyView(); }
  else if (e.key === 'ArrowDown') { state.view.y -= step; applyView(); }
});
function centerOfPlan() { const r = plan.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }

let markerDrag = null;
function startMarkerDrag(e, p) {
  e.stopPropagation(); e.preventDefault();
  markerDrag = { id: p.id };
  const move = (ev) => {
    const q = screenToPlan(ev.clientX, ev.clientY);
    const g = overlay.querySelector(`.wt-marker[data-id="${p.id}"]`);
    if (g) for (const c of g.querySelectorAll('circle')) { c.setAttribute('cx', q.x * state.planW); c.setAttribute('cy', q.y * state.planH); }
    markerDrag.pos = q;
  };
  const up = async () => {
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    const q = markerDrag.pos; markerDrag = null; state.repositionId = null; updateEditStatus();
    if (!q || q.x < 0 || q.x > 1 || q.y < 0 || q.y > 1) { drawOverlay(); return; }
    try { await api(`/points/${p.id}`, { x: q.x, y: q.y }); toast('Point moved.'); await load(); } catch (err) { toast(err.message, true); }
  };
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
}

$('wt-toolbar').addEventListener('click', (e) => {
  const act = e.target.closest('button')?.dataset.act;
  if (!act) return;
  if (state.mode === '3d') { three?.toolbar(act); return; }
  if (act === 'zoom-in') zoomAt(1.3, ...centerOfPlan());
  if (act === 'zoom-out') zoomAt(1 / 1.3, ...centerOfPlan());
  if (act === 'rotate') { state.view.r = (state.view.r + 90) % 360; fitView(); }
  if (act === 'reset') { state.view.r = 0; state.touched = false; fitView(); }
});
// Re-fit the drawing when the layout changes, unless the user has already zoomed or panned.
new ResizeObserver(() => { if (state.planW && state.mode === '2d') (state.touched ? applyView() : fitView()); }).observe(plan);

$('wt-show-paths').addEventListener('change', (e) => { state.showPaths = e.target.checked; drawOverlay(); three?.setLayerVisibility(); });
$('wt-show-markers').addEventListener('change', (e) => { state.showMarkers = e.target.checked; drawOverlay(); three?.setLayerVisibility(); });

// ------------------------------------------------------- 360° pano viewer
class PanoViewer {
  constructor(container, url, onView) {
    this.container = container; this.onView = onView;
    this.yaw = 0; this.pitch = 0; this.fov = 75;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.append(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(this.fov, 1, 0.1, 100);
    const geometry = new THREE.SphereGeometry(10, 64, 40); geometry.scale(-1, 1, 1);
    const loading = el('div', { class: 'wt-pane-empty', text: 'Loading 360° image…' }); container.append(loading);
    const texture = new THREE.TextureLoader().load(url, () => { loading.remove(); this.render(); },
      undefined, () => { loading.textContent = 'This 360° image could not be loaded.'; });
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
    this.mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ map: texture }));
    this.scene.add(this.mesh);
    const canvasEl = this.renderer.domElement;
    let drag = null;
    canvasEl.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY, yaw: this.yaw, pitch: this.pitch }; canvasEl.setPointerCapture(e.pointerId); canvasEl.style.cursor = 'grabbing'; });
    canvasEl.addEventListener('pointermove', (e) => {
      if (!drag) return;
      const k = this.fov / canvasEl.clientHeight * Math.PI / 180;
      this.setView(drag.yaw + (e.clientX - drag.x) * k, drag.pitch + (e.clientY - drag.y) * k, this.fov, true);
    });
    canvasEl.addEventListener('pointerup', () => { drag = null; canvasEl.style.cursor = ''; });
    canvasEl.addEventListener('wheel', (e) => { e.preventDefault(); this.setView(this.yaw, this.pitch, this.fov + e.deltaY * 0.05, true); }, { passive: false });
    this.resizeObserver = new ResizeObserver(() => this.resize()); this.resizeObserver.observe(container);
    this.resize();
  }
  setView(yaw, pitch, fov, emit = false) {
    this.yaw = yaw; this.pitch = Math.max(-Math.PI / 2 + 0.01, Math.min(Math.PI / 2 - 0.01, pitch));
    this.fov = Math.max(30, Math.min(100, fov));
    this.render();
    if (emit && this.onView) this.onView(this.yaw, this.pitch, this.fov);
  }
  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.render();
  }
  render() {
    this.camera.fov = this.fov; this.camera.updateProjectionMatrix();
    const target = new THREE.Vector3(Math.sin(this.yaw) * Math.cos(this.pitch), Math.sin(this.pitch), -Math.cos(this.yaw) * Math.cos(this.pitch));
    this.camera.lookAt(target);
    this.renderer.render(this.scene, this.camera);
  }
  dispose() {
    this.resizeObserver.disconnect();
    this.mesh.geometry.dispose(); this.mesh.material.map?.dispose(); this.mesh.material.dispose();
    this.renderer.dispose(); this.renderer.forceContextLoss(); this.renderer.domElement.remove();
  }
}

// ------------------------------------------------------- floating window
const win = $('wt-window');
let panes = [];
const imm = $('wt-imm');
const windowOpen = () => !win.hidden || !imm.hidden;
state.viewMode = 'immersive'; // 'immersive' (full workspace) or 'window' (floating)
state.look = { yaw: 0, pitch: 0, fov: 75 };

function pointMedia(point) {
  // Prefer 360° images, then photos, then videos.
  const order = { pano: 0, photo: 1, video: 2 };
  return [...point.media].sort((a, b) => order[a.kind] - order[b.kind] || a.id - b.id);
}

function nearestEarlier(found) {
  // The closest point on the same drawing from another capture date, if any has media.
  const { session, point } = found;
  let best = null;
  for (const s of sheetSessions()) {
    if (s.id === session.id || s.date === session.date) continue;
    for (const p of s.points) {
      if (!p.media.length) continue;
      const d = Math.hypot(p.x - point.x, (p.y - point.y) * (state.planH / (state.planW || 1)));
      if (d < 0.05 && (!best || d < best.d - 1e-9 || (Math.abs(d - best.d) < 0.01 && s.date > best.session.date))) best = { d, session: s, point: p };
    }
  }
  return best;
}

function disposePanes() { for (const p of panes) p.viewer?.dispose?.(); panes = []; }

function mountMedia(container, media, tag) {
  container.replaceChildren();
  const pane = { viewer: null };
  if (!media) { container.append(el('div', { class: 'wt-pane-empty', text: 'No media at this location yet.' })); return pane; }
  if (media.kind === 'pano') {
    pane.viewer = new PanoViewer(container, media.url, (yaw, pitch, fov) => {
      if (pane === panes[0]) { state.look = { yaw, pitch, fov }; drawCone(); }
      if (!state.syncCompare) return;
      for (const other of panes) if (other !== pane && other.viewer instanceof PanoViewer) other.viewer.setView(yaw, pitch, fov);
    });
    // Keep the viewing direction when walking from point to point.
    pane.viewer.setView(state.look.yaw, state.look.pitch, state.look.fov);
  } else if (media.kind === 'photo') {
    container.append(el('img', { src: media.url, alt: media.name, loading: 'lazy' }));
  } else {
    const video = el('video', { src: media.url, controls: true, playsinline: true, preload: 'metadata' });
    container.append(video);
  }
  if (tag) container.append(el('div', { class: 'wt-pane-tag', text: tag }));
  return pane;
}

function openPoint(pointId, { keepMedia = false } = {}) {
  const found = findPoint(pointId);
  if (!found) return;
  const { session, point } = found;
  if (state.activePointId !== pointId || !keepMedia) state.mediaIndex = 0;
  state.activePointId = pointId;
  const media = pointMedia(point);
  state.mediaIndex = Math.min(state.mediaIndex, Math.max(0, media.length - 1));
  const immersive = state.viewMode === 'immersive';
  win.hidden = immersive; imm.hidden = !immersive;
  $('wt-window-title').textContent = `${session.name} · Point ${point.seq}${point.label ? ' — ' + point.label : ''}`;
  const sheet = sheets().find((s) => s.id === session.sheet_id);
  $('wt-window-sub').textContent = `${fmtDate(session.date)}${sheet ? ' · ' + (sheet.floor ? sheet.floor + ' · ' : '') + sheet.name : ''} · ${point.seq} of ${session.points.length}`;
  const pick = $('wt-media-pick'); pick.replaceChildren();
  media.forEach((m, i) => pick.append(el('option', { value: i, text: `${KIND_LABEL[m.kind]} · ${m.name}` })));
  pick.hidden = media.length < 2; pick.value = state.mediaIndex;
  const idx = session.points.findIndex((p) => p.id === pointId);
  $('wt-prev').disabled = idx <= 0; $('wt-next').disabled = idx >= session.points.length - 1;
  $('wt-window-foot').hidden = !state.edit;
  disposePanes();
  const body = immersive ? $('wt-imm-body') : $('wt-window-body'); body.replaceChildren();
  (immersive ? $('wt-window-body') : $('wt-imm-body')).replaceChildren();
  const main = el('div', { class: 'wt-pane' }); body.append(main);
  const earlier = state.compare ? nearestEarlier(found) : null;
  panes.push(mountMedia(main, media[state.mediaIndex], state.compare ? fmtDate(session.date) : ''));
  if (state.compare) {
    const second = el('div', { class: 'wt-pane' }); body.append(second);
    if (earlier) panes.push(mountMedia(second, pointMedia(earlier.point)[0], `${fmtDate(earlier.session.date)} · ${earlier.session.name} #${earlier.point.seq}`));
    else second.append(el('div', { class: 'wt-pane-empty', text: 'No capture from another date near this location.' }));
  }
  $('wt-compare').setAttribute('aria-pressed', String(state.compare));
  $('wt-compare').style.borderColor = state.compare ? 'var(--wt-blue)' : '';
  if (immersive) updateImmersive(found);
  try { history.replaceState(null, '', '#point=' + pointId); } catch { /* ignore */ }
  drawOverlay(); three?.highlight();
}
function closeWindow() {
  stopPlay(); disposePanes(); win.hidden = true; imm.hidden = true; state.activePointId = null;
  try { history.replaceState(null, '', location.pathname + location.search); } catch { /* ignore */ }
  drawOverlay(); three?.highlight();
}

// ----------------------------------------------------- immersive controls
const miniCanvas = $('wt-mini-canvas'), miniSvg = $('wt-mini-svg');
function updateImmersive(found) {
  const { session, point } = found;
  const sheet = sheets().find((s) => s.id === session.sheet_id);
  $('wt-mini-floor').textContent = sheet ? `${sheet.floor ? sheet.floor + ' · ' : ''}${sheet.name}` : '';
  if (miniCanvas.dataset.sheet !== String(state.sheetId) && state.planW) {
    const w = 640; miniCanvas.width = w; miniCanvas.height = Math.round(w * state.planH / state.planW);
    miniCanvas.getContext('2d').drawImage(canvas, 0, 0, miniCanvas.width, miniCanvas.height);
    miniCanvas.dataset.sheet = String(state.sheetId);
  }
  const W = state.planW || 1, H = state.planH || 1;
  miniSvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  miniSvg.replaceChildren();
  const px = point.x * W, py = point.y * H;
  miniSvg.append(svg('line', { class: 'wt-mini-cross', x1: px, y1: 0, x2: px, y2: H }),
    svg('line', { class: 'wt-mini-cross', x1: 0, y1: py, x2: W, y2: py }));
  if (session.points.length > 1) miniSvg.append(svg('polyline', { class: 'wt-mini-route', stroke: session.color,
    points: session.points.map((p) => `${p.x * W},${p.y * H}`).join(' ') }));
  miniSvg.append(svg('path', { class: 'wt-mini-cone', id: 'wt-mini-cone' }));
  const r = W / 110;
  for (const p of session.points) {
    if (p.id === point.id) continue;
    const dot = svg('circle', { class: 'wt-mini-dot', cx: p.x * W, cy: p.y * H, r, fill: session.color });
    dot.addEventListener('click', (e) => { e.stopPropagation(); openPoint(p.id); });
    miniSvg.append(dot);
  }
  miniSvg.append(svg('rect', { class: 'wt-mini-here', x: px - r * 1.3, y: py - r * 1.3, width: r * 2.6, height: r * 2.6,
    transform: `rotate(45 ${px} ${py})` }));
  drawCone();
  const dates = datesWithMedia();
  const di = dates.indexOf(session.date);
  $('wt-imm-date').textContent = fmtDate(session.date);
  $('wt-imm-dprev').disabled = di <= 0; $('wt-imm-dnext').disabled = di < 0 || di >= dates.length - 1;
  const idx = session.points.findIndex((p) => p.id === point.id);
  $('wt-imm-count').textContent = `${idx + 1} / ${session.points.length}`;
  $('wt-imm-first').disabled = $('wt-imm-prev').disabled = idx <= 0;
  $('wt-imm-last').disabled = $('wt-imm-next').disabled = idx >= session.points.length - 1;
  $('wt-imm-session').textContent = session.name;
  $('wt-imm-compare').setAttribute('aria-pressed', String(state.compare));
}
function drawCone() {
  const cone = document.getElementById('wt-mini-cone');
  const found = state.activePointId && findPoint(state.activePointId);
  if (!cone || !found) return;
  // Heading 0 faces the top of the drawing; the cone widens with the field of view.
  const W = state.planW || 1, R = W * 0.09;
  const px = found.point.x * W, py = found.point.y * (state.planH || 1);
  const half = (state.look.fov / 2) * Math.PI / 180;
  const a1 = state.look.yaw - half, a2 = state.look.yaw + half;
  cone.setAttribute('d', `M${px},${py} L${px + Math.sin(a1) * R},${py - Math.cos(a1) * R} A${R},${R} 0 0 1 ${px + Math.sin(a2) * R},${py - Math.cos(a2) * R} Z`);
}
function datesWithMedia() {
  return [...new Set(sheetSessions().filter((s) => s.points.some((p) => p.media.length)).map((s) => s.date))].sort();
}
function jumpDate(delta) {
  const found = findPoint(state.activePointId); if (!found) return;
  const dates = datesWithMedia(); const target = dates[dates.indexOf(found.session.date) + delta];
  if (!target) return;
  let best = null;
  for (const s of sheetSessions().filter((x) => x.date === target)) for (const p of s.points) {
    if (!p.media.length) continue;
    const d = Math.hypot(p.x - found.point.x, p.y - found.point.y);
    if (!best || d < best.d) best = { d, p };
  }
  if (!best) return;
  state.date = target; renderDates(); renderSidebar();
  openPoint(best.p.id);
}
let playTimer = null;
function stopPlay() { clearInterval(playTimer); playTimer = null; $('wt-imm-play').textContent = '▶'; $('wt-imm-play').title = 'Play walk'; }
function togglePlay() {
  if (playTimer) { stopPlay(); return; }
  $('wt-imm-play').textContent = '⏸'; $('wt-imm-play').title = 'Pause';
  playTimer = setInterval(() => {
    const found = findPoint(state.activePointId);
    const pts = found ? found.session.points : [];
    if (!found || pts[pts.length - 1].id === state.activePointId) { stopPlay(); return; }
    step(1);
  }, 3500);
}
function jumpEnd(last) {
  const found = findPoint(state.activePointId); if (!found) return;
  const pts = found.session.points; const target = last ? pts[pts.length - 1] : pts[0];
  if (target) openPoint(target.id);
}
function zoomPano(delta) { const v = panes[0]?.viewer; if (v instanceof PanoViewer) v.setView(v.yaw, v.pitch, v.fov + delta, true); }
function setViewMode(mode) { state.viewMode = mode; if (state.activePointId) openPoint(state.activePointId, { keepMedia: true }); }
$('wt-imm-dprev').addEventListener('click', () => jumpDate(-1));
$('wt-imm-dnext').addEventListener('click', () => jumpDate(1));
$('wt-imm-prev').addEventListener('click', () => { stopPlay(); step(-1); });
$('wt-imm-next').addEventListener('click', () => { stopPlay(); step(1); });
$('wt-imm-first').addEventListener('click', () => { stopPlay(); jumpEnd(false); });
$('wt-imm-last').addEventListener('click', () => { stopPlay(); jumpEnd(true); });
$('wt-imm-play').addEventListener('click', togglePlay);
$('wt-imm-zin').addEventListener('click', () => zoomPano(-10));
$('wt-imm-zout').addEventListener('click', () => zoomPano(10));
$('wt-imm-compare').addEventListener('click', () => { state.compare = !state.compare; openPoint(state.activePointId, { keepMedia: true }); });
$('wt-imm-exit').addEventListener('click', closeWindow);
$('wt-mini-map').addEventListener('click', closeWindow);
$('wt-imm-pop').addEventListener('click', () => setViewMode('window'));
$('wt-to-imm').addEventListener('click', () => setViewMode('immersive'));
$('wt-imm-edit')?.addEventListener('click', () => setViewMode('window'));
$('wt-imm-link').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(location.href); toast('Link to this location copied. Viewers still need access to the project.'); }
  catch { toast('Copy the address bar to share this location.'); }
});

function step(delta) {
  const found = findPoint(state.activePointId); if (!found) return;
  const pts = found.session.points; const i = pts.findIndex((p) => p.id === state.activePointId);
  const next = pts[i + delta]; if (next) { openPoint(next.id); centerOn(next); }
}
function centerOn(point) {
  if (state.mode !== '2d' || !state.planW) return;
  const rect = plan.getBoundingClientRect();
  const screen = matrix().transformPoint(new DOMPoint(point.x * state.planW, point.y * state.planH));
  const margin = 80;
  if (screen.x < margin || screen.y < margin || screen.x > rect.width - margin || screen.y > rect.height - margin) {
    state.view.x += rect.width / 2 - screen.x; state.view.y += rect.height / 2 - screen.y; applyView();
  }
}
$('wt-prev').addEventListener('click', () => step(-1));
$('wt-next').addEventListener('click', () => step(1));
$('wt-close').addEventListener('click', closeWindow);
$('wt-max').addEventListener('click', () => win.classList.toggle('is-max'));
$('wt-compare').addEventListener('click', () => { state.compare = !state.compare; if (state.activePointId) openPoint(state.activePointId, { keepMedia: true }); });
$('wt-media-pick').addEventListener('change', (e) => { state.mediaIndex = Number(e.target.value); openPoint(state.activePointId, { keepMedia: true }); });
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input,select,textarea')) return;
  if (e.key === 'Escape') { if (state.adding || state.repositionId || state.placing3d) { setAdding(false); state.repositionId = null; setPlacing3d(false); updateEditStatus(); } else if (windowOpen()) closeWindow(); }
  if (windowOpen() && e.key === 'ArrowLeft') step(-1);
  if (windowOpen() && e.key === 'ArrowRight') step(1);
});

// Drag and resize the floating window.
(function windowChrome() {
  const head = $('wt-window-head');
  head.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button,select') || win.classList.contains('is-max')) return;
    const r = win.getBoundingClientRect(); const ox = e.clientX - r.left, oy = e.clientY - r.top;
    const move = (ev) => {
      win.style.right = 'auto';
      win.style.left = Math.max(0, Math.min(window.innerWidth - 80, ev.clientX - ox)) + 'px';
      win.style.top = Math.max(0, Math.min(window.innerHeight - 40, ev.clientY - oy)) + 'px';
    };
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  });
  $('wt-resize').addEventListener('pointerdown', (e) => {
    e.preventDefault(); const r = win.getBoundingClientRect(); const sx = e.clientX, sy = e.clientY;
    win.style.right = 'auto'; win.style.left = r.left + 'px'; win.style.top = r.top + 'px';
    const move = (ev) => { win.style.width = Math.max(300, r.width + ev.clientX - sx) + 'px'; win.style.height = Math.max(240, r.height + ev.clientY - sy) + 'px'; };
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  });
})();

// ------------------------------------------------------------- 3D mode
let three = null;
class ModelView {
  constructor() {
    this.canvas = $('wt-3d-canvas');
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.scene = new THREE.Scene(); this.scene.background = new THREE.Color('#18191b');
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.05, 5000);
    this.camera.position.set(20, 20, 20);
    this.controls = new OrbitControls(this.camera, this.canvas); this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x334155, 2.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(30, 50, 20); this.scene.add(sun);
    this.grid = new THREE.GridHelper(100, 50, 0x4a4c52, 0x2c2d31); this.scene.add(this.grid);
    this.models = new Map(); this.markerGroup = new THREE.Group(); this.scene.add(this.markerGroup);
    this.raycaster = new THREE.Raycaster(); this.size = 10;
    this.loader = new GLTFLoader(); this.loader.setWithCredentials(true);
    new ResizeObserver(() => this.resize()).observe($('wt-3d'));
    let down = null;
    this.canvas.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY }; });
    this.canvas.addEventListener('pointerup', (e) => { if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 5) this.click(e); down = null; });
    this.resize();
  }
  resize() {
    const box = $('wt-3d'); const w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false); this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
  start() { this.resize(); this.renderer.setAnimationLoop(() => { this.controls.update(); this.renderer.render(this.scene, this.camera); }); }
  stop() { this.renderer.setAnimationLoop(null); }
  async sync() {
    const wanted = state.data?.models || [];
    for (const [id, entry] of this.models) if (!wanted.some((m) => m.id === id)) { this.scene.remove(entry.root); this.models.delete(id); }
    const hint = $('wt-3d-hint');
    if (!wanted.length) { hint.textContent = cfg.canEdit ? 'No 3D model yet. Turn on Edit and upload a GLB under “Set up”.' : 'No 3D model has been added to this project.'; return; }
    const pending = wanted.filter((m) => !this.models.has(m.id));
    if (pending.length) hint.textContent = 'Loading 3D model…';
    await Promise.all(pending.map((m) => new Promise((resolve) => {
      this.loader.load(m.url, (gltf) => { this.models.set(m.id, { root: gltf.scene, meta: m }); this.scene.add(gltf.scene); resolve(); },
        undefined, (err) => { console.error(err); toast(`3D model "${m.name}" could not be loaded.`, true); resolve(); });
    })));
    hint.textContent = '';
    this.applyVisibility();
    if (pending.length) this.frame();
    render3DMarkers();
  }
  applyVisibility() { for (const [id, entry] of this.models) entry.root.visible = !state.hiddenModels.has(id); }
  frame() {
    const box = new THREE.Box3();
    for (const entry of this.models.values()) if (entry.root.visible) box.expandByObject(entry.root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
    this.size = Math.max(size.x, size.y, size.z) || 10;
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(new THREE.Vector3(1, 0.9, 1.1).normalize().multiplyScalar(this.size * 1.4));
    this.camera.near = this.size / 1000; this.camera.far = this.size * 20; this.camera.updateProjectionMatrix();
    this.grid.position.set(center.x, box.min.y - 0.001, center.z);
    this.grid.scale.setScalar(Math.max(1, this.size / 60));
  }
  toolbar(act) {
    if (act === 'reset') return this.frame();
    if (act === 'rotate') { const off = this.camera.position.clone().sub(this.controls.target); off.applyAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI / 2); this.camera.position.copy(this.controls.target).add(off); return; }
    const off = this.camera.position.clone().sub(this.controls.target).multiplyScalar(act === 'zoom-in' ? 0.75 : 1.33);
    this.camera.position.copy(this.controls.target).add(off);
  }
  markers() {
    for (const child of [...this.markerGroup.children]) { child.geometry?.dispose(); child.material?.map?.dispose(); child.material?.dispose(); this.markerGroup.remove(child); }
    const r = this.size * 0.012 * (state.markerSize / 11);
    for (const s of visibleSessions()) {
      const pts = s.points.filter((p) => p.m && this.models.has(p.model_id) && !state.hiddenModels.has(p.model_id));
      if (state.showPaths && pts.length > 1) {
        const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts.map((p) => new THREE.Vector3(...p.m))),
          new THREE.LineBasicMaterial({ color: s.color, depthTest: false, transparent: true }));
        line.renderOrder = 2; line.userData.kind = 'path'; this.markerGroup.add(line);
      }
      if (!state.showMarkers) continue;
      for (const p of pts) {
        const active = p.id === state.activePointId;
        const mesh = new THREE.Mesh(new THREE.SphereGeometry(r * (active ? 1.6 : 1), 20, 14),
          new THREE.MeshBasicMaterial({ color: active ? '#fbbf24' : (p.media.length ? s.color : '#64748b'), depthTest: false, transparent: true }));
        mesh.position.set(...p.m); mesh.renderOrder = 3; mesh.userData.pointId = p.id; this.markerGroup.add(mesh);
        const label = this.label(String(p.seq)); label.position.set(p.m[0], p.m[1] + r * 2.4, p.m[2]); label.scale.setScalar(r * 2.6); this.markerGroup.add(label);
      }
    }
  }
  label(text) {
    const c = document.createElement('canvas'); c.width = c.height = 64; const g = c.getContext('2d');
    g.fillStyle = 'rgba(24,25,27,.88)'; g.beginPath(); g.arc(32, 32, 28, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#fff'; g.font = 'bold 30px sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(text, 32, 34);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false, transparent: true }));
    sprite.renderOrder = 4; return sprite;
  }
  highlight() { this.markers(); }
  setLayerVisibility() { this.markers(); }
  pick(e, objects) {
    const rect = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    return this.raycaster.intersectObjects(objects, true);
  }
  async click(e) {
    if (state.placing3d) {
      const found = findPoint(state.activePointId);
      const roots = [...this.models.entries()].filter(([id]) => !state.hiddenModels.has(id)).map(([, v]) => v.root);
      const hit = this.pick(e, roots)[0];
      if (!found || !hit) { toast('Click on the model surface.', true); return; }
      let modelId = null; for (const [id, entry] of this.models) { let o = hit.object; while (o) { if (o === entry.root) modelId = id; o = o.parent; } }
      try {
        await api(`/points/${found.point.id}`, { model_id: modelId, m: [hit.point.x, hit.point.y, hit.point.z] });
        toast(`Point ${found.point.seq} placed in 3D.`); setPlacing3d(false); await load();
      } catch (err) { toast(err.message, true); }
      return;
    }
    const hit = this.pick(e, this.markerGroup.children.filter((c) => c.userData.pointId))[0];
    if (hit) openPoint(hit.object.userData.pointId);
  }
}
function render3DMarkers() { if (three) three.markers(); }

async function setMode(mode) {
  state.mode = mode;
  for (const b of document.querySelectorAll('.wt-mode')) { const on = b.dataset.mode === mode; b.classList.toggle('is-active', on); b.setAttribute('aria-selected', String(on)); }
  $('wt-3d').hidden = mode !== '3d';
  plan.hidden = mode !== '2d' || !currentSheet();
  $('wt-empty').hidden = mode !== '2d' || !!currentSheet();
  $('wt-place-3d').hidden = mode !== '3d';
  $('wt-add-points').hidden = mode !== '2d';
  if (mode === '3d') { setAdding(false); three ||= new ModelView(); three.start(); await three.sync(); }
  else { three?.stop(); setPlacing3d(false); if (state.planW) applyView(); }
}
for (const b of document.querySelectorAll('.wt-mode')) b.addEventListener('click', () => setMode(b.dataset.mode));

// ------------------------------------------------------------ edit tools
function setAdding(on) { state.adding = on; $('wt-add-points')?.setAttribute('aria-pressed', String(on)); plan.classList.toggle('is-adding', on); updateEditStatus(); }
function setPlacing3d(on) { state.placing3d = on; $('wt-place-3d')?.setAttribute('aria-pressed', String(on)); updateEditStatus(); }
function updateEditStatus() {
  const s = allSessions().find((x) => x.id === state.editSessionId);
  let msg = s ? `Editing: ${s.name}` : 'Create a capture session to start a route';
  if (state.adding) msg = s ? `Click the drawing to add point ${s.points.length + 1} to ${s.name}` : 'Select a session first';
  if (state.repositionId) msg = 'Drag the point, or click its new position';
  if (state.placing3d) msg = 'Click the model where this point was captured';
  $('wt-edit-status').textContent = msg;
}
if (cfg.canEdit) {
  $('wt-edit-toggle').addEventListener('click', () => {
    state.edit = !state.edit;
    $('wt-edit-toggle').setAttribute('aria-pressed', String(state.edit));
    for (const node of document.querySelectorAll('.wt-edit-only')) node.hidden = !state.edit;
    if (!state.edit) { setAdding(false); setPlacing3d(false); state.repositionId = null; }
    $('wt-place-3d').hidden = state.mode !== '3d'; $('wt-add-points').hidden = state.mode !== '2d';
    renderSidebar(); drawOverlay(); updateEditStatus();
    if (state.activePointId) openPoint(state.activePointId, { keepMedia: true });
  });
  $('wt-add-points').addEventListener('click', () => setAdding(!state.adding));
  $('wt-place-3d').addEventListener('click', () => {
    if (!state.activePointId) { toast('Open a point first (click it on the 2D plan), then place it in 3D.', true); return; }
    setPlacing3d(!state.placing3d);
  });
  const upload = (formId, path, label) => $(formId).addEventListener('submit', async (e) => {
    e.preventDefault(); const form = e.target; const fd = new FormData(form); fd.append('project', cfg.project);
    const button = form.querySelector('button'); button.disabled = true; button.textContent = 'Uploading…';
    try { await api(path, fd, true); toast(`${label} uploaded.`); form.reset(); sheetCache.clear(); delete plan.dataset.sheet; await load(); if (state.mode === '3d') await three?.sync(); }
    catch (err) { toast(err.message, true); }
    finally { button.disabled = false; button.textContent = `Upload ${label.toLowerCase()}`; }
  });
  upload('wt-sheet-form', '/sheets', 'Drawing');
  upload('wt-model-form', '/models', 'Model');
  $('wt-session-form').elements.date.valueAsDate = new Date();
  $('wt-session-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!state.sheetId) { toast('Upload a drawing first.', true); return; }
    const f = e.target.elements;
    try {
      const res = await api('/sessions', { sheet_id: state.sheetId, name: f.name.value, date: f.date.value, color: f.color.value });
      state.editSessionId = res.id; state.date = f.date.value; f.name.value = '';
      toast('Session created. Click “Add points”, then click the drawing.'); await load();
    } catch (err) { toast(err.message, true); }
  });
  $('wt-media-form').addEventListener('submit', async (e) => {
    e.preventDefault(); if (!state.activePointId) return;
    const fd = new FormData(e.target); const button = e.target.querySelector('button');
    button.disabled = true; button.textContent = 'Uploading…';
    try { await api(`/points/${state.activePointId}/media`, fd, true); toast('Media added.'); e.target.reset(); await load(); }
    catch (err) { toast(err.message, true); }
    finally { button.disabled = false; button.textContent = 'Add to this location'; }
  });
  $('wt-window-foot').addEventListener('click', async (e) => {
    const act = e.target.closest('[data-pt]')?.dataset.pt; const id = state.activePointId;
    if (!act || !id) return;
    try {
      if (act === 'up' || act === 'down') { await api(`/points/${id}/move`, { direction: act }); await load(); }
      if (act === 'drag') { if (state.mode !== '2d') await setMode('2d'); state.repositionId = id; setAdding(false); updateEditStatus(); drawOverlay(); toast('Drag the point, or click its new position.'); }
      if (act === 'delete' && confirm('Delete this point and its media? Later points are renumbered.')) { await api(`/points/${id}/delete`); closeWindow(); await load(); }
    } catch (err) { toast(err.message, true); }
  });
}

// ------------------------------------------------- panel, share, chrome
function openPanel(title, ...content) {
  $('wt-panel-body').replaceChildren(el('h2', { text: title }), ...content);
  $('wt-panel').hidden = false;
}
$('wt-panel-close').addEventListener('click', () => { $('wt-panel').hidden = true; });
$('wt-settings').addEventListener('click', () => {
  const range = (label, min, max, value, onInput) => {
    const input = el('input', { type: 'range', min, max, value });
    input.addEventListener('input', () => onInput(Number(input.value)));
    return el('label', { class: 'wt-form' }, el('span', { text: label }), input);
  };
  const sync = el('input', { type: 'checkbox' }); sync.checked = state.syncCompare;
  sync.addEventListener('change', () => { state.syncCompare = sync.checked; });
  openPanel('Display settings',
    range('Marker size', 6, 22, state.markerSize, (v) => { state.markerSize = v; sizeMarkers(); render3DMarkers(); }),
    range('Path width', 1, 8, state.pathWidth, (v) => { state.pathWidth = v; drawOverlay(); }),
    el('label', { class: 'wt-check' }, sync, el('span', { text: 'Link 360° views when comparing dates' })));
});
$('wt-share')?.addEventListener('click', () => openPanel('Share with a client',
  el('p', { text: 'Clients open this walkthrough with the project’s existing share link and four-letter code. “Media” must be allowed for this project.' }),
  el('p', { class: 'wt-floor', text: 'Manage links, codes and expiry under Admin → Project access.' }),
  el('a', { class: 'wt-btn', href: '/admin#project-access', text: 'Open project access' })));
$('wt-fullscreen').addEventListener('click', () => { document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.(); });
$('wt-collapse').addEventListener('click', () => { $('wt-side').classList.toggle('is-collapsed'); setTimeout(() => { if (state.mode === '2d' && state.planW) applyView(); three?.resize(); }, 220); });
if (window.matchMedia('(max-width: 860px)').matches) $('wt-side').classList.add('is-collapsed');

load({ keepPoint: false }).then(() => {
  // Open a shared location link (#point=123) once the project has loaded.
  const match = /#point=(\d+)/.exec(location.hash);
  const target = match && findPoint(Number(match[1]));
  if (target) {
    if (target.session.sheet_id !== state.sheetId) { state.sheetId = target.session.sheet_id; delete plan.dataset.sheet; }
    state.date = target.session.date; renderSidebar(); renderDates();
    renderPlan().then(() => openPoint(target.point.id));
  }
});
