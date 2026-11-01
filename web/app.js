'use strict';

const GROUP_ORDER = ['Image', 'Voice', 'Music', 'Video'];
const ACTIVE = new Set(['queued', 'running']);
const FINISHED = new Set(['done', 'failed', 'cancelled']);
const STATUS_LABEL = {queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', cancelled: 'Cancelled'};
const KEY = {
  lastTool: 'ws.lastTool',
  advanced: 'ws.advancedOpen',
  form: (toolId) => 'ws.form.' + toolId,
};

// "Use in..." targets per output kind: [tool id, file field name]. Missing tools or fields are skipped.
const SEND_TARGETS = {
  image: [['image.klein_edit', 'references'], ['video.i2v', 'image'], ['video.a2v', 'image']],
  audio: [['video.a2v', 'audio'], ['edit.mix', 'music'], ['voice.drama3', 'ref0'], ['voice.drama3', 'ref1']],
  video: [['video.extend', 'video'], ['edit.mix', 'video']],
};

const $ = (id) => document.getElementById(id);

const state = {
  tools: [],
  toolsById: new Map(),
  tool: null,
  controls: new Map(), // field name -> control
  jobs: [],
  jobsFetchedAt: 0,
  jobsSeq: 0,
  jobsTimer: null,
  cards: new Map(), // job id -> card
  openDetails: new Set(), // "jobId:kind" of details the user opened
  offline: {tools: false, jobs: false, system: false},
  submitting: false,
  saveTimer: null,
  lightbox: {items: [], index: 0},
  sessionFiles: new Map(), // "toolId:field" -> File[]; memory only, never stored
  menu: null,
};

// ---------- helpers ----------

function lsGet(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch {
    return null;
  }
}

function lsSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable: nothing to remember
  }
}

function lsRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

// Builds an element. String attributes go through setAttribute, booleans and
// functions are set as properties/listeners, children become text nodes.
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (v === true) {
        if (k in el) el[k] = true;
        else el.setAttribute(k, '');
      }
      else el.setAttribute(k, String(v));
    }
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

// Output URLs must stay on this origin under /files/.
function safeFileUrl(url) {
  return typeof url === 'string' && url.startsWith('/files/') ? url : null;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '';
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const time = d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
  if (d.toDateString() === new Date().toDateString()) return time;
  return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) + ' ' + time;
}

function fmtGb(v) {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(1) + ' GB' : '-';
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function fmtValue(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// ---------- API ----------

async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, Object.assign({cache: 'no-store'}, opts));
  } catch {
    const err = new Error('Server offline');
    err.offline = true;
    throw err;
  }
  const text = await res.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) throw new Error(detailText(body, res));
  return body;
}

function detailText(body, res) {
  const d = body && typeof body === 'object' ? body.detail : body;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d)) {
    // FastAPI validation errors
    return d.map((x) => {
      if (x && x.msg) {
        const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
        return (loc ? loc + ': ' : '') + x.msg;
      }
      return JSON.stringify(x);
    }).join('\n');
  }
  if (d) return JSON.stringify(d);
  return ('HTTP ' + res.status + ' ' + res.statusText).trim();
}

function setOffline(source, value) {
  state.offline[source] = value;
  $('offline').hidden = !Object.values(state.offline).some(Boolean);
}

// ---------- tools and sidebar ----------

async function loadTools() {
  try {
    const tools = await api('/api/tools');
    setOffline('tools', false);
    state.tools = tools;
    state.toolsById = new Map(tools.map((t) => [t.id, t]));
    renderNav();
    const last = lsGet(KEY.lastTool);
    const first = orderedGroups()[0];
    const initial = state.toolsById.has(last) ? last : (first ? first.tools[0].id : null);
    if (initial) selectTool(initial);
    else $('tool-head').replaceChildren(h('p', {class: 'muted'}, 'The server reports no tools.'));
    // Rebuild cards so "Use in..." menus reflect the tool list.
    for (const card of state.cards.values()) card.sig = null;
    renderJobs();
  } catch (e) {
    setOffline('tools', !!e.offline);
    $('tool-nav').replaceChildren(
      h('p', {class: 'muted small'}, e.offline ? 'Waiting for the server...' : 'Could not load tools: ' + e.message));
    setTimeout(loadTools, 3000);
  }
}

function orderedGroups() {
  const groups = new Map();
  for (const t of state.tools) {
    if (!groups.has(t.group)) groups.set(t.group, []);
    groups.get(t.group).push(t);
  }
  const names = [...groups.keys()].sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a), ib = GROUP_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return names.map((name) => ({name, tools: groups.get(name)}));
}

function renderNav() {
  const nav = $('tool-nav');
  nav.replaceChildren();
  for (const g of orderedGroups()) {
    nav.appendChild(h('div', {class: 'nav-group'},
      h('div', {class: 'nav-group-name'}, g.name),
      g.tools.map((t) => h('button', {
        type: 'button',
        class: 'nav-tool',
        'data-tool': t.id,
        title: t.description || null,
        onclick: () => selectTool(t.id),
      }, t.label))));
  }
  markActiveNav();
}

function markActiveNav() {
  for (const b of document.querySelectorAll('.nav-tool')) {
    const on = state.tool && b.getAttribute('data-tool') === state.tool.id;
    b.classList.toggle('active', !!on);
    if (on) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  }
}

function selectTool(id) {
  const tool = state.toolsById.get(id);
  if (!tool) return;
  if (state.tool && state.tool.id === id) return;
  saveFormNow();
  state.tool = tool;
  lsSet(KEY.lastTool, id);
  markActiveNav();
  buildForm(tool);
  const saved = lsGet(KEY.form(id));
  if (saved && typeof saved === 'object') applyValues(saved);
  for (const ctl of state.controls.values()) {
    const files = ctl.isFile && state.sessionFiles.get(ctl.key);
    if (files && files.length) ctl.setFiles(files);
  }
  growAll();
}

// ---------- form ----------

function buildForm(tool) {
  const head = $('tool-head');
  head.replaceChildren();
  appendChildren(head, [
    h('div', {class: 'tool-title'},
      h('h1', null, tool.label),
      h('span', {class: 'tool-group'}, tool.group)),
    tool.description ? h('p', {class: 'tool-desc'}, tool.description) : null]);

  const main = $('fields');
  const adv = $('advanced-fields');
  main.replaceChildren();
  adv.replaceChildren();
  state.controls = new Map();
  let advCount = 0;
  for (const f of tool.fields || []) {
    const ctl = makeControl(f);
    ctl.key = tool.id + ':' + f.name;
    state.controls.set(f.name, ctl);
    if (f.advanced) {
      adv.appendChild(ctl.wrap);
      advCount += 1;
    } else {
      main.appendChild(ctl.wrap);
    }
  }
  const details = $('advanced');
  details.hidden = advCount === 0;
  details.open = lsGet(KEY.advanced) === true;
  details.querySelector('summary').textContent = 'Advanced (' + advCount + ')';
  clearFormMessages();
  $('generate').disabled = false;
  $('reset-form').disabled = false;
}

function makeControl(f) {
  const id = 'f-' + f.name;
  const wide = ['text', 'textarea', 'file', 'files'].includes(f.type);
  const wrap = h('div', {class: 'field type-' + f.type + (wide ? ' wide' : '')});
  const errEl = h('div', {class: 'field-error', hidden: true});
  const helpEl = f.help ? h('div', {class: 'help'}, f.help) : null;
  const req = f.required ? h('span', {class: 'req', title: 'Required'}, '*') : null;
  const ctl = {field: f, wrap, errEl, isFile: false};

  if (f.type === 'checkbox') {
    const input = h('input', {type: 'checkbox', id, name: f.name});
    appendChildren(wrap, [h('label', {class: 'check', for: id}, input, h('span', null, f.label), req), helpEl, errEl]);
    ctl.get = () => input.checked;
    ctl.set = (v) => { input.checked = v === true || v === 'true' || v === 1; };
    ctl.reset = () => ctl.set(f.default === true);
    ctl.validate = () => null;
    ctl.focus = () => input.focus();
    ctl.reset();
    return ctl;
  }

  const label = h('label', {for: id}, f.label, req);
  let input;

  if (f.type === 'textarea') {
    const mono = /prompt|lyric|tags|caption/i.test(f.name);
    input = h('textarea', {id, name: f.name, rows: 3, class: mono ? 'mono' : null, spellcheck: 'false'});
    input.addEventListener('input', () => grow(input));
    ctl.grow = () => grow(input);
  } else if (f.type === 'number') {
    input = h('input', {
      type: 'number', id, name: f.name, inputmode: 'decimal',
      min: f.min ?? null, max: f.max ?? null, step: f.step ?? 'any',
      placeholder: f.default === null ? 'empty' : null,
    });
  } else if (f.type === 'select') {
    input = h('select', {id, name: f.name},
      (f.options || []).map((o) => h('option', {value: o.value}, o.label ?? o.value)));
  } else if (f.type === 'file' || f.type === 'files') {
    return makeFileControl(f, ctl, label, helpEl, errEl);
  } else {
    input = h('input', {type: 'text', id, name: f.name, spellcheck: 'false'});
  }

  appendChildren(wrap, [label, input, helpEl, errEl]);
  ctl.get = () => input.value;
  ctl.set = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    if (f.type === 'select' && ![...input.options].some((o) => o.value === s)) return;
    input.value = s;
    if (ctl.grow) ctl.grow();
  };
  ctl.reset = () => {
    if (f.type === 'select') {
      input.selectedIndex = 0;
      if (f.default !== null && f.default !== undefined) ctl.set(f.default);
    } else {
      ctl.set(f.default);
    }
  };
  ctl.focus = () => input.focus();
  ctl.validate = () => {
    const v = input.value;
    if (f.type === 'number') {
      if (input.validity.badInput) return 'Enter a number.';
      if (v === '') return f.required || (f.default !== null && f.default !== undefined) ? 'Enter a number.' : null;
      const n = Number(v);
      if (f.min !== undefined && f.min !== null && n < f.min) return 'Minimum is ' + f.min + '.';
      if (f.max !== undefined && f.max !== null && n > f.max) return 'Maximum is ' + f.max + '.';
      return null;
    }
    if (f.required && v.trim() === '') return 'Required.';
    return null;
  };
  ctl.reset();
  return ctl;
}

function makeFileControl(f, ctl, label, helpEl, errEl) {
  const multiple = f.type === 'files';
  const id = 'f-' + f.name;
  const input = h('input', {type: 'file', id, name: f.name, accept: f.accept || null, multiple});
  const clearBtn = h('button', {type: 'button', class: 'btn ghost small-btn', hidden: true}, 'Clear');
  const preview = h('div', {class: multiple ? 'file-preview multi' : 'file-preview'});
  let urls = [];

  function render() {
    for (const u of urls) URL.revokeObjectURL(u);
    urls = [];
    preview.replaceChildren();
    const files = [...input.files];
    state.sessionFiles.set(ctl.key, files);
    clearBtn.hidden = files.length === 0;
    for (const file of files) {
      const url = URL.createObjectURL(file);
      urls.push(url);
      const caption = h('div', {class: 'file-name small', title: file.name}, file.name + ' (' + fmtBytes(file.size) + ')');
      if (file.type.startsWith('image/')) {
        preview.appendChild(h('figure', {class: 'file-item'}, h('img', {src: url, alt: file.name}), caption));
      } else if (!multiple && file.type.startsWith('audio/')) {
        preview.appendChild(h('figure', {class: 'file-item wide'}, h('audio', {controls: true, preload: 'metadata', src: url}), caption));
      } else if (!multiple && file.type.startsWith('video/')) {
        preview.appendChild(h('figure', {class: 'file-item'}, h('video', {controls: true, playsinline: true, muted: true, preload: 'metadata', src: url}), caption));
      } else {
        preview.appendChild(h('figure', {class: 'file-item plain'}, caption));
      }
    }
    if (multiple && f.max && files.length > f.max) {
      showFieldError(ctl, 'Choose at most ' + f.max + ' files (' + files.length + ' selected).');
    } else {
      showFieldError(ctl, null);
    }
  }

  input.addEventListener('change', render);
  clearBtn.addEventListener('click', () => {
    input.value = '';
    render();
  });

  appendChildren(ctl.wrap, [label, h('div', {class: 'file-row'}, input, clearBtn),
    multiple && f.max ? h('div', {class: 'help'}, 'Up to ' + f.max + ' files.') : null,
    helpEl, errEl, preview]);
  ctl.isFile = true;
  ctl.get = () => (multiple ? [...input.files] : input.files[0] || null);
  ctl.set = () => {};
  ctl.reset = () => {
    input.value = '';
    render();
  };
  ctl.setFiles = (files) => {
    const dt = new DataTransfer();
    for (const file of files) dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', {bubbles: true}));
  };
  // Single file fields replace, multi file fields append. Returns an error text or null.
  ctl.addFile = (file) => {
    if (!multiple) {
      ctl.setFiles([file]);
      return null;
    }
    const files = [...input.files];
    if (f.max && files.length >= f.max) return f.label + ' already has ' + files.length + ' files (max ' + f.max + ').';
    ctl.setFiles([...files, file]);
    return null;
  };
  ctl.focus = () => input.focus();
  ctl.validate = () => {
    const n = input.files.length;
    if (f.required && n === 0) return multiple ? 'Choose at least one file.' : 'Choose a file.';
    if (multiple && f.max && n > f.max) return 'Choose at most ' + f.max + ' files.';
    return null;
  };
  return ctl;
}

function grow(ta) {
  if (!ta.isConnected) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * 0.6)) + 'px';
}

function growAll() {
  requestAnimationFrame(() => {
    for (const ctl of state.controls.values()) if (ctl.grow) ctl.grow();
  });
}

function applyValues(values) {
  for (const [name, v] of Object.entries(values)) {
    const ctl = state.controls.get(name);
    if (ctl && !ctl.isFile) ctl.set(v);
  }
}

function collectValues() {
  const values = {};
  for (const [name, ctl] of state.controls) if (!ctl.isFile) values[name] = ctl.get();
  return values;
}

function saveFormNow() {
  clearTimeout(state.saveTimer);
  if (state.tool) lsSet(KEY.form(state.tool.id), collectValues());
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveFormNow, 300);
}

function showFieldError(ctl, msg) {
  ctl.errEl.textContent = msg || '';
  ctl.errEl.hidden = !msg;
  ctl.wrap.classList.toggle('invalid', !!msg);
}

function clearFormMessages() {
  for (const ctl of state.controls.values()) {
    if (!ctl.isFile) showFieldError(ctl, null);
  }
  showFormError(null);
  setFormStatus('');
}

function showFormError(msg) {
  const el = $('form-error');
  el.textContent = msg || '';
  el.hidden = !msg;
}

let statusTimer = null;
function setFormStatus(msg, kind) {
  const el = $('form-status');
  el.textContent = msg;
  el.className = 'form-status small' + (kind ? ' ' + kind : '');
  clearTimeout(statusTimer);
  if (msg) statusTimer = setTimeout(() => { el.textContent = ''; }, 6000);
}

function resetForm() {
  if (!state.tool) return;
  for (const ctl of state.controls.values()) ctl.reset();
  lsRemove(KEY.form(state.tool.id));
  clearFormMessages();
  growAll();
}

async function submitForm() {
  const tool = state.tool;
  if (!tool || state.submitting) return;
  showFormError(null);
  let firstBad = null;
  for (const ctl of state.controls.values()) {
    const msg = ctl.validate();
    showFieldError(ctl, msg);
    if (msg && !firstBad) firstBad = ctl;
  }
  if (firstBad) {
    if (firstBad.field.advanced) $('advanced').open = true;
    firstBad.focus();
    showFormError('Check the marked fields.');
    return;
  }

  const fd = new FormData();
  fd.append('tool', tool.id);
  for (const [name, ctl] of state.controls) {
    const v = ctl.get();
    if (ctl.field.type === 'file') {
      if (v) fd.append(name, v, v.name);
    } else if (ctl.field.type === 'files') {
      for (const file of v) fd.append(name, file, file.name);
    } else if (ctl.field.type === 'checkbox') {
      fd.append(name, v ? 'true' : 'false');
    } else {
      fd.append(name, v);
    }
  }
  saveFormNow();

  const btn = $('generate');
  state.submitting = true;
  btn.disabled = true;
  btn.textContent = 'Submitting...';
  try {
    const res = await api('/api/jobs', {method: 'POST', body: fd});
    setFormStatus('Queued ' + (res && res.id ? res.id : ''), 'ok');
    refreshJobs();
  } catch (e) {
    showFormError(e.offline ? 'Server offline. The job was not submitted.' : e.message);
  } finally {
    state.submitting = false;
    btn.disabled = false;
    btn.textContent = 'Generate';
  }
}

function useSettings(job) {
  const tool = state.toolsById.get(job.tool);
  if (!tool) {
    setFormStatus('Tool "' + job.tool + '" is not available.', 'err');
    return;
  }
  if (!state.tool || state.tool.id !== tool.id) selectTool(tool.id);
  applyValues(job.params || {});
  const advChanged = [...state.controls.values()].some((c) =>
    c.field.advanced && !c.isFile && job.params && Object.hasOwn(job.params, c.field.name)
    && fmtValue(c.get()) !== fmtValue(c.field.default));
  if (advChanged) $('advanced').open = true;
  saveFormNow();
  clearFormMessages();
  growAll();
  setFormStatus('Settings loaded from ' + job.id + '.', 'ok');
  $('main').scrollTo({top: 0, behavior: 'smooth'});
}

// ---------- jobs ----------

async function refreshJobs() {
  clearTimeout(state.jobsTimer);
  const seq = ++state.jobsSeq;
  let delay;
  try {
    const jobs = await api('/api/jobs');
    if (seq !== state.jobsSeq) return;
    setOffline('jobs', false);
    state.jobs = jobs;
    state.jobsFetchedAt = performance.now();
    renderJobs();
    delay = jobs.some((j) => ACTIVE.has(j.status)) ? 2000 : 10000;
  } catch (e) {
    if (seq !== state.jobsSeq) return;
    setOffline('jobs', !!e.offline);
    if (!e.offline) $('jobs-count').textContent = 'Could not load jobs: ' + e.message;
    delay = 3000;
  }
  state.jobsTimer = setTimeout(refreshJobs, delay);
}

// Fields that change while a job runs are updated in place; anything else rebuilds the card.
function jobSig(j) {
  return JSON.stringify([j.status, j.position, j.tool_label, j.created, j.started, j.finished,
    j.outputs, j.error, j.params]);
}

function renderJobs() {
  const jobs = state.jobs;
  const list = $('jobs');
  const running = jobs.filter((j) => j.status === 'running').length;
  const queued = jobs.filter((j) => j.status === 'queued').length;
  const parts = [jobs.length + (jobs.length === 1 ? ' job' : ' jobs')];
  if (running) parts.push(running + ' running');
  if (queued) parts.push(queued + ' queued');
  $('jobs-count').textContent = parts.join(' · ');
  document.title = (running + queued ? '(' + (running + queued) + ') ' : '') + 'Workstation';

  const seen = new Set();
  jobs.forEach((job, i) => {
    seen.add(job.id);
    let card = state.cards.get(job.id);
    const sig = jobSig(job);
    if (!card || card.sig !== sig) {
      const fresh = buildCard(job, sig);
      if (card) card.el.replaceWith(fresh.el);
      card = fresh;
      state.cards.set(job.id, card);
    }
    updateCard(card, job);
    if (list.children[i] !== card.el) list.insertBefore(card.el, list.children[i] || null);
  });
  for (const [id, card] of state.cards) {
    if (!seen.has(id)) {
      card.el.remove();
      state.cards.delete(id);
    }
  }
  const empty = list.querySelector('.empty');
  if (jobs.length === 0 && !empty) list.appendChild(h('p', {class: 'empty muted'}, 'No jobs yet.'));
  if (jobs.length > 0 && empty) empty.remove();
}

function trackedDetails(job, kind, summary, body, openByDefault) {
  const key = job.id + ':' + kind;
  const d = h('details', {class: 'job-' + kind}, h('summary', null, summary), body);
  d.open = state.openDetails.has(key) || (openByDefault && !state.openDetails.has(key + ':closed'));
  d.addEventListener('toggle', () => {
    if (d.open) {
      state.openDetails.add(key);
      state.openDetails.delete(key + ':closed');
    } else {
      state.openDetails.delete(key);
      state.openDetails.add(key + ':closed');
    }
  });
  return d;
}

function buildCard(job, sig) {
  const card = {sig, job, el: null, refs: {}};
  const status = job.status;
  const badgeText = status === 'queued' && job.position > 0
    ? 'Queued #' + job.position
    : STATUS_LABEL[status] || status;

  const refs = card.refs;
  refs.elapsed = h('span', {class: 'meta-elapsed'});
  refs.peak = h('span', {class: 'meta-peak'});
  refs.log = h('pre', {class: 'log'});

  const actions = h('div', {class: 'job-actions'});
  actions.appendChild(h('button', {
    type: 'button', class: 'btn ghost small-btn', title: 'Select this tool and fill the form with these parameters',
    onclick: () => useSettings(job),
  }, 'Use these settings'));
  if (ACTIVE.has(status)) {
    actions.appendChild(h('button', {type: 'button', class: 'btn ghost small-btn danger',
      onclick: (e) => cancelJob(job, e.currentTarget)}, 'Cancel'));
  } else if (FINISHED.has(status)) {
    actions.appendChild(h('button', {type: 'button', class: 'btn ghost small-btn danger',
      onclick: (e) => deleteJob(job, e.currentTarget)}, 'Delete'));
  }

  const params = job.params || {};
  const promptText = ['prompt', 'text', 'caption'].map((k) => params[k]).find((v) => typeof v === 'string' && v.trim());
  const prompt = promptText || null;
  const outputs = (job.outputs || []).map((o, i) => buildOutput(job, o, i)).filter(Boolean);
  const paramRows = Object.entries(params);

  card.el = h('article', {class: 'job s-' + status, 'data-job': job.id},
    h('header', {class: 'job-head'},
      h('span', {class: 'badge s-' + status}, badgeText),
      h('span', {class: 'job-tool'}, job.tool_label || job.tool),
      h('span', {class: 'job-meta'},
        h('span', {title: job.created || ''}, fmtTime(job.created)),
        refs.elapsed,
        refs.peak),
      actions),
    prompt ? h('p', {class: 'job-prompt', title: prompt}, prompt) : null,
    outputs.length ? h('div', {class: 'outputs'}, outputs) : null,
    job.error ? h('div', {class: 'job-error'}, job.error) : null,
    h('div', {class: 'job-error action-error', hidden: true}),
    paramRows.length
      ? trackedDetails(job, 'params', 'Parameters',
        h('dl', {class: 'params'}, paramRows.map(([k, v]) => [h('dt', null, k), h('dd', null, fmtValue(v))])), false)
      : null,
    refs.logDetails = trackedDetails(job, 'logs', 'Log', refs.log, status === 'failed'),
    h('div', {class: 'job-id muted small'}, job.id));
  refs.logDetails.addEventListener('toggle', () => {
    if (refs.logDetails.open) refs.log.scrollTop = refs.log.scrollHeight;
  });
  return card;
}

function buildOutput(job, o, index) {
  const url = safeFileUrl(o.url);
  if (!url) return null;
  const name = o.name || url.split('/').pop();
  const targets = sendTargets(o.kind);
  const caption = h('figcaption', null,
    h('span', {class: 'out-name', title: name}, name),
    h('span', {class: 'out-actions'},
      targets.length
        ? h('button', {type: 'button', class: 'out-send', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
          title: 'Send this file to another tool', onclick: (e) => toggleSendMenu(e.currentTarget, {url, name}, targets)},
        'Use in…')
        : null,
      h('a', {href: url, download: name, class: 'out-dl'}, 'Download')));
  const note = o.note ? h('div', {class: 'out-note'}, o.note) : null;
  let media;
  if (o.kind === 'image') {
    media = h('button', {type: 'button', class: 'thumb', title: 'Enlarge', onclick: () => openLightbox(job, index)},
      h('img', {src: url, alt: name, loading: 'lazy'}));
  } else if (o.kind === 'audio') {
    media = h('audio', {controls: true, preload: 'metadata', src: url});
  } else if (o.kind === 'video') {
    media = h('video', {controls: true, playsinline: true, preload: 'metadata', src: url});
  }
  return h('figure', {class: 'out out-' + (o.kind || 'file')}, media, caption, note);
}

function liveElapsed(job) {
  if (job.status !== 'running') return job.elapsed;
  if (typeof job.elapsed === 'number') return job.elapsed + (performance.now() - state.jobsFetchedAt) / 1000;
  const started = Date.parse(job.started);
  return isNaN(started) ? null : (Date.now() - started) / 1000;
}

function updateCard(card, job) {
  card.job = job;
  const el = liveElapsed(job);
  card.refs.elapsed.textContent = job.status === 'running' || el > 0 ? fmtDuration(el) : '';
  card.refs.peak.textContent = typeof job.peak_gb === 'number' ? 'peak ' + fmtGb(job.peak_gb) : '';
  const log = job.log_tail || '';
  if (card.refs.log.textContent !== log) {
    const pre = card.refs.log;
    const follow = !pre.textContent || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    pre.textContent = log;
    if (follow) requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
  }
  card.refs.logDetails.hidden = !log;
}

function tickElapsed() {
  for (const card of state.cards.values()) {
    if (card.job.status === 'running') card.refs.elapsed.textContent = fmtDuration(liveElapsed(card.job));
  }
}

function showCardError(job, msg) {
  const card = state.cards.get(job.id);
  if (!card) return;
  const el = card.el.querySelector('.action-error');
  el.textContent = msg;
  el.hidden = !msg;
}

async function cancelJob(job, btn) {
  btn.disabled = true;
  btn.textContent = 'Cancelling...';
  try {
    await api('/api/jobs/' + encodeURIComponent(job.id) + '/cancel', {method: 'POST'});
  } catch (e) {
    showCardError(job, 'Cancel failed: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Cancel';
  }
  refreshJobs();
}

async function deleteJob(job, btn) {
  // Two-step confirm, no native dialog.
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = 'Confirm delete';
    btn.classList.add('armed');
    setTimeout(() => {
      if (btn.isConnected && btn.dataset.armed === '1' && !btn.disabled) {
        btn.dataset.armed = '';
        btn.textContent = 'Delete';
        btn.classList.remove('armed');
      }
    }, 3000);
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Deleting...';
  try {
    await api('/api/jobs/' + encodeURIComponent(job.id), {method: 'DELETE'});
    state.openDetails.forEach((k) => { if (k.startsWith(job.id + ':')) state.openDetails.delete(k); });
  } catch (e) {
    showCardError(job, 'Delete failed: ' + e.message);
    btn.disabled = false;
    btn.dataset.armed = '';
    btn.textContent = 'Delete';
    btn.classList.remove('armed');
  }
  refreshJobs();
}

// ---------- "Use in..." ----------

function sendTargets(kind) {
  const out = [];
  for (const [toolId, fieldName] of SEND_TARGETS[kind] || []) {
    const tool = state.toolsById.get(toolId);
    const field = tool && (tool.fields || []).find((f) => f.name === fieldName);
    if (field && (field.type === 'file' || field.type === 'files')) out.push({tool, field});
  }
  return out;
}

function toggleSendMenu(btn, file, targets) {
  const wasOpenHere = state.menu && state.menu.anchor === btn;
  closeSendMenu();
  if (wasOpenHere) return;
  const menu = h('div', {class: 'send-menu', role: 'menu'},
    targets.map((t) => h('button', {
      type: 'button', role: 'menuitem', class: 'send-item',
      onclick: () => {
        closeSendMenu();
        sendFile(file, t);
      },
    }, h('span', null, t.tool.label), h('span', {class: 'send-field'}, t.field.label))));
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  const mh = menu.offsetHeight, mw = menu.offsetWidth;
  const top = r.bottom + 4 + mh > window.innerHeight ? r.top - 4 - mh : r.bottom + 4;
  menu.style.top = Math.max(4, top) + 'px';
  menu.style.left = Math.max(4, Math.min(r.left, window.innerWidth - mw - 4)) + 'px';
  btn.setAttribute('aria-expanded', 'true');
  state.menu = {el: menu, anchor: btn};
  menu.querySelector('button').focus();
}

function closeSendMenu() {
  if (!state.menu) return;
  state.menu.el.remove();
  state.menu.anchor.setAttribute('aria-expanded', 'false');
  state.menu = null;
}

async function sendFile(file, target) {
  let blob;
  try {
    const res = await fetch(file.url, {cache: 'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    blob = await res.blob();
  } catch (e) {
    toast('Could not load ' + file.name + ': ' + e.message, 'err');
    return;
  }
  const f = new File([blob], file.name, {type: blob.type});
  selectTool(target.tool.id);
  const ctl = state.controls.get(target.field.name);
  if (!ctl || !ctl.isFile) {
    toast(target.tool.label + ' › ' + target.field.label + ' is not available.', 'err');
    return;
  }
  const err = ctl.addFile(f);
  if (err) {
    toast(err, 'err');
    return;
  }
  if (target.field.advanced) $('advanced').open = true;
  toast('Sent ' + file.name + ' to ' + target.tool.label + ' › ' + target.field.label);
  ctl.wrap.scrollIntoView({block: 'center', behavior: 'smooth'});
}

let toastTimer = null;
function toast(msg, kind) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'err' ? 6000 : 3500);
}

// ---------- system ----------

async function refreshSystem() {
  const box = $('system');
  try {
    const s = await api('/api/system');
    setOffline('system', false);
    renderSystem(s);
  } catch (e) {
    setOffline('system', !!e.offline);
    box.replaceChildren(h('div', {class: 'system-line warn small'}, e.offline ? 'Server offline' : 'System: ' + e.message));
  }
  setTimeout(refreshSystem, 5000);
}

function renderSystem(s) {
  const total = s.mem_total_gb, free = s.mem_free_gb;
  const usedPct = total > 0 ? Math.min(100, Math.max(0, ((total - free) / total) * 100)) : 0;
  const bar = h('div', {class: 'mem-bar' + (free < 8 ? ' low' : '')}, h('div', {class: 'mem-used'}));
  bar.firstChild.style.width = usedPct.toFixed(1) + '%';

  let runningText = 'idle';
  if (s.running) {
    const job = state.jobs.find((j) => j.id === s.running);
    runningText = 'running ' + (job ? job.tool_label || job.tool : s.running);
  }

  $('system').replaceChildren(
    h('div', {class: 'system-line small'},
      h('span', null, 'Memory'),
      h('span', {class: free < 8 ? 'warn' : null}, fmtGb(free) + ' free / ' + fmtGb(total))),
    bar,
    h('div', {class: 'system-line small'}, h('span', null, 'Swap'), h('span', null, fmtGb(s.swap_used_gb))),
    h('div', {class: 'system-line small'},
      h('span', null, 'Queue'),
      h('span', {title: s.running || ''}, (s.queued || 0) + ' queued, ' + runningText)));
}

// ---------- lightbox ----------

function openLightbox(job, outputIndex) {
  const outputs = job.outputs || [];
  const items = [];
  let index = 0;
  outputs.forEach((o, i) => {
    const url = safeFileUrl(o.url);
    if (o.kind !== 'image' || !url) return;
    if (i === outputIndex) index = items.length;
    items.push({url, name: o.name || url.split('/').pop(), job: job.tool_label || job.tool});
  });
  if (!items.length) return;
  state.lightbox = {items, index};
  $('lightbox').hidden = false;
  showLightboxItem();
}

function showLightboxItem() {
  const {items, index} = state.lightbox;
  const it = items[index];
  $('lb-img').src = it.url;
  $('lb-img').alt = it.name;
  $('lb-caption').textContent = it.job + ' · ' + it.name + (items.length > 1 ? ' · ' + (index + 1) + '/' + items.length : '');
  const multi = items.length > 1;
  document.querySelector('.lb-prev').hidden = !multi;
  document.querySelector('.lb-next').hidden = !multi;
}

function stepLightbox(delta) {
  const lb = state.lightbox;
  if (lb.items.length < 2) return;
  lb.index = (lb.index + delta + lb.items.length) % lb.items.length;
  showLightboxItem();
}

function closeLightbox() {
  $('lightbox').hidden = true;
  $('lb-img').removeAttribute('src');
}

// ---------- wiring ----------

function init() {
  const form = $('tool-form');
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submitForm();
  });
  form.addEventListener('input', scheduleSave);
  form.addEventListener('change', scheduleSave);
  // Plain Enter in a single-line input must not start a job.
  form.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey && e.target.tagName === 'INPUT') e.preventDefault();
  });
  $('reset-form').addEventListener('click', resetForm);
  $('advanced').addEventListener('toggle', () => lsSet(KEY.advanced, $('advanced').open));

  const lb = $('lightbox');
  lb.addEventListener('click', (e) => {
    if (e.target === lb || e.target.classList.contains('lb-figure')) closeLightbox();
  });
  lb.querySelector('.lb-close').addEventListener('click', closeLightbox);
  lb.querySelector('.lb-prev').addEventListener('click', () => stepLightbox(-1));
  lb.querySelector('.lb-next').addEventListener('click', () => stepLightbox(1));

  // Nothing may navigate the app window away (pywebview has no back button):
  // /files links only download, and dropped files outside a file input are ignored.
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (a && a.getAttribute('href').startsWith('/files/') && !a.hasAttribute('download')) e.preventDefault();
    if (state.menu && !state.menu.el.contains(e.target) && e.target !== state.menu.anchor) closeSendMenu();
  }, true);
  const isFileInput = (t) => t instanceof HTMLInputElement && t.type === 'file';
  window.addEventListener('dragover', (e) => { if (!isFileInput(e.target)) e.preventDefault(); });
  window.addEventListener('drop', (e) => { if (!isFileInput(e.target)) e.preventDefault(); });
  $('main').addEventListener('scroll', closeSendMenu, {passive: true});
  window.addEventListener('resize', closeSendMenu);

  document.addEventListener('keydown', (e) => {
    if (state.menu && e.key === 'Escape') {
      const anchor = state.menu.anchor;
      closeSendMenu();
      anchor.focus();
      return;
    }
    if (!lb.hidden) {
      if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'ArrowLeft') stepLightbox(-1);
      else if (e.key === 'ArrowRight') stepLightbox(1);
      return;
    }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submitForm();
    }
  });
  window.addEventListener('resize', growAll);
  window.addEventListener('pagehide', saveFormNow);

  setInterval(tickElapsed, 1000);
  loadTools();
  refreshJobs();
  refreshSystem();
}

init();
