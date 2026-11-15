/* Hand the shared map's pins to project-viewer-map.js without inline scripts (strict CSP). */
(() => {
  let pins = [];
  try { pins = JSON.parse(document.getElementById('viewer-map-pins')?.textContent || '[]'); } catch (e) { pins = []; }
  window.PROJECT_MAP_PINS = Array.isArray(pins) ? pins : [];
  window.PROJECT_PHOTO_PINS = [];
})();
