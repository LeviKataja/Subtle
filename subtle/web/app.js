'use strict';

const S = {
  cfg: {}, models: [], labels: {}, errors: [], keySource: 'none', keyHint: '', mock: false, home: '', version: '',
  jobs: new Map(), ticker: {}, open: null, detail: null, rowEls: new Map(), filter: 'all', query: '',
  ffmpeg: false, listen: {}, activity: [], bazarrKeySet: false, autoSpent: 0,
  arr: {}, arrResult: {},  // Sonarr/Radarr: what is saved, and what the last Connect reported
};
const SHORT = {
  failed: 'not translated', refused: 'declined', untranslated: 'same as source', english: 'English?',
  fast: 'too fast', long: 'long line', ratio: 'length', tags: 'tags', empty: 'no words',
};
const FIXABLE = ['failed', 'untranslated', 'english', 'empty', 'fast', 'long'];

// ── small helpers ──────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) node.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return node;
}

async function api(path, body, method) {
  const isBlob = body instanceof Blob;
  const res = await fetch(path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: { 'X-Subtle': '1', ...(body !== undefined && !isBlob ? { 'Content-Type': 'application/json' } : {}) },
    body: body === undefined ? undefined : isBlob ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText || 'Request failed');
  return data;
}

function toast(message, bad) {
  const node = el('div', { class: 'toast' + (bad ? ' bad' : ''), text: message });
  $('#toasts').append(node);
  setTimeout(() => node.remove(), bad ? 8000 : 4000);
}
const guard = fn => async (...args) => { try { return await fn(...args); } catch (e) { toast(e.message, true); } };

const money = v => !v ? '$0' : v < 0.01 ? '$' + v.toFixed(4) : v < 1 ? '$' + v.toFixed(3) : '$' + v.toFixed(2);
const tokens = n => n < 1000 ? String(n) : n < 1e6 ? (n / 1000).toFixed(n < 1e4 ? 1 : 0) + 'k' : (n / 1e6).toFixed(2) + 'M';
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
function clock(ms, full) {
  if (ms == null) return '';
  const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60, s = Math.floor(ms / 1000) % 60;
  const base = [h, m, s].map(x => String(x).padStart(2, '0')).join(':');
  return full ? base + ',' + String(ms % 1000).padStart(3, '0') : base;
}
const modelLabel = id => (S.models.find(m => m.id === id) || {}).label || id || '';

// ── state ──────────────────────────────────────────────────────────────────
function applyState(st) {
  Object.assign(S, {
    cfg: st.config, models: st.models, labels: st.labels, errors: st.errors, keySource: st.key_source,
    keyHint: st.key_hint, mock: st.mock, home: st.home, version: st.version,
    ffmpeg: st.ffmpeg, listen: st.listen, activity: st.activity, bazarrKeySet: st.bazarr_key_set,
    autoSpent: st.auto_spent_today, arr: st.arr || {},
  });
  S.jobs = new Map(st.jobs.map(j => [j.id, j]));
  $('#mockTag').hidden = !S.mock;
  $('#keyBanner').hidden = S.keySource !== 'none';
  renderControls(st.languages);
  renderQueue();
}

const saveCfg = guard(async patch => applyState(await api('/api/config', patch)));

function renderControls(languages) {
  if (languages) {
    $('#langs').replaceChildren(...languages.map(l => el('option', { value: l.name })));
  }
  const lang = $('#lang');
  if (document.activeElement !== lang) lang.value = S.cfg.target_name || '';
  lang.classList.toggle('need', !S.cfg.target_code);
  $('#models').replaceChildren(...S.models.map(m => el('button', {
    type: 'button', role: 'radio', 'aria-checked': String(m.id === S.cfg.model),
    title: `${m.blurb} $${m.inp.toFixed(2)} per million tokens in, $${m.out.toFixed(2)} out.`,
    onclick: () => saveCfg({ model: m.id }),
  }, m.label, el('small', { text: `$${m.inp.toFixed(2)} / $${m.out.toFixed(2)}` }))));
  for (const id of ['register', 'sdh', 'lyrics']) $('#' + id).value = S.cfg[id];
  if (document.activeElement !== $('#notes')) $('#notes').value = S.cfg.notes || '';
  const n = (S.cfg.glossary || []).length;
  $('#glossaryBtn').textContent = n ? `Glossary · ${n}` : 'Glossary';
}

// ── queue ──────────────────────────────────────────────────────────────────
const startable = j => ['ready', 'stopped', 'error'].includes(j.status);
const active = j => ['queued', 'running'].includes(j.status);

function renderQueue() {
  const jobs = [...S.jobs.values()].sort((a, b) => a.created - b.created);
  $('#queue').hidden = !jobs.length;
  $('#drop').classList.toggle('compact', jobs.length > 0);
  $('#jobs').replaceChildren(...jobs.map(jobRow));
  renderSummary();
}

function renderSummary() {
  const jobs = [...S.jobs.values()];
  const todo = jobs.filter(startable);
  const est = todo.reduce((sum, j) => sum + ((j.est || {}).cost || 0), 0);
  const done = jobs.filter(j => j.status === 'done').length;
  const running = jobs.filter(active).length;
  const bits = [plural(jobs.length, 'file')];
  if (running) bits.push(`${running} in progress`);
  if (done) bits.push(`${done} done`);
  $('#queueSummary').textContent = bits.join(' · ');
  const go = $('#goBtn');
  go.disabled = !todo.length || !S.cfg.target_code;
  go.textContent = todo.length ? `Translate ${plural(todo.length, 'file')} · about ${money(est)}` : 'Translate';
  go.title = S.cfg.target_code ? '' : 'Choose a language to translate into first';
  $('#zipBtn').hidden = done < 2;
  $('#clearBtn').hidden = !done;
  const spent = jobs.reduce((a, j) => ({ cost: a.cost + j.spent.cost, tok: a.tok + j.spent.inp + j.spent.out }), { cost: 0, tok: 0 });
  $('#spend').replaceChildren(...(spent.tok ? ['Spent ', el('b', { text: money(spent.cost) }), ` · ${tokens(spent.tok)} tokens`] : []));
}

function jobRow(j) {
  const [done, total] = j.progress;
  const pct = total ? Math.round(100 * done / total) : 0;
  const sub = el('div', { class: 'job-sub' });
  if (j.doing === 'proofread') sub.append(el('span', { class: 'live', text: 'Proofreading the whole translation…' }));
  else if (j.status === 'running' && S.ticker[j.id]) sub.append(el('span', { class: 'live', text: S.ticker[j.id] }));
  else if (j.error) sub.append(el('span', { class: 'bad', text: j.error }));
  else {
    const bits = [plural(j.cues, 'line')];
    if (j.via) bits.push(j.via);
    if (j.used.target_name) bits.push(`${j.used.target_name}, ${modelLabel(j.used.model)}`);
    sub.append(bits.join(' · '));
    if (j.status === 'done' && j.flagged) sub.append(' · ', el('span', { class: 'amber', text: `${j.flagged} to check` }));
    if (j.status === 'done' && j.proofed.model) sub.append(` · proofread, ${j.changed} changed`);
    if (j.saved_to) sub.append(' · saved next to the source');
  }

  let prog = '';
  if (active(j)) {
    prog = el('div', { class: 'job-prog' }, el('div', { class: 'bar' }, el('i', { style: `width:${pct}%` })),
      j.status === 'queued' ? 'Waiting…' : `${pct}%  ·  ${done} of ${total}`);
  } else if (j.status === 'done' && j.last.cached && j.last.cached === j.last.units) {
    prog = el('div', { class: 'job-prog', text: 'From cache' });
  }

  const cost = j.spent.requests
    ? el('div', { class: 'job-cost', title: `${j.spent.inp.toLocaleString()} tokens in, ${j.spent.out.toLocaleString()} out, ${plural(j.spent.requests, 'request')}` },
        money(j.spent.cost), el('small', { text: `${tokens(j.spent.inp + j.spent.out)} tokens` }))
    : el('div', { class: 'job-cost est', title: 'Estimate' }, startable(j) ? `≈ ${money((j.est || {}).cost || 0)}` : '');

  const act = (label, fn, cls) => el('button', { class: 'btn small ' + (cls || ''), text: label, onclick: e => { e.stopPropagation(); fn(); } });
  const actions = el('div', { class: 'job-actions' });
  if (startable(j)) actions.append(act(j.status === 'ready' ? 'Translate' : 'Resume', guard(() => api(`/api/jobs/${j.id}/start`, {}))));
  if (active(j)) actions.append(act('Stop', guard(() => api(`/api/jobs/${j.id}/cancel`, {}))));
  if (j.status === 'done') {
    actions.append(act('Review', () => { location.hash = '#/job/' + j.id; }));
    actions.append(act('Download', () => download(j), 'primary'));
  }
  if (!active(j)) actions.append(act('✕', guard(() => api(`/api/jobs/${j.id}`, undefined, 'DELETE')), 'ghost danger'));

  return el('div', {
    class: 'job' + (j.flagged ? ' warn' : ''), 'data-status': j.status, 'data-id': j.id, tabIndex: 0,
    onclick: () => { location.hash = '#/job/' + j.id; },
    onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) location.hash = '#/job/' + j.id; },
  }, el('span', { class: 'dot' }),
    el('div', {}, el('div', { class: 'job-name', text: j.name, title: j.src_path || j.name }), sub),
    prog || el('div'), cost, actions);
}

function upsertJob(j) {
  S.jobs.set(j.id, j);
  if (!active(j)) delete S.ticker[j.id];
  const old = $(`.job[data-id="${j.id}"]`);
  if (old) old.replaceWith(jobRow(j));
  else renderQueue();
  renderSummary();
  if (S.open === j.id) onOpenJobChanged(j);
}

function download(j) {
  location.href = `/api/jobs/${j.id}/download?bilingual=${S.cfg.bilingual ? 1 : 0}`;
}

const upload = guard(async files => {
  let added = 0;
  for (const file of files) {
    try {
      await api('/api/upload?name=' + encodeURIComponent(file.name), file);
      added++;
    } catch (e) { toast(`${file.name}: ${e.message}`, true); }
  }
  if (added && !S.cfg.target_code) $('#lang').focus();
});

// ── review ─────────────────────────────────────────────────────────────────
const openJob = guard(async id => {
  S.detail = await api('/api/jobs/' + id);
  S.open = id;
  S.jobs.set(id, S.detail);
  S.filter = 'all';
  S.query = $('#search').value = '';
  $('#home').hidden = true;
  $('#review').hidden = false;
  renderReview();
  window.scrollTo(0, 0);
});

function closeJob() {
  if (S.open) window.scrollTo(0, 0);  // the review may have been scrolled far down
  S.open = S.detail = null;
  $('#review').hidden = true;
  $('#home').hidden = false;
  renderQueue();
}

function onOpenJobChanged(j) {
  const before = S.detail.status, wasBusy = S.detail.busy;
  Object.assign(S.detail, j);
  if (before !== j.status || (wasBusy && !j.busy)) {
    const y = window.scrollY;  // flags only exist once a run ends: fetch the rows again
    api('/api/jobs/' + j.id).then(d => { if (S.open === j.id) { S.detail = d; renderReview(); window.scrollTo(0, y); } }).catch(() => {});
  } else renderReviewHead();
}

function renderReview() {
  renderReviewHead();
  S.rowEls = new Map();
  $('#rows').replaceChildren(...S.detail.rows.map(r => { const node = cueRow(r); S.rowEls.set(r.i, node); return node; }));
  applyFilter();
}

function renderReviewHead() {
  const d = S.detail, [done, total] = d.progress;
  $('#rvName').textContent = d.name;
  const chips = [el('span', { class: 'chip', text: plural(d.rows.length, 'line') })];
  const proofing = d.doing === 'proofread';
  if (proofing) chips.push(el('span', { class: 'chip green', text: 'Proofreading…' }));
  else if (d.status === 'running') chips.push(el('span', { class: 'chip green', text: `Translating… ${total ? Math.round(100 * done / total) : 0}%` }));
  if (d.status === 'queued') chips.push(el('span', { class: 'chip', text: 'Waiting in the queue' }));
  if (d.used.target_name) chips.push(el('span', { class: 'chip', text: `${d.used.target_name} · ${modelLabel(d.used.model)}` }));
  if (d.errors) chips.push(el('span', { class: 'chip red', text: `${d.errors} not translated` }));
  if (d.flagged - d.errors > 0) chips.push(el('span', { class: 'chip amber', text: `${d.flagged - d.errors} to check` }));
  if (d.spent.requests) chips.push(el('span', { class: 'chip', title: `${plural(d.spent.requests, 'request')}`, text: `${money(d.spent.cost)} · ${tokens(d.spent.inp + d.spent.out)} tokens` }));
  if (d.proofed.model && !proofing) {
    const p = d.proofed;
    chips.push(el('span', {
      class: 'chip', text: `Proofread by ${modelLabel(p.model)} · ${d.changed} changed`,
      title: `${plural(p.lines, 'line')} read for ${money(p.cost)}.` + (p.dropped ? ` ${plural(p.dropped, 'correction')} discarded for breaking a check.` : ''),
    }));
  }
  if (d.busy && !proofing) chips.push(el('span', { class: 'chip green', text: 'Reworking lines…' }));
  $('#rvChips').replaceChildren(...chips);

  const note = $('#rvNote');
  note.hidden = !(d.error || d.saved_to);
  note.className = 'rv-note' + (d.error ? ' bad' : '');
  note.textContent = d.error || (d.saved_to ? `Saved to ${d.saved_to}. Edits you make here are written to that file too.` : '');

  const btn = (label, fn, cls, title) => el('button', { class: 'btn ' + (cls || ''), text: label, title, onclick: guard(fn) });
  const actions = [];
  if (startable(d)) actions.push(btn(d.status === 'ready' ? 'Translate' : 'Resume', () => api(`/api/jobs/${d.id}/start`, {}), 'primary'));
  if (active(d)) actions.push(btn('Stop', () => api(`/api/jobs/${d.id}/cancel`, {})));
  if (d.status === 'done') {
    const fixable = d.rows.filter(r => !r.ed && r.flags.some(f => FIXABLE.includes(f))).length;
    if (fixable && !d.busy) actions.push(btn(`Fix ${plural(fixable, 'flagged line')}`, fixFlagged, '', 'Sends only these lines back: untranslated ones are redone, long or fast ones are shortened'));
    if (S.filter === 'proofed' && d.changed && !d.busy) actions.push(btn(`Undo all ${d.changed}`, undoProofread, '', 'Put back the wording from before proofreading on every line'));
    if (!d.busy) actions.push(el('button', {
      class: 'btn', text: d.proofed.model ? 'Proofread again' : 'Proofread', 'aria-haspopup': 'menu',
      title: 'Read the whole translation once more and correct only the lines that need it',
      onclick: e => { e.stopPropagation(); proofMenu(e.currentTarget); },
    }));
    actions.push(btn('Start over', startOver, 'ghost', 'Translate the whole file again with the current settings'));
    actions.push(btn('Download', () => download(d), 'primary'));
  }
  $('#rvActions').replaceChildren(...actions);
  $('#followWrap').hidden = d.status !== 'running';

  const count = f => d.rows.filter(r => matches(r, f, '')).length;
  const tabs = [['all', 'All'], ['flagged', 'To check'], ['edited', 'Edited']];
  if (d.proofed.model || d.changed) tabs.splice(2, 0, ['proofed', 'Proofread']);
  else if (S.filter === 'proofed') S.filter = 'all';
  $('#rvTabs').replaceChildren(...tabs.map(([key, label]) =>
    el('button', { role: 'tab', 'aria-selected': String(S.filter === key), onclick: () => { S.filter = key; renderReviewHead(); applyFilter(); } },
      label, el('span', { text: count(key) }))));
}

async function fixFlagged() {
  const r = await api(`/api/jobs/${S.detail.id}/fix`, {});
  toast(`Reworking ${plural(r.again + r.shorter, 'line')}…`);
}

// How well a model suits proofreading what another one translated. A reader corrects what it can
// tell is wrong, so the useful direction is a stronger model reading a weaker one's work.
function readerFit(reader, translator) {
  const rank = id => [...S.models].sort((a, b) => a.inp - b.inp).findIndex(m => m.id === id);
  const r = rank(reader), t = rank(translator);
  if (r < 0 || t < 0) return { kind: '', note: '' };
  const top = t === S.models.length - 1;
  if (r < t) return { kind: 'weak', note: 'Weaker than the model that translated this file: it can undo good lines. Not recommended.' };
  if (r === t) return {
    kind: top ? 'best' : '',
    note: 'The model that translated this file, reading its own work: catches slips and inconsistency, tends to miss what it misunderstood.',
  };
  if (r === t + 1) return { kind: 'best', note: 'A stronger reader also catches what the translator misunderstood.' };
  return { kind: '', note: 'Stronger still, at a higher price.' };
}

function proofMenu(anchor) {
  const open = $('.menu');
  if (open) return open.remove();
  const d = S.detail, est = d.proof_est || {};
  const close = e => { if (!e || !menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close); } };
  const pick = guard(async id => {
    close();
    await api(`/api/jobs/${d.id}/proofread`, { model: id });
  });
  const menu = el('div', { class: 'menu', role: 'menu', onkeydown: e => { if (e.key === 'Escape') { close(); anchor.focus(); } } },
    el('div', { class: 'menu-head', text: 'Which model should read it?' }),
    S.models.map(m => {
      const fit = readerFit(m.id, d.used.model);
      return el('button', { type: 'button', role: 'menuitem', class: fit.kind, onclick: () => pick(m.id) },
        el('div', { class: 'menu-row' },
          el('span', {}, m.label, fit.kind === 'best' && el('b', { text: ' · recommended' })),
          el('small', { text: `about ${money(est[m.id] || 0)}` })),
        fit.note && el('div', { class: 'menu-note', text: fit.note }));
    }));
  anchor.after(menu);
  $('button', menu).focus();
  document.addEventListener('click', close);
}

async function undoProofread() {
  const d = S.detail;
  const r = await api(`/api/jobs/${d.id}/unproof`, {});
  S.detail = await api('/api/jobs/' + d.id);
  renderReview();
  toast(`Put back ${plural(r.restored, 'line')}.`);
}

async function startOver() {
  const d = S.detail;
  if (d.edited && !confirm(`This replaces your ${plural(d.edited, 'edit')} in this file. Continue?`)) return;
  await api(`/api/jobs/${d.id}/reset`, {});
  await api(`/api/jobs/${d.id}/start`, {});
}

function matches(r, filter, query) {
  if (filter === 'flagged' && !r.flags.length) return false;
  if (filter === 'edited' && !r.ed) return false;
  if (filter === 'proofed' && r.pv == null) return false;
  if (!query) return true;
  return r.src.toLowerCase().includes(query) || (r.tr || '').toLowerCase().includes(query);
}

function applyFilter() {
  const q = S.query.trim().toLowerCase();
  let shown = 0;
  for (const r of S.detail.rows) {
    const ok = matches(r, S.filter, q);
    S.rowEls.get(r.i).hidden = !ok;
    shown += ok;
  }
  $('#rowsEmpty').hidden = shown > 0;
  $('#rows').hidden = shown === 0;
}

function cueRow(r) {
  const editable = !active(S.detail) && r.tr != null;
  const cell = el('div', { class: 'tr', spellcheck: false, text: r.tr ?? '' });
  if (editable) {
    try { cell.contentEditable = 'plaintext-only'; } catch (e) { cell.contentEditable = 'true'; }
    cell.addEventListener('keydown', e => {
      if (e.key === 'Escape') { cell.textContent = r.tr ?? ''; cell.blur(); }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        let next = cell.closest('.row').nextElementSibling;
        while (next && (next.hidden || !$('.tr[contenteditable]', next))) next = next.nextElementSibling;
        next ? $('.tr', next).focus() : cell.blur();
      }
    });
    cell.addEventListener('blur', () => saveEdit(r, cell));
  }
  const meta = el('div', { class: 'meta' });
  fillMeta(meta, r);
  const dur = r.s != null ? `${clock(r.s, true)} → ${clock(r.e, true)}  (${((r.e - r.s) / 1000).toFixed(1)} s)` : '';
  const row = el('div', { class: rowClass(r), 'data-i': r.i },
    el('div', { class: 'n', text: r.i + 1 }), el('div', { class: 't', text: clock(r.s), title: dur }),
    el('div', { class: 'src', text: r.src }), cell, meta,
    r.pv != null && el('div', { class: 'was', text: r.pv, title: 'The wording before proofreading' }));
  return row;
}

function rowClass(r) {
  const err = r.flags.some(f => S.errors.includes(f));
  return 'row' + (r.tr == null ? ' pending' : '') + (err ? ' err' : r.flags.length ? ' flag' : '');
}

function fillMeta(meta, r) {
  const kids = [];
  if (r.cps != null) kids.push(el('span', { class: 'cps' + (r.flags.includes('fast') ? ' over' : ''), text: `${Math.round(r.cps)} cps`, title: 'Characters per second' }));
  if (r.ed) kids.push(el('span', { class: 'badge plain', text: 'edited' }));
  if (r.pv != null) {
    kids.push(el('span', { class: 'badge plain', text: 'proofread' }));
    if (!active(S.detail) && !S.detail.busy) kids.push(el('button', {
      class: 'redo', text: '↶', title: 'Put back the earlier wording', 'aria-label': 'Put back the earlier wording',
      onclick: guard(async () => updateRow(await api(`/api/jobs/${S.detail.id}/unproof/${r.i}`, {}))),
    }));
  }
  for (const f of r.flags) kids.push(el('span', { class: 'badge' + (S.errors.includes(f) ? ' red' : ''), text: SHORT[f] || f, title: S.labels[f] || '' }));
  if (!active(S.detail) && r.tr != null && S.detail.status !== 'ready') {
    kids.push(el('button', { class: 'redo', text: '↻', title: 'Translate this line again', 'aria-label': 'Translate this line again', onclick: () => toggleRedo(r) }));
  }
  meta.replaceChildren(...kids);
}

function updateRow(r) {
  const i = S.detail.rows.findIndex(x => x.i === r.i);
  if (i < 0) return;
  S.detail.rows[i] = r;
  const fresh = cueRow(r);
  const old = S.rowEls.get(r.i);
  fresh.hidden = old.hidden;  // a line you just fixed stays put until the filter is applied again
  old.replaceWith(fresh);
  S.rowEls.set(r.i, fresh);
  renderReviewHead();
  return fresh;
}

const saveEdit = guard(async (r, cell) => {
  const text = cell.innerText.replace(/\n+$/, '');
  if (text === (r.tr ?? '')) return;
  try {
    updateRow(await api(`/api/jobs/${S.detail.id}/cue/${r.i}`, { text }));
  } catch (e) { cell.textContent = r.tr ?? ''; throw e; }
});

function toggleRedo(r) {
  const row = S.rowEls.get(r.i), open = $('.redo-form', row);
  if (open) return open.remove();
  document.querySelectorAll('.redo-form').forEach(n => n.remove());
  const input = el('input', { placeholder: 'Anything to change?  e.g. “more casual”, “she is talking to a child”  (optional)', maxLength: 300 });
  const send = guard(async condense => {
    row.classList.add('busy');
    form.remove();
    try { await api(`/api/jobs/${S.detail.id}/retranslate`, { cues: [r.i], instruction: input.value, condense }); }
    catch (e) { row.classList.remove('busy'); throw e; }
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); send(false); }
    if (e.key === 'Escape') form.remove();
  });
  const form = el('div', { class: 'redo-form' }, input,
    el('button', { class: 'btn small', type: 'button', text: 'Shorter', onclick: () => send(true) }),
    el('button', { class: 'btn small primary', type: 'button', text: 'Translate again', onclick: () => send(false) }));
  row.append(form);
  input.focus();
}

// ── library ────────────────────────────────────────────────────────────────
// What Sonarr and Radarr have, and what each video has by way of subtitles. `open` holds the
// episodes of the shows that are unfolded (null while they load).
const L = { data: null, tab: 'shows', query: '', open: new Map(), loading: false };
const VIDEO_STATE = {
  have: ['', 'plain'], ours: ['translated by Subtle', 'green'], missing: ['missing', ''],
  nosource: ['nothing to translate from', 'plain'], gone: ['file not found', 'red'],
};

function stateBadge(v) {
  const [label, cls] = VIDEO_STATE[v.state] || [v.state, 'plain'];
  let title = v.note || '';
  if (v.made) {
    const m = v.made;
    title = modelLabel(m.model) + (m.reader ? `, proofread by ${modelLabel(m.reader)}` : '') +
      ` · ${money(m.cost)} · ${plural(m.lines, 'line')}` + (m.flagged ? `, ${m.flagged} to check` : '');
  }
  return el('span', { class: 'badge ' + cls, text: v.state === 'have' ? `${L.data.target} subtitles` : label, title });
}

const loadShow = guard(async id => {
  if (!L.open.has(id)) L.open.set(id, null);
  renderLibrary();
  try {
    L.open.set(id, (await api('/api/library/show/' + id)).episodes);
  } catch (e) { L.open.delete(id); throw e; } finally { renderLibrary(); }
});

const loadLibrary = guard(async () => {
  L.loading = true;
  renderLibrary();
  try {
    L.data = await api('/api/library');
    for (const id of L.open.keys()) loadShow(id);  // what is unfolded may have changed too
  } finally { L.loading = false; renderLibrary(); }
});

function openLibrary() {
  S.open = S.detail = null;
  $('#home').hidden = $('#review').hidden = true;
  $('#library').hidden = false;
  window.scrollTo(0, 0);
  loadLibrary();
}

const setAuto = guard(async patch => {
  const r = await api('/api/library/auto', patch);
  Object.assign(S.cfg, r);
  if (L.data) {
    Object.assign(L.data, { auto_all: r.auto_all, auto_movies: r.auto_movies });
    for (const s of L.data.sonarr.items) s.auto = r.auto_series.includes(s.id);
  }
  renderLibrary();
});

const queueMissing = guard(async (kind, ids, series) => {
  await api('/api/library/queue', { kind, ids, series });
  toast(`Looking for subtitles in ${plural(ids.length, 'video')}…`);
});

function autoSwitch(label, on, change, disabled, title) {
  const box = el('input', { type: 'checkbox', checked: on, disabled, onchange: e => change(e.target.checked) });
  return el('label', { class: 'check lib-auto', title, onclick: e => e.stopPropagation() }, box, label);
}

function addButton(kind, missing, series) {
  return el('button', {
    class: 'btn small', text: `Add ${missing.length} missing to the queue`,
    title: 'Puts them in the queue with a cost estimate each. Nothing is spent until you press Translate there.',
    onclick: e => { e.stopPropagation(); queueMissing(kind, missing.map(v => v.id), series); },
  });
}

function showRows(show) {
  const unfolded = L.open.has(show.id), eps = L.open.get(show.id);
  const head = el('div', {
    class: 'lib-row show', tabIndex: 0, role: 'button', 'aria-expanded': String(unfolded),
    onclick: () => { unfolded ? L.open.delete(show.id) : loadShow(show.id); if (unfolded) renderLibrary(); },
    onkeydown: e => { if (e.key === 'Enter' && e.target === e.currentTarget) e.currentTarget.click(); },
  }, el('span', { class: 'chev', text: '›' }),
    el('div', { class: 'lib-title' }, show.title, show.year && el('small', { text: show.year })),
    el('span', { class: 'lib-count', text: plural(show.files, 'episode') }),
    autoSwitch('New episodes', show.auto || L.data.auto_all, on => setAuto({ series: show.id, on }), L.data.auto_all,
      L.data.auto_all ? 'On for every show (the switch above the list)' : 'Translate new episodes of this show as they arrive'));
  if (!unfolded) return [head];
  if (!eps) return [head, el('div', { class: 'lib-eps' }, el('div', { class: 'lib-sum', text: 'Looking at the files…' }))];
  const done = eps.filter(e => e.state === 'have' || e.state === 'ours').length;
  const missing = eps.filter(e => e.state === 'missing');
  const box = el('div', { class: 'lib-eps' }, el('div', { class: 'lib-sum' },
    `${done} of ${eps.length} have ${L.data.target} subtitles` + (missing.length ? ` · ${missing.length} missing` : ''),
    missing.length > 0 && addButton('sonarr', missing, show.id)));
  let season = null;
  for (const e of eps) {
    if (e.season !== season) {
      season = e.season;
      box.append(el('div', { class: 'lib-season', text: season ? `Season ${season}` : 'Specials' }));
    }
    box.append(el('div', { class: 'lib-ep' },
      el('span', { class: 'num', text: 'E' + String(e.number).padStart(2, '0') }),
      el('span', { class: 'what' }, e.title, ['nosource', 'gone'].includes(e.state) && el('span', { class: 'why', text: e.note })),
      stateBadge(e)));
  }
  return [head, box];
}

function renderLibrary() {
  const d = L.data, note = $('#libNote'), body = $('#libBody'), tools = $('#libTools');
  $('#libSummary').textContent = L.loading ? 'Asking Sonarr and Radarr…' : '';
  if (!d) {
    note.hidden = true;
    return [$('#libTabs'), tools, body].forEach(n => n.replaceChildren());
  }
  const shows = L.tab === 'shows', part = shows ? d.sonarr : d.radarr, app = shows ? 'Sonarr' : 'Radarr';
  $('#libTabs').replaceChildren(...[['shows', 'Shows', d.sonarr], ['movies', 'Movies', d.radarr]].map(([key, label, p]) =>
    el('button', { role: 'tab', 'aria-selected': String(L.tab === key), onclick: () => { L.tab = key; renderLibrary(); } },
      label, el('span', { text: p.items.length }))));

  const message = !d.target ? 'Choose a language to translate into on the main page first.'
    : !part.connected ? `${app} is not connected, so there is nothing to list here.` : part.error;
  note.hidden = !message;
  note.className = 'rv-note' + (part.connected && part.error ? ' bad' : '');
  note.replaceChildren(message || '', ' ', d.target && !part.connected
    ? el('button', { class: 'btn small', text: `Connect ${app}…`, onclick: openSetup }) : '');

  const items = part.items.filter(i => !L.query || i.title.toLowerCase().includes(L.query));
  tools.replaceChildren();
  if (part.connected && !part.error) {
    const missing = shows ? [] : items.filter(m => m.state === 'missing');
    if (missing.length) tools.append(addButton('radarr', missing));
    tools.append(shows
      ? autoSwitch('Translate new episodes of every show', d.auto_all, on => setAuto({ all: on }), false,
          'Otherwise only the shows switched on below are translated as episodes arrive')
      : autoSwitch('Translate new movies', d.auto_movies, on => setAuto({ movies: on }), false,
          'Translate each movie as it arrives, unless it already has subtitles'));
  }
  const rows = shows ? items.flatMap(showRows) : items.map(m => el('div', { class: 'lib-row' },
    el('span'), el('div', { class: 'lib-title' }, m.title, m.year && el('small', { text: m.year }),
      ['nosource', 'gone'].includes(m.state) && el('span', { class: 'why', text: m.note })),
    el('span'), m.state ? stateBadge(m) : el('span')));
  if (!rows.length && part.connected && !part.error) {
    rows.push(el('div', { class: 'lib-empty', text: L.query ? 'Nothing matches.' : `${app} lists nothing with a file yet.` }));
  }
  body.hidden = !rows.length;
  body.replaceChildren(...rows);
}

// ── live events ────────────────────────────────────────────────────────────
function onEvent(ev) {
  if (ev.type === 'job') return upsertJob(ev.job);
  if (ev.type === 'library') {
    toast(ev.added ? `${plural(ev.added, 'file')} added to the queue, ready to translate.` : 'Nothing was added to the queue.');
    if (ev.skipped.length) {
      toast(`Left out ${ev.skipped.length}: ` + ev.skipped.slice(0, 3).map(s => (s.file ? s.file + ': ' : '') + s.why).join(' · '), true);
    }
    return;
  }
  if (ev.type === 'activity') {
    S.activity = ev.list;
    S.autoSpent = ev.spent;
    if ($('#autoDlg').open) renderActivity();
    return;
  }
  if (ev.type === 'removed') {
    S.jobs.delete(ev.id);
    if (S.open === ev.id) location.hash = '#/';
    return renderQueue();
  }
  if (ev.type === 'cue') {
    if (ev.row) { if (S.open === ev.job) updateRow(ev.row)?.classList.add('fresh'); return; }
    S.ticker[ev.job] = ev.text.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ');
    if (S.open !== ev.job) {
      const sub = $(`.job[data-id="${ev.job}"] .job-sub`);
      if (sub) sub.replaceChildren(el('span', { class: 'live', text: S.ticker[ev.job] }));
      return;
    }
    const r = S.detail.rows.find(x => x.i === ev.i), row = S.rowEls.get(ev.i);
    if (!r || !row) return;
    r.tr = ev.text;
    $('.tr', row).textContent = ev.text;
    row.classList.remove('pending');
    row.classList.add('fresh');
    if ($('#follow').checked && !row.hidden) row.scrollIntoView({ block: 'center' });
    return;
  }
  if (ev.type === 'reworked' && S.open === ev.id) {
    for (const i of ev.cues) S.rowEls.get(i)?.classList.remove('busy');
    if (ev.error) toast(ev.error, true);
  }
  if (ev.type === 'proofread' && S.open === ev.id) {
    if (ev.error) return toast(ev.error, true);
    toast(ev.changed ? `Proofreading changed ${plural(ev.changed, 'line')}. They are under the Proofread tab.`
      : 'Proofreading found nothing to change.');
  }
}

function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = e => { for (const ev of JSON.parse(e.data)) { try { onEvent(ev); } catch (err) { console.error(err); } } };
  let lost = false;
  es.onerror = () => { lost = true; };
  es.onopen = guard(async () => {
    if (!lost) return;
    lost = false;  // events were missed while disconnected: resync
    applyState(await api('/api/state'));
    if (S.open && S.jobs.has(S.open)) openJob(S.open);
  });
}

// ── dialogs ────────────────────────────────────────────────────────────────
function openSettings() {
  const c = S.cfg;
  $('#keyStatus').textContent = {
    env: 'Using the key in the ANTHROPIC_API_KEY environment variable.',
    saved: `A key is saved (${S.keyHint}).`, mock: 'Demo mode: no key is needed and nothing is sent anywhere.',
    none: 'No key yet. Create one at console.anthropic.com and paste it here.',
  }[S.keySource];
  $('#keyRow').hidden = S.keySource === 'env' || S.keySource === 'mock';
  $('#keyRemove').hidden = S.keySource !== 'saved';
  $('#apiKey').value = '';
  $('#budget').value = c.budget;
  $('#escalate').value = c.escalate_model;
  $('#chunk').value = c.chunk_lines;
  $('#conc').value = c.concurrency;
  $('#maxChars').value = c.max_chars;
  $('#maxCps').value = c.max_cps;
  $('#saveNext').checked = c.save_next_to_source;
  $('#bilingual').checked = c.bilingual;
  $('#proofAuto').checked = c.proofread;
  $('#proofModel').replaceChildren(el('option', { value: '', text: 'The model that translated the file' }),
    ...S.models.map(m => el('option', { value: m.id, text: m.label })));
  $('#proofModel').value = c.proof_model;
  $('#about').textContent = `Subtle ${S.version} · settings, cache and queue live in ${S.home}`;
  $('#settingsDlg').showModal();
}

function openGlossary() {
  const rows = $('#glRows');
  const add = (a = '', b = '') => {
    const row = el('div', { class: 'gl-row' }, el('input', { value: a, placeholder: 'e.g. the Night Watch' }), el('input', { value: b, placeholder: 'how it should read in the translation' }),
      el('button', { class: 'btn ghost small danger', type: 'button', text: '✕', 'aria-label': 'Remove', onclick: () => row.remove() }));
    rows.append(row);
    return row;
  };
  rows.replaceChildren();
  (S.cfg.glossary || []).forEach(([a, b]) => add(a, b));
  if (!rows.children.length) add();
  $('#glAdd').onclick = () => $('input', add()).focus();
  $('#glSave').onclick = async () => {
    const glossary = [...rows.children].map(r => [...r.querySelectorAll('input')].map(i => i.value.trim())).filter(([a, b]) => a && b);
    await saveCfg({ glossary });
    $('#glossaryDlg').close();
  };
  $('#glossaryDlg').showModal();
}

function openFolder() {
  const list = $('#folderList'), addBtn = $('#folderAdd');
  $('#folderExample').textContent = `name.${S.cfg.target_code || 'xx'}.srt`;
  list.replaceChildren();
  addBtn.disabled = true;
  const refresh = () => {
    const n = list.querySelectorAll('input:checked').length;
    addBtn.disabled = !n;
    addBtn.textContent = n ? `Add ${plural(n, 'file')}` : 'Add';
  };
  const scan = guard(async () => {
    const path = $('#folderPath').value.trim();
    if (!path) return;
    list.replaceChildren(el('div', { class: 'msg', text: 'Looking…' }));
    let found;
    try { found = await api('/api/scan', { path }); } catch (e) { list.replaceChildren(el('div', { class: 'msg', text: e.message })); return; }
    const skipped = found.videos_skipped
      ? el('div', { class: 'msg', text: `${plural(found.videos_skipped, 'video file')} without a subtitle file of their own were left out: reading subtitles from inside a video needs ffmpeg, which is not installed.` })
      : null;
    if (!found.files.length) {
      list.replaceChildren(skipped || el('div', { class: 'msg', text: 'No subtitle files in there.' }));
      return refresh();
    }
    const note = { done: 'already translated', queued: 'in the queue', other: 'another language?', new: '', video: '', nosub: '' };
    list.replaceChildren(...found.files.map(f => {
      const usable = !['queued', 'nosub'].includes(f.state);
      return el('label', { title: f.note ? `${f.path}\n${f.note}` : f.path, class: usable ? '' : 'off' },
        el('input', { type: 'checkbox', value: f.path, checked: ['new', 'video'].includes(f.state), disabled: !usable, onchange: refresh }),
        el('span', { class: 'fname', text: f.name }),
        el('span', { class: f.note ? 'fnote' : 'fstate', text: f.note || note[f.state] }));
    }), ...(skipped ? [skipped] : []));
    refresh();
  });
  $('#folderScan').onclick = scan;
  $('#folderPath').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); scan(); } };
  addBtn.onclick = guard(async () => {
    const paths = [...list.querySelectorAll('input:checked')].map(i => i.value);
    addBtn.disabled = true;
    addBtn.textContent = 'Reading…';  // pulling a track out of a large video takes a moment
    let r;
    try { r = await api('/api/add-paths', { paths }); } finally { refresh(); }
    toast(`Added ${plural(r.added.length, 'file')}.`);
    for (const f of r.failed) toast(`${f.name}: ${f.error}`, true);
    $('#folderDlg').close();
  });
  $('#folderDlg').showModal();
  $('#folderPath').focus();
}

function renderActivity() {
  const words = {
    translated: ['Translated', 'good'], bazarr: ['Bazarr had it', 'good'], skipped: ['Skipped', ''], error: ['Failed', 'bad'],
    translating: ['Translating…', ''], waiting: ['Waiting', ''], checking: ['Checking…', ''], test: ['Test', 'good'],
  };
  const list = [...S.activity].reverse();
  $('#activity').replaceChildren(...(list.length ? list.map(a => {
    const [label, tone] = words[a.status] || [a.status, ''];
    const when = new Date(a.t * 1000);
    const today = when.toDateString() === new Date().toDateString();
    return el('div', { class: 'act' },
      el('span', { class: 'when', title: when.toLocaleString(), text: today ? when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : when.toLocaleDateString([], { day: 'numeric', month: 'short' }) }),
      el('span', { class: 'what ' + tone, text: label }),
      el('span', { class: 'file', title: a.file, text: `${a.origin}${a.file ? ' · ' + a.file : ''}` }),
      el('span', { class: 'note', text: [a.note, a.cost ? money(a.cost) : ''].filter(Boolean).join(' ') }));
  }) : [el('div', { class: 'msg', text: 'Nothing yet. Imports reported by Sonarr and Radarr will be listed here.' })]));
  const limit = S.cfg.auto_daily_budget;
  $('#autoSpent').textContent = `${money(S.autoSpent)} used today` + (limit ? '' : ' · 0 means no limit');
}

function openAutomation() {
  const c = S.cfg, local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const status = (ok, text) => el('li', { class: ok ? '' : 'off', text });
  $('#autoStatus').replaceChildren(
    status(!!c.target_code, c.target_code
      ? `New downloads are translated into ${c.target_name} with ${modelLabel(c.model)}.`
      : 'Choose a language on the main page first; until then imports are skipped.'),
    status(S.keySource !== 'none', S.keySource !== 'none' ? 'An API key is set.' : 'No API key yet (Settings).'),
    status(S.ffmpeg, S.ffmpeg ? 'ffmpeg found: subtitles inside video files can be read.'
      : 'ffmpeg not found: only videos with a subtitle file next to them can be translated.'),
    status(S.listen.open, S.listen.open ? `Listening on the network, port ${S.listen.port}.`
      : 'Only reachable from this computer. Start Subtle with --host 0.0.0.0 so Sonarr and Radarr on other machines can reach it.'));
  for (const kind of ['Sonarr', 'Radarr']) {
    $('#hook' + kind).value = `${location.origin}/webhook/${kind.toLowerCase()}?secret=${c.webhook_secret}`;
  }
  $('#hookHint').hidden = !local;
  $('#autoArr').replaceChildren(...Object.keys(ARR).map(kind => {
    const saved = S.arr[kind] || {};
    return status(saved.key_set, saved.key_set ? `${ARR[kind].name} is connected (${saved.url}).` : `${ARR[kind].name} is not connected.`);
  }));
  $('#autoConnect').onclick = () => { $('#autoDlg').close(); openSetup(); };

  const rows = $('#mapRows');
  const add = (a = '', b = '') => {
    const row = el('div', { class: 'gl-row' }, el('input', { value: a, placeholder: '/tv', spellcheck: false }),
      el('input', { value: b, placeholder: 'D:\\Media\\TV', spellcheck: false }),
      el('button', { class: 'btn ghost small danger', type: 'button', text: '✕', 'aria-label': 'Remove', onclick: () => row.remove() }));
    rows.append(row);
    return row;
  };
  rows.replaceChildren();
  (c.path_maps || []).forEach(([a, b]) => add(a, b));
  $('#mapAdd').onclick = () => $('input', add()).focus();

  $('#bzUrl').value = c.bazarr_url || '';
  $('#bzKey').value = '';
  $('#bzKey').placeholder = S.bazarrKeySet ? 'saved — type to replace' : '';
  $('#bzWait').value = c.bazarr_wait;
  $('#autoBudget').value = c.auto_daily_budget;
  $('#bzTest').onclick = guard(async () => {
    const r = await api('/api/bazarr/test', { url: $('#bzUrl').value.trim(), key: $('#bzKey').value.trim() });
    toast(`Bazarr answered${r.version ? ' (version ' + r.version + ')' : ''}.`);
  });
  renderActivity();
  $('#autoDlg').showModal();
}

// ── setup: key, language, Sonarr and Radarr ────────────────────────────────
const ARR = {
  sonarr: { name: 'Sonarr', example: 'http://192.168.1.20:8989', thing: 'show' },
  radarr: { name: 'Radarr', example: 'http://192.168.1.20:7878', thing: 'movie' },
};

function renderSetupKey() {
  $('#suKeyStatus').textContent = {
    env: 'Using the key in the ANTHROPIC_API_KEY environment variable.',
    saved: `A key is saved (${S.keyHint}). Paste another one to replace it.`,
    mock: 'Demo mode: no key is needed and nothing is sent anywhere.',
    none: 'Create one at console.anthropic.com and paste it here. It is kept in a settings file on this computer.',
  }[S.keySource];
  $('#suKeyRow').hidden = S.keySource === 'env' || S.keySource === 'mock';
  $('#suKey').value = '';
}

function showReachField() {
  $('#suReachRow').hidden = !S.cfg.reach_url && !Object.values(S.arrResult).some(r => r && !r.hook.ok);
}

function arrLines(kind, res) {
  const { name, thing } = ARR[kind];
  const lines = [el('li', { text: `Connected to ${res.app} ${res.version}.` })];
  for (const f of res.folders) {
    const checked = f.total ? ` (${f.found} of ${f.total} ${thing} folders found there)` : '';
    if (f.state === 'same') lines.push(el('li', { text: `Library folder ${f.remote} can be opened from this machine${checked}.` }));
    else if (f.state === 'mapped') lines.push(el('li', { text: `Library folder ${f.remote} is ${f.local} on this machine${checked}.` }));
    else {
      lines.push(el('li', { class: 'off' }, el('div', { class: 'ask' },
        el('span', { text: f.error || `Library folder ${f.remote} is not at that path on this machine. Where is it here?` }),
        el('input', { class: 'arr-local', 'data-remote': f.remote, value: f.offered || '', placeholder: 'For example D:\\Media\\TV or /mnt/media/tv',
          spellcheck: false, 'aria-label': `Where ${f.remote} is on this machine` }))));
    }
  }
  if (!res.folders.length) lines.push(el('li', { class: 'off', text: `${name} has no library folders yet.` }));
  const how = { added: 'was added', updated: 'was updated', kept: 'is in place' }[res.hook.state];
  lines.push(res.hook.ok
    ? el('li', { text: `The webhook ${how} in ${name}, and its test reached Subtle at ${res.hook.url}. New imports will be translated.` })
    : el('li', { class: 'off', text: res.hook.error }));
  return lines;
}

function arrBlock(kind) {
  const { name, example } = ARR[kind], saved = S.arr[kind] || {}, res = S.arrResult[kind];
  const label = saved.key_set ? 'Check again' : 'Connect';
  const url = el('input', { class: 'arr-url', value: saved.url || '', placeholder: example, spellcheck: false, 'aria-label': `${name} address` });
  const key = el('input', { class: 'arr-key', type: 'password', autocomplete: 'off', spellcheck: false,
    placeholder: saved.key_set ? 'API key saved. Type to replace' : 'API key', 'aria-label': `${name} API key` });
  const go = el('button', { class: 'btn arr-go', type: 'button', text: label });
  const block = el('div', { class: 'arr', 'data-kind': kind },
    el('div', { class: 'arr-head' }, el('strong', { text: name }),
      el('span', { class: 'hint', text: !saved.key_set ? 'Not connected' : res ? '' : 'Connected. Check again to see how it is doing.' }),
      saved.key_set && el('button', { class: 'btn ghost small danger', type: 'button', text: 'Disconnect', onclick: () => disconnectArr(kind) })),
    el('div', { class: 'arr-row' }, url, key, go),
    res && el('ul', { class: 'status-list arr-result' }, arrLines(kind, res)));
  go.onclick = guard(async () => {
    const local = {};
    block.querySelectorAll('.arr-local').forEach(i => { if (i.value.trim()) local[i.dataset.remote] = i.value.trim(); });
    const body = { kind, url: url.value.trim(), key: key.value.trim(), local };
    const reach = $('#suReach').value.trim();
    if (reach !== (S.cfg.reach_url || '')) body.reach = reach;
    go.disabled = true;
    go.textContent = 'Connecting…';
    try {
      const out = await api('/api/arr/connect', body);
      S.arrResult[kind] = out;
      applyState(out.state);
      block.replaceWith(arrBlock(kind));  // only this block: what was typed for the other app stays
      showReachField();
    } finally {
      go.disabled = false;
      go.textContent = label;
    }
  });
  return block;
}

const disconnectArr = guard(async kind => {
  const out = await api('/api/arr/disconnect', { kind });
  delete S.arrResult[kind];
  applyState(out.state);
  $(`.arr[data-kind="${kind}"]`).replaceWith(arrBlock(kind));
  toast(out.note || `${ARR[kind].name} disconnected, and Subtle's webhook removed from it.`, !!out.note);
});

function openSetup() {
  renderSetupKey();
  const lang = $('#suLang');
  lang.value = S.cfg.target_name || '';
  lang.classList.toggle('need', !S.cfg.target_code);
  $('#suReach').value = S.cfg.reach_url || '';
  showReachField();
  $('#suArr').replaceChildren(...Object.keys(ARR).map(arrBlock));
  if (!$('#setupDlg').open) $('#setupDlg').showModal();
}

// ── wiring ─────────────────────────────────────────────────────────────────
function route() {
  if (location.hash.startsWith('#/library')) return openLibrary();
  $('#library').hidden = true;
  const m = location.hash.match(/^#\/job\/([0-9a-f]+)/);
  if (m && S.jobs.has(m[1])) openJob(m[1]);
  else { if (m) history.replaceState(null, '', '#/'); closeJob(); }
}

function setTheme(next) {
  const root = document.documentElement;
  if (next) { root.dataset.theme = next; try { localStorage.setItem('subtle-theme', next); } catch (e) {} }
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : !matchMedia('(prefers-color-scheme: light)').matches;
  $('#themeBtn').innerHTML = dark
    ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>'
    : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/></svg>';
  return dark;
}

function bind() {
  $('#lang').addEventListener('change', e => saveCfg({ target: e.target.value }));
  for (const id of ['register', 'sdh', 'lyrics']) $('#' + id).addEventListener('change', e => saveCfg({ [id]: e.target.value }));
  $('#notes').addEventListener('change', e => saveCfg({ notes: e.target.value }));
  $('#glossaryBtn').onclick = openGlossary;
  $('#settingsBtn').onclick = openSettings;
  $('#libBtn').onclick = () => { location.hash = '#/library'; };
  $('#libRefresh').onclick = loadLibrary;
  $('#libSearch').addEventListener('input', e => { L.query = e.target.value.trim().toLowerCase(); renderLibrary(); });
  $('#keyBannerBtn').onclick = openSetup;
  $('#autoBtn').onclick = openAutomation;
  $('#suKeySave').onclick = guard(async () => {
    const key = $('#suKey').value.trim();
    if (!key) return;
    await saveCfg({ api_key: key });
    renderSetupKey();
    toast('Key saved.');
  });
  $('#suLang').addEventListener('change', async e => {
    await saveCfg({ target: e.target.value });
    e.target.value = S.cfg.target_name || '';
    e.target.classList.toggle('need', !S.cfg.target_code);
  });
  $('#setupDlg').addEventListener('keydown', e => {
    // Enter in a field must not press the dialog's Done button.
    if (e.key !== 'Enter' || e.target.tagName !== 'INPUT') return;
    e.preventDefault();
    const block = e.target.closest('.arr');
    if (block) $('.arr-go', block).click();
    else if (e.target.id === 'suKey') $('#suKeySave').click();
    else e.target.blur();
  });
  $('#setupDlg').addEventListener('close', () => {
    const patch = {}, reach = $('#suReach').value.trim();
    if (!S.cfg.setup_seen) patch.setup_seen = true;
    if (reach !== (S.cfg.reach_url || '')) patch.reach_url = reach;
    if (Object.keys(patch).length) saveCfg(patch);
  });
  document.querySelectorAll('[data-copy]').forEach(btn => btn.addEventListener('click', guard(async () => {
    const field = $('#' + btn.dataset.copy);
    try { await navigator.clipboard.writeText(field.value); } catch (e) { field.select(); document.execCommand('copy'); }
    toast('Copied.');
  })));
  $('#autoDlg').addEventListener('close', () => saveCfg({
    path_maps: [...$('#mapRows').children].map(r => [...r.querySelectorAll('input')].map(i => i.value.trim())).filter(([a, b]) => a && b),
    bazarr_url: $('#bzUrl').value.trim(), bazarr_key: $('#bzKey').value.trim(),
    bazarr_wait: +$('#bzWait').value || 0, auto_daily_budget: Math.max(0, +$('#autoBudget').value || 0),
  }));
  $('#themeBtn').onclick = () => setTheme(setTheme() ? 'light' : 'dark');
  $('#pickBtn').onclick = () => $('#picker').click();
  $('#picker').onchange = e => { upload([...e.target.files]); e.target.value = ''; };
  $('#folderBtn').onclick = openFolder;
  $('#goBtn').onclick = guard(() => api('/api/start', {}));
  $('#zipBtn').onclick = () => { location.href = '/api/download.zip'; };
  $('#clearBtn').onclick = guard(() => api('/api/clear', { which: 'done' }));
  $('#search').addEventListener('input', e => { S.query = e.target.value; applyFilter(); });

  $('#keySave').onclick = guard(async () => {
    const key = $('#apiKey').value.trim();
    if (!key) return;
    await saveCfg({ api_key: key });
    openSettings();
    toast('Key saved.');
  });
  $('#keyRemove').onclick = async () => { await saveCfg({ api_key: '' }); openSettings(); };
  $('#cacheClear').onclick = guard(async () => toast(`Cleared ${plural((await api('/api/cache/clear', {})).removed, 'cached chunk')}.`));
  $('#settingsDlg').addEventListener('close', () => saveCfg({
    budget: +$('#budget').value || 0, escalate_model: $('#escalate').value, chunk_lines: +$('#chunk').value || 120,
    concurrency: +$('#conc').value || 3, max_chars: +$('#maxChars').value || 42, max_cps: +$('#maxCps').value || 21,
    save_next_to_source: $('#saveNext').checked, bilingual: $('#bilingual').checked,
    proofread: $('#proofAuto').checked, proof_model: $('#proofModel').value,
  }));

  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  addEventListener('dragenter', e => { if (hasFiles(e)) { depth++; document.body.classList.add('dragging'); } });
  addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; document.body.classList.remove('dragging'); } });
  addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove('dragging');
    if (location.hash.startsWith('#/job/')) location.hash = '#/';
    upload([...e.dataTransfer.files]);
  });
  addEventListener('keydown', e => {
    const typing = e.target.closest('input, select, textarea, [contenteditable]');
    if (e.key === '/' && !typing && S.open) { e.preventDefault(); $('#search').focus(); }
  });
  addEventListener('hashchange', route);
}

(async function boot() {
  setTheme();
  bind();
  try {
    applyState(await api('/api/state'));
  } catch (e) { toast('Could not reach Subtle: ' + e.message, true); return; }
  connect();
  route();
  // First run: nothing can be translated yet, so start with the setup screen.
  if (!S.cfg.setup_seen && (S.keySource === 'none' || !S.cfg.target_code)) openSetup();
})();
