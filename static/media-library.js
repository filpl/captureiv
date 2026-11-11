/* Media library (team page): copy links, share options, uploads (direct-to-cloud when available). */
(() => {
  const pad = n => String(n).padStart(2, '0');
  const now = new Date();
  const localToday = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  document.querySelectorAll('[data-tz-offset]').forEach(input => { input.value = String(now.getTimezoneOffset()); });
  document.querySelectorAll('[data-local-min]').forEach(input => { input.min = localToday; });

  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    const input = document.getElementById(button.dataset.copy); if (!input) return;
    try { await navigator.clipboard.writeText(input.value); }
    catch (e) { input.classList.remove('sr-only'); input.select(); document.execCommand('copy'); input.classList.add('sr-only'); }
    const label = button.textContent; button.textContent = 'Copied'; setTimeout(() => { button.textContent = label; }, 1600);
  }));
  document.querySelectorAll('[data-mode-toggle]').forEach(radio => radio.addEventListener('change', () => {
    const box = document.getElementById(radio.dataset.modeToggle + '-keyword'); if (!box) return;
    const on = radio.form.querySelector('input[name=mode]:checked').value === 'keyword';
    box.hidden = !on; box.querySelector('input').required = on;
  }));
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));
  const mapToggle = document.getElementById('lib-show-map'), mapProject = document.getElementById('lib-map-project');
  if (mapToggle && mapProject) mapToggle.addEventListener('change', () => { mapProject.hidden = !mapToggle.checked; });

  const form = document.getElementById('ml-upload-form');
  if (!form) return;
  const fileInput = form.querySelector('input[name=file]');
  const progress = form.querySelector('.ml-progress'), bar = progress.querySelector('span'), pct = progress.querySelector('b');
  const errorBox = form.querySelector('.ml-upload-error'), button = form.querySelector('button');
  const maxBytes = Number(form.dataset.maxBytes) || 0;
  const gb = bytes => (bytes / 1073741824).toFixed(bytes >= 10737418240 ? 0 : 1);
  const showError = message => { errorBox.textContent = message; errorBox.hidden = false; button.disabled = false; progress.hidden = true; };
  const setProgress = (done, total) => { const p = total ? Math.round(done / total * 100) : 0; bar.style.width = p + '%'; pct.textContent = p + '%'; };
  const tooBig = file => maxBytes && file && file.size > maxBytes;
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (tooBig(file)) showError(`This file is ${gb(file.size)} GB. The limit is ${gb(maxBytes)} GB.`); else errorBox.hidden = true;
  });
  form.addEventListener('submit', async event => {
    const file = fileInput.files[0];
    if (!file || !window.FormData || !window.XMLHttpRequest) return;
    event.preventDefault();
    if (tooBig(file)) { showError(`This file is ${gb(file.size)} GB. The limit is ${gb(maxBytes)} GB.`); return; }
    errorBox.hidden = true; progress.hidden = false; button.disabled = true; setProgress(0, 1);
    const value = name => (form.querySelector(`[name=${name}]`) || {}).value || '';
    if (form.dataset.r2 && window.uploadR2Multipart) {
      try {
        const result = await window.uploadR2Multipart(file, {
          project: value('project'), new_project: value('new_project'), title: value('title'), tags: value('tags'),
        }, { csrf: form.dataset.csrf, baseUrl: form.dataset.r2Base, onProgress: setProgress });
        location.href = (result && result.redirect) || form.dataset.done;
      } catch (error) { showError(error.message || 'Upload failed. Press Upload again to resume.'); }
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', form.action); xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.upload.onprogress = e => { if (e.lengthComputable) setProgress(e.loaded, e.total); };
    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status >= 200 && xhr.status < 300 && data.redirect) { location.href = data.redirect; return; }
      showError(data.error || 'Upload failed. Please try again.');
    };
    xhr.onerror = () => showError('The connection dropped. Please try again.');
    xhr.send(new FormData(form));
  });
})();
