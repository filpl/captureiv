(() => {
  const dateToggle = document.getElementById('flight-dates-toggle');
  const dateRail = document.querySelector('.viewer-date-rail');
  const calendar = document.getElementById('flight-date-calendar');
  const dayGrid = document.getElementById('calendar-days');
  const monthLabel = document.getElementById('calendar-month');
  const availableDates = new Set(JSON.parse(document.getElementById('viewer-available-dates')?.textContent || '[]'));
  const selectedISO = dateRail?.dataset.selectedDate || '';
  let monthCursor = selectedISO ? new Date(`${selectedISO}T12:00:00`) : new Date();
  monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth(), 1);

  const renderCalendar = () => {
    if (!dayGrid || !monthLabel) return;
    const year = monthCursor.getFullYear(), month = monthCursor.getMonth();
    monthLabel.textContent = new Intl.DateTimeFormat(undefined, {month: 'long', year: 'numeric'}).format(monthCursor);
    dayGrid.replaceChildren();
    for (let i = 0; i < new Date(year, month, 1).getDay(); i++) {
      const blank = document.createElement('span');
      blank.className = 'calendar-day-blank';
      blank.setAttribute('role', 'presentation');
      dayGrid.append(blank);
    }
    const count = new Date(year, month + 1, 0).getDate();
    for (let day = 1; day <= count; day++) {
      const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'calendar-day';
      button.textContent = String(day);
      button.disabled = !availableDates.has(iso);
      button.setAttribute('role', 'gridcell');
      button.setAttribute('aria-label', new Intl.DateTimeFormat(undefined, {dateStyle: 'full'}).format(new Date(`${iso}T12:00:00`)));
      if (iso === selectedISO) {
        button.classList.add('selected');
        button.setAttribute('aria-current', 'date');
      }
      if (!button.disabled) button.addEventListener('click', () => {
        const url = new URL(location.href);
        url.searchParams.set('date', iso);
        url.searchParams.delete('flight');
        location.href = url.toString();
      });
      dayGrid.append(button);
    }
  };
  renderCalendar();
  dateToggle?.addEventListener('click', () => {
    const expanded = dateToggle.getAttribute('aria-expanded') === 'true';
    dateToggle.setAttribute('aria-expanded', String(!expanded));
    if (calendar) calendar.hidden = expanded;
  });
  document.getElementById('calendar-prev')?.addEventListener('click', () => {
    monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() - 1, 1);
    renderCalendar();
  });
  document.getElementById('calendar-next')?.addEventListener('click', () => {
    monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() + 1, 1);
    renderCalendar();
  });
  document.addEventListener('click', (event) => {
    if (!dateRail?.contains(event.target) && dateToggle?.getAttribute('aria-expanded') === 'true') {
      dateToggle.setAttribute('aria-expanded', 'false');
      if (calendar) calendar.hidden = true;
    }
  });

  const libraryToggle = document.getElementById('flight-library-toggle');
  const workspace = document.querySelector('.viewer-workspace');
  libraryToggle?.addEventListener('click', () => {
    const expanded = libraryToggle.getAttribute('aria-expanded') === 'true';
    libraryToggle.setAttribute('aria-expanded', String(!expanded));
    workspace?.classList.toggle('library-collapsed', expanded);
    const label = libraryToggle.querySelector('span:first-child');
    if (label) label.textContent = expanded ? 'Expand' : 'Collapse';
  });

  const detail = document.getElementById('flight-detail-dialog');
  const closeButton = document.querySelector('.flight-detail-close');
  const closeDetail = () => {
    if (!detail) return;
    detail.hidden = true;
    document.body.classList.remove('viewer-detail-open');
    const url = new URL(location.href);
    url.searchParams.delete('flight');
    history.replaceState(null, '', url);
  };
  if (detail && new URLSearchParams(location.search).has('flight')) {
    detail.hidden = false;
    document.body.classList.add('viewer-detail-open');
  }
  closeButton?.addEventListener('click', closeDetail);
  detail?.addEventListener('click', (event) => {
    if (event.target === detail) closeDetail();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && dateToggle?.getAttribute('aria-expanded') === 'true') {
      dateToggle.setAttribute('aria-expanded', 'false');
      if (calendar) calendar.hidden = true;
    }
    if (event.key === 'Escape' && detail && !detail.hidden) closeDetail();
  });
})();
