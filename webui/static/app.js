/* Dashboard behaviour.

   Progress is rendered from the structured events the pipeline emits, not from log
   text. Raw log lines are still available under "Technical details" for when
   something goes wrong, but nobody has to read them to use this. */

function el(id) { return document.getElementById(id); }

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

function escapeHTML(s) {
  return String(s).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));
}

/* ------------------------------------------------------------------- theme */

function initTheme() {
  const buttons = document.querySelectorAll('[data-theme-set]');
  const current = localStorage.getItem('theme') || 'auto';

  /* Transitions are suspended across the swap. Without this, any property with a
     transition whose value comes from a themed custom property keeps its old
     colour instead of re-resolving — which left file buttons dark on a light page
     (and light on a dark one, depending which theme loaded first). Suspending also
     avoids every surface cross-fading at once, which looks like a glitch. */
  const apply = (mode) => {
    const root = document.documentElement;
    root.classList.add('no-transition');

    if (mode === 'auto') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    localStorage.setItem('theme', mode);
    buttons.forEach(b => b.classList.toggle('on', b.dataset.themeSet === mode));

    void root.offsetHeight;  // force the restyle before transitions come back
    requestAnimationFrame(() => {
      requestAnimationFrame(() => root.classList.remove('no-transition'));
    });
  };

  buttons.forEach(b => b.addEventListener('click', () => apply(b.dataset.themeSet)));
  apply(current);
}

/* ---------------------------------------------------------------- progress */

function currentMonth() {
  const sel = document.querySelector('select[name="month"]');
  return sel ? sel.value : null;
}

let pollTimer = null;

/* Kept in sync with _icons.html; inline so a result row needs no extra request. */
const ICONS = {
  done: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  attention: '<path d="M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z"/><path d="M12 9v4.5M12 17.2v.01"/>',
  failed: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
  skipped: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
};

function svg(tone) {
  return `<svg class="ico" width="17" height="17" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true">${ICONS[tone] || ICONS.skipped}</svg>`;
}

function renderResults(items) {
  const list = el('results');
  if (!items.length) { list.innerHTML = ''; return; }
  list.innerHTML = items.map(r => `
    <li class="result ${r.tone}">
      ${svg(r.tone)}
      <span class="who">${escapeHTML(r.client)}</span>
      <span class="what">${escapeHTML(r.label)}</span>
      ${r.created ? `<span class="made">${escapeHTML(r.created_kind)} created</span>` : ''}
      ${r.duration_label ? `<span class="dur">${escapeHTML(r.duration_label)}</span>` : ''}
      ${r.tokens_label ? `<span class="toks" title="Approximate tokens used for this paper">${escapeHTML(r.tokens_label)} tokens</span>` : ''}
      ${r.detail && r.tone !== 'done' ? `<span class="note">${escapeHTML(r.detail)}</span>` : ''}
    </li>`).join('');
}

function renderOutcomes(counts, running) {
  const box = el('outcomes');
  if (running) { box.hidden = true; return; }
  const parts = [
    ['done', counts.done, 'finished'],
    ['attention', counts.attention, 'need a look'],
    ['failed', counts.failed, 'could not finish'],
    ['skipped', counts.skipped, 'left alone'],
  ].filter(([, n]) => n > 0);
  if (!parts.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = parts.map(([tone, n, label]) =>
    `<div class="outcome ${tone}"><span class="n">${n}</span><span class="l">${label}</span></div>`).join('');
}

function renderStatus(s) {
  /* Absent for a writer login, which has no run controls or progress panel. */
  if (!el('progressWrap')) return;
  el('progressWrap').hidden = false;
  el('headline').textContent = s.headline || (s.running ? 'Working…' : 'Finished');
  el('stepLabel').textContent = s.running ? (s.step_label || '') : '';
  el('spinner').hidden = !s.running;
  const bitsCount = [];
  if (s.total) bitsCount.push(`${s.done} of ${s.total}`);
  if (s.elapsed_label) bitsCount.push(s.elapsed_label);
  el('progressCount').textContent = bitsCount.join(' · ');
  const rem = el('remaining');
  if (rem) {
    rem.hidden = !(s.running && s.remaining_label);
    rem.textContent = s.remaining_label ? `about ${s.remaining_label} left` : '';
  }

  // Token spend, live while running and as a total afterwards.
  const meter = el('tokenMeter');
  if (meter) {
    const bits = [];
    if (s.tokens_label) bits.push(`<strong>${escapeHTML(s.tokens_label)}</strong> tokens used${s.running ? ' so far' : ''}`);
    if (s.limit_used_percent !== null && s.limit_used_percent !== undefined) {
      bits.push(`<span class="lim">${Math.round(s.limit_used_percent)}% of your monthly allowance used</span>`);
    }
    meter.hidden = bits.length === 0;
    meter.innerHTML = bits.join('<span class="sep">·</span>');
  }
  const fill = el('progressFill');
  fill.style.width = s.running ? `${Math.max(s.percent, 4)}%` : '100%';
  fill.classList.toggle('failed', !s.running && !!s.error);
  fill.classList.toggle('done-state', !s.running);  // stop the shimmer when idle

  renderResults(s.results || []);
  renderOutcomes(s.outcomes || {}, s.running);

  const tech = el('techWrap');
  tech.hidden = !(s.log && s.log.length);
  if (s.log && s.log.length) {
    const box = el('logbox');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.innerHTML = s.log.map(l => {
      const cls = l.level === 'ERROR' ? 'err' : (l.level === 'WARNING' ? 'warn' : '');
      const text = escapeHTML(l.text);
      return cls ? `<span class="${cls}">${text}</span>` : text;
    }).join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  if (s.error) {
    el('headline').textContent = 'Something went wrong.';
    el('stepLabel').textContent = s.error;
  }

  const start = el('btnStart');
  if (start) {
    start.disabled = s.running;
    start.textContent = s.running ? 'Working…' : 'Start';
  }
}

/* Only the poll loop reloads the page, and only when a run finishes. Reloading
   from renderStatus would loop forever: the status after the reload still carries
   the same finished run. */
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    let s;
    try {
      s = await (await fetch('/api/status')).json();
    } catch (e) {
      return; // server busy; try again next tick
    }
    renderStatus(s);
    if (!s.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      setTimeout(() => window.location.reload(), 2500); // refresh the file list
    }
  }, 1500);
}

/* ------------------------------------------------------------------ wiring */

function initDashboard(isRunning) {
  const start = el('btnStart');
  if (start) {
    start.addEventListener('click', async () => {
      start.disabled = true;
      el('estimate').hidden = true;
      const res = await postJSON('/api/run', {
        phase: el('phase').value,
        month: currentMonth(),
        test_mode: el('testMode').checked,
        provider: el('mockMode').checked ? 'mock' : null,
      });
      if (!res.ok) { alert(res.message); start.disabled = false; return; }
      el('progressWrap').hidden = false;
      startPolling();
    });
  }

  const preview = el('btnPreview');
  if (preview) {
    preview.addEventListener('click', async () => {
      const month = currentMonth();
      const url = '/api/preview' + (month ? `?month=${encodeURIComponent(month)}` : '');
      const data = await (await fetch(url)).json();
      const box = el('estimate');
      box.hidden = false;
      if (data.error) { box.textContent = data.error; return; }
      const bits = [];
      if (data.would_review.length) bits.push(`check ${data.would_review.length} paper${data.would_review.length > 1 ? 's' : ''}`);
      if (data.would_revise.length) bits.push(`make ${data.would_revise.length} corrected cop${data.would_revise.length > 1 ? 'ies' : 'y'}`);
      if (!bits.length) {
        box.textContent = 'There is nothing waiting right now, so pressing Start would do nothing.';
        return;
      }
      const basis = data.estimate_measured
        ? `based on your last ${data.estimate_samples} papers`
        : 'a rough guess until a few papers have been timed';
      box.textContent = `If you press Start, it will ${bits.join(' and ')}. `
        + `That should take about ${data.estimate_label} — ${basis}. Nothing has been changed yet.`;
    });
  }

  document.querySelectorAll('button.filelink').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await postJSON('/api/open', { path: btn.dataset.path });
      if (!res.ok) alert(res.message);
    });
  });

  const sched = el('btnSchedule');
  const timeField = el('scheduleAt');
  const saveTime = el('btnSaveTime');

  /* The time field only takes effect once it is written to the task, so the Save
     button appears the moment the value differs from what is registered. Without
     it the only control is the on/off button, which would turn the schedule off
     rather than re-time it. */
  if (timeField && saveTime) {
    const sync = () => {
      const changed = timeField.value !== timeField.dataset.original;
      saveTime.hidden = !changed;
      el('scheduleMsg').textContent = changed
        ? 'Press "Save new time" to apply this.'
        : '';
    };
    timeField.addEventListener('input', sync);
    timeField.addEventListener('change', sync);

    saveTime.addEventListener('click', async () => {
      saveTime.disabled = true;
      el('scheduleMsg').textContent = 'Saving…';
      const res = await postJSON('/api/schedule', { enabled: true, at: timeField.value });
      el('scheduleMsg').textContent = res.ok
        ? `Saved. It will run every day at ${timeField.value}.`
        : res.message;
      saveTime.disabled = false;
      if (res.ok) {
        timeField.dataset.original = timeField.value;
        saveTime.hidden = true;
        setTimeout(() => window.location.reload(), 1100);
      }
    });
  }

  if (sched) {
    sched.addEventListener('click', async () => {
      const enable = sched.dataset.enabled !== '1';
      sched.disabled = true;
      el('scheduleMsg').textContent = enable ? 'Setting it up…' : 'Turning it off…';
      const res = await postJSON('/api/schedule', { enabled: enable, at: timeField.value });
      el('scheduleMsg').textContent = res.ok
        ? (enable ? `Done. It will run every day at ${timeField.value}.` : 'Turned off.')
        : res.message;
      sched.disabled = false;
      if (res.ok) setTimeout(() => window.location.reload(), 900);
    });
  }

  const addFolderForm = el('addFolderForm');
  if (addFolderForm) {
    addFolderForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = el('addFolderMsg');
      const submit = addFolderForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      msg.textContent = 'Creating…';
      const res = await postJSON('/api/create-folder', {
        month: el('folderMonth').value,
        employee: el('folderEmployee').value,
        client: el('folderClient').value,
      });
      msg.textContent = res.message;
      submit.disabled = false;
      if (res.ok) {
        el('folderEmployee').value = '';
        el('folderClient').value = '';
        setTimeout(() => window.location.reload(), 1100);
      }
    });
  }

  // Keep the last run's outcome visible across the post-run reload.
  fetch('/api/status')
    .then(r => r.json())
    .then(s => {
      if (s.running) startPolling();
      else if (s.results?.length || s.error) renderStatus(s);
    })
    .catch(() => { if (isRunning) startPolling(); });
}

initTheme();
