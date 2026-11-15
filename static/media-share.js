/* Public media share pages: tag filter, search, player dialog, map date picker. No inline scripts. */
(() => {
  const $ = id => document.getElementById(id);
  // Map tab: change date without a separate button.
  document.querySelectorAll('select[data-autosubmit]').forEach(select => select.addEventListener('change', () => select.form.submit()));
  const target = document.querySelector('[data-scroll-into-view]');
  if (target) target.scrollIntoView({ block: 'start' });

  const tiles = [...document.querySelectorAll('.ms-tile')];
  if (!tiles.length) return;
  const chips = [...document.querySelectorAll('.ms-chip')], search = $('ms-search'), none = $('ms-none');
  let tag = '';
  const apply = () => {
    const q = (search && search.value || '').trim().toLowerCase(); let shown = 0;
    tiles.forEach(tile => {
      const ok = (!tag || tile.dataset.tags.split('|').includes(tag)) && (!q || tile.dataset.title.includes(q));
      tile.hidden = !ok; if (ok) shown++;
    });
    if (none) none.hidden = shown > 0;
  };
  chips.forEach(chip => chip.addEventListener('click', () => { tag = chip.dataset.tag; chips.forEach(c => c.classList.toggle('is-active', c === chip)); apply(); }));
  if (search) search.addEventListener('input', apply);

  const dialog = $('ms-dialog'), video = $('ms-dialog-video'), frame = $('ms-dialog-frame'), download = $('ms-dialog-download');
  if (!dialog) return;
  const reset = () => {
    video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
    frame.textContent = ''; frame.hidden = true;
  };
  const open = () => { if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', ''); };
  const close = () => { reset(); if (typeof dialog.close === 'function' && dialog.open) dialog.close(); else dialog.removeAttribute('open'); };
  tiles.forEach(tile => {
    const button = tile.querySelector('.ms-tile-open');
    button.addEventListener('click', async () => {
      $('ms-dialog-title').textContent = button.dataset.name;
      $('ms-dialog-meta').textContent = button.dataset.meta;
      reset(); open();
      let data = {};
      try {
        const response = await fetch(button.dataset.play, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        data = await response.json();
      } catch (e) { data = {}; }
      if (data.download) download.href = data.download;
      if (data.embed) {
        const iframe = document.createElement('iframe');
        iframe.src = data.embed; iframe.title = button.dataset.name;
        iframe.allow = 'accelerometer; gyroscope; autoplay; encrypted-media; picture-in-picture; fullscreen';
        iframe.setAttribute('allowfullscreen', '');
        frame.appendChild(iframe); frame.hidden = false;
      } else if (data.src) {
        video.src = data.src; video.hidden = false; video.play().catch(() => {});
      }
    });
  });
  $('ms-close').addEventListener('click', close);
  dialog.addEventListener('click', event => { if (event.target === dialog) close(); });
  dialog.addEventListener('cancel', reset);
})();
