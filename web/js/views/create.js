// Create: tool picker, the selected tool's form (from GET /api/tools), prompt help, and the pre-flight rail with
// Generate. Form values are saved per tool in localStorage; chosen files stay in memory for this session.

import {store} from '../store.js';
import {
  h, append, icon, toolIcon, groupTools, grow, lsGet, lsSet, lsRemove, fmtAbout, fmtBytes, fmtClock, fmtGb, fmtLeft, fmtTime,
  fmtValue, fit, toast,
} from '../ui.js';

const KEY = {
  lastTool: 'ws.lastTool',
  advanced: 'ws.advancedOpen',
  form: (toolId) => 'ws.form.' + toolId,
};
// Prompt length limits the model needs (README: MOSS-Image gives a black image above about 400 tokens).
const TOKEN_LIMITS = {'image.moss:prompt': 400};
const NOUN = {image: 'image', audio: 'audio file', video: 'video'};

// Kept across mounts: chosen files (never stored) and prompt-help state per "toolId:field".
const sessionFiles = new Map();
const improve = new Map();
const improveRuns = new Map(); // "toolId:field" -> the running Improve request: {id, api, ctrl, cancelled}
const fileInfo = new WeakMap();

let s = null; // the mounted view

// ---------- mount ----------

function mount(root, ctx, params) {
  root.classList.add('v-create');
  s = {
    root, ctx, tool: null, controls: new Map(), offs: [], saveTimer: null, submitting: false,
    els: {}, pickerRows: new Map(), headSig: null, lastSig: null,
  };
  const els = s.els;
  els.setup = h('div', {class: 'create-setup'});
  els.picker = h('nav', {class: 'picker', 'aria-label': 'Tools'});
  els.form = h('form', {class: 'tool-form', novalidate: true, autocomplete: 'off', 'aria-label': 'Tool settings'});
  els.rail = h('aside', {class: 'rail', 'aria-label': 'Before you generate'});
  root.append(els.setup, h('div', {class: 'create'}, els.picker, els.form, els.rail));
  buildRail();

  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    submit();
  });
  els.form.addEventListener('input', onFormInput);
  els.form.addEventListener('change', scheduleSave);
  // Plain Enter in a single-line input must not start a job.
  els.form.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey && e.target.tagName === 'INPUT') e.preventDefault();
  });
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', growAll);
  window.addEventListener('pagehide', saveFormNow);

  const {on} = ctx.store;
  s.offs.push(
    on('tools', () => {
      buildPicker();
      if (!s.tool) selectTool(initialTool(params));
    }),
    on('models', refreshFacts),
    on('system', refreshFacts),
    on('jobs', refreshFacts),
    on('llm', () => {
      refreshFacts();
      renderAllImprove();
    }),
    on('assist', renderAllImprove),
  );

  if (store.get('tools').length) {
    buildPicker();
    selectTool(initialTool(params));
  } else {
    els.form.append(h('p', {class: 'muted'}, 'Loading tools...'));
  }
}

function unmount() {
  if (!s) return;
  saveFormNow();
  for (const off of s.offs) off();
  document.removeEventListener('keydown', onKey);
  window.removeEventListener('resize', growAll);
  window.removeEventListener('pagehide', saveFormNow);
  s = null;
}

// A route change while Create is open: another tool, or a handoff to the tool on screen.
function update(params) {
  if (!s || !store.get('tools').length) return;
  const id = params && params.tool;
  if (id && store.get('toolsById').has(id) && (!s.tool || s.tool.id !== id)) selectTool(id);
  else if (s.tool) takeHandoff(s.tool.id);
}

function initialTool(params) {
  const byId = store.get('toolsById');
  if (params && byId.has(params.tool)) return params.tool;
  const last = lsGet(KEY.lastTool);
  if (byId.has(last)) return last;
  const first = groupTools(store.get('tools'))[0];
  return first ? first.tools[0].id : null;
}

function onKey(e) {
  if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return;
  if (document.querySelector('dialog[open], .lightbox')) return;
  e.preventDefault();
  submit();
}

// ---------- picker ----------

function buildPicker() {
  const picker = s.els.picker;
  picker.replaceChildren();
  s.pickerRows = new Map();
  for (const g of groupTools(store.get('tools'))) {
    const list = h('ul', {role: 'list'});
    for (const t of g.tools) {
      const meta = h('span', {class: 'pk-meta'});
      const dot = h('span', {class: 'dot', 'aria-hidden': 'true', hidden: true});
      const btn = h('button', {type: 'button', class: 'pk-item', 'data-tool': t.id, onclick: () => selectTool(t.id)},
        toolIcon(t.group), h('span', {class: 'pk-text'}, h('span', {class: 'pk-name'}, t.label), meta), dot);
      s.pickerRows.set(t.id, {btn, meta, dot, tool: t});
      list.append(h('li', null, btn));
    }
    picker.append(h('div', {class: 'pk-group'}, h('h3', {class: 'eyebrow'}, g.name), list));
  }
  updatePicker();
}

function toolFacts(toolId) {
  const peak = store.toolPeak(toolId);
  const mem = store.memTotal();
  return {
    peak,
    mem,
    installed: store.toolInstalled(toolId),
    fit: peak === null ? null : fit(peak, mem, store.reserve()),
    time: store.toolTime(toolId),
  };
}

function peakText(peak) {
  if (peak === null) return '';
  return peak === 0 ? 'under 1 GB' : 'peak ' + Math.round(peak) + ' GB';
}

function updatePicker() {
  for (const [id, row] of s.pickerRows) {
    const f = toolFacts(id);
    const time = f.time ? fmtAbout(f.time.seconds) : '';
    let meta, dot = null;
    if (f.installed === false) {
      meta = 'Not installed';
      dot = 'off';
    } else if (f.fit && f.fit.k !== 'ok') {
      meta = [f.fit.short, time].filter(Boolean).join(' · ');
      dot = f.fit.k;
    } else {
      meta = [time, peakText(f.peak)].filter(Boolean).join(' · ');
    }
    row.meta.textContent = meta;
    row.dot.className = 'dot' + (dot ? ' dot-' + dot : '');
    row.dot.hidden = !dot;
    row.btn.setAttribute('aria-label', row.tool.label + (meta ? '. ' + meta : ''));
    const on = !!(s.tool && s.tool.id === id);
    row.btn.setAttribute('aria-current', on ? 'true' : 'false');
  }
}

// ---------- tool selection ----------

function selectTool(id) {
  const tool = store.get('toolsById').get(id);
  if (!tool) return;
  if (s.tool && s.tool.id === id) {
    takeHandoff(id);
    return;
  }
  // Leaving a tool cancels its running Improve (leaving the view does not: see runImprove).
  if (s.tool) for (const name of s.controls.keys()) cancelImprove(s.tool.id + ':' + name);
  saveFormNow();
  s.tool = tool;
  lsSet(KEY.lastTool, id);
  s.ctx.router.replace('create', {tool: id});
  buildForm(tool);
  const saved = lsGet(KEY.form(id));
  if (saved && typeof saved === 'object') applyValues(saved);
  for (const ctl of s.controls.values()) {
    const files = ctl.isFile && sessionFiles.get(ctl.key);
    if (files && files.length) ctl.setFiles(files);
  }
  renderAllImprove();
  updateCounters();
  growAll();
  refreshFacts();
  takeHandoff(id);
}

// ---------- form ----------

function buildForm(tool) {
  const form = s.els.form;
  form.replaceChildren();
  s.controls = new Map();
  s.headSig = null;
  s.els.head = h('header', {class: 'tool-head'});
  s.els.notice = h('div', {class: 'tool-notice'});
  s.els.summary = h('div', {class: 'form-summary', role: 'alert', hidden: true});
  const main = h('div', {class: 'fields'});
  const adv = h('div', {class: 'fields'});
  const advNames = [];
  for (const f of tool.fields || []) {
    const ctl = makeControl(tool, f);
    ctl.key = tool.id + ':' + f.name;
    s.controls.set(f.name, ctl);
    if (f.advanced) {
      adv.append(ctl.wrap);
      advNames.push(f.label);
    } else {
      main.append(ctl.wrap);
    }
  }
  form.append(s.els.head, s.els.notice, s.els.summary, main);
  s.els.adv = null;
  if (advNames.length) {
    const details = h('details', {class: 'adv'},
      h('summary', null, icon('chev'), 'Advanced settings',
        h('span', {class: 'faint'}, advNames.slice(0, 3).join(', ') + (advNames.length > 3 ? ', ...' : ''))),
      adv);
    details.open = lsGet(KEY.advanced) === true;
    details.addEventListener('toggle', () => lsSet(KEY.advanced, details.open));
    form.append(details);
    s.els.adv = details;
  }
  renderHead();
}

function renderHead() {
  const tool = s.tool;
  const f = toolFacts(tool.id);
  const models = (store.toolModels(tool.id) || []).filter((m) => m.kind === 'download');
  const sig = JSON.stringify([tool.id, f.peak, f.installed, f.time, models.map((m) => m.label)]);
  if (sig === s.headSig) return;
  s.headSig = sig;
  const chips = [];
  if (store.get('models')) {
    chips.push(h('span', {class: 'chip'}, models.length ? models.map((m) => m.label).join(' + ') : 'No model (ffmpeg)'));
  }
  if (f.peak !== null) chips.push(h('span', {class: 'chip'}, icon('chip', 'ic-sm'), f.peak ? 'Peak ' + Math.round(f.peak) + ' GB' : 'Under 1 GB'));
  if (f.time) chips.push(h('span', {class: 'chip', title: 'Median of ' + f.time.count + ' finished jobs on this Mac'}, icon('clock', 'ic-sm'), fmtAbout(f.time.seconds)));
  s.els.head.replaceChildren(
    toolIcon(tool.group),
    h('div', {class: 'tool-head-text'},
      h('span', {class: 'eyebrow'}, tool.group),
      h('h2', null, tool.label),
      tool.description ? h('p', null, tool.description) : null,
      chips.length ? h('div', {class: 'chips'}, chips) : null));

  s.els.notice.replaceChildren();
  if (f.installed === false) {
    const missing = (store.toolModels(tool.id) || []).filter((m) => !m.installed).map((m) => m.label);
    s.els.notice.append(h('div', {class: 'banner banner-warn'}, icon('alert'),
      h('div', {class: 'banner-body'},
        h('strong', null, missing.join(', ') + (missing.length === 1 ? ' is' : ' are') + ' not installed'),
        h('span', null, 'This tool needs ' + (missing.length === 1 ? 'it' : 'them') + '. Install on the Models screen, or set the path in Settings if it is in another folder.'),
        h('div', {class: 'banner-actions'},
          h('button', {type: 'button', class: 'btn btn-sm btn-primary', onclick: () => s.ctx.router.go('models')}, 'Install in Models'),
          h('button', {type: 'button', class: 'btn btn-sm', onclick: () => s.ctx.router.go('settings')}, 'It is in another folder')))));
  }
}

function fieldIds(f) {
  const id = 'f-' + f.name;
  return {id, help: id + '-help', err: id + '-err'};
}

function labelEl(f, id, extra) {
  return h('label', {class: 'label', for: id}, f.label, f.required ? h('span', {class: 'req'}, ' (required)') : null, extra || null);
}

function baseControl(f, wrap) {
  const ids = fieldIds(f);
  const errEl = h('p', {class: 'field-err', id: ids.err, hidden: true});
  const helpEl = f.help ? h('p', {class: 'help', id: ids.help}, f.help) : null;
  return {field: f, wrap, errEl, helpEl, ids, isFile: false};
}

function describe(input, ctl, withErr) {
  const ids = [ctl.helpEl ? ctl.ids.help : null, withErr ? ctl.ids.err : null].filter(Boolean);
  if (ids.length) input.setAttribute('aria-describedby', ids.join(' '));
  else input.removeAttribute('aria-describedby');
}

function makeControl(tool, f) {
  if (f.type === 'file' || f.type === 'files') return makeFileControl(tool, f);
  if (f.type === 'checkbox') return makeCheckbox(f);
  const wide = f.type === 'textarea';
  const ctl = baseControl(f, h('div', {class: 'field' + (wide ? ' span' : '')}));
  const {id} = ctl.ids;
  let input;
  if (f.type === 'textarea') {
    input = h('textarea', {class: 'textarea', id, name: f.name, rows: 3, spellcheck: 'false'});
    input.addEventListener('input', () => grow(input));
    ctl.grow = () => grow(input);
  } else if (f.type === 'number') {
    let placeholder = null;
    if (f.name === 'seed') placeholder = 'Random';
    else if (f.default === null || f.default === undefined) placeholder = 'Empty';
    else if (f.min !== undefined || f.max !== undefined) placeholder = (f.min ?? '') + '-' + (f.max ?? '');
    input = h('input', {
      class: 'input num', type: 'number', id, name: f.name, inputmode: 'decimal',
      min: f.min ?? null, max: f.max ?? null, step: f.step ?? 'any', placeholder,
    });
  } else if (f.type === 'select') {
    input = h('select', {class: 'select', id, name: f.name},
      (f.options || []).map((o) => h('option', {value: o.value}, o.label ?? o.value)));
  } else {
    input = h('input', {class: 'input', type: 'text', id, name: f.name, spellcheck: 'false'});
  }
  if (f.required) input.setAttribute('aria-required', 'true');
  ctl.input = input;
  describe(input, ctl, false);

  if (f.type === 'textarea') {
    const tools = h('div', {class: 'improve-row'});
    const limit = TOKEN_LIMITS[tool.id + ':' + f.name];
    if (limit) {
      ctl.counter = h('span', {class: 'counter num', 'aria-live': 'polite'});
      ctl.limit = limit;
      tools.append(ctl.counter);
    }
    if (f.assist) {
      ctl.improveBtn = h('button', {type: 'button', class: 'btn btn-sm btn-improve', onclick: () => runImprove(tool, f)});
      ctl.improveNote = h('div', {class: 'improve-slot'});
      tools.append(ctl.improveBtn);
    }
    append(ctl.wrap, [h('div', {class: 'field-head'}, labelEl(f, id), tools), input, ctl.improveNote, ctl.errEl, ctl.helpEl]);
  } else {
    append(ctl.wrap, [labelEl(f, id), input, ctl.errEl, ctl.helpEl]);
  }

  ctl.get = () => input.value;
  ctl.set = (v) => {
    const str = v === null || v === undefined ? '' : String(v);
    if (f.type === 'select' && ![...input.options].some((o) => o.value === str)) return;
    input.value = str;
    if (ctl.grow) ctl.grow();
    if (ctl.counter) updateCounter(ctl);
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

function makeCheckbox(f) {
  const ctl = baseControl(f, h('div', {class: 'field' + (f.help ? ' span' : '')}));
  const {id} = ctl.ids;
  const input = h('input', {type: 'checkbox', id, name: f.name, role: 'switch'});
  ctl.input = input;
  describe(input, ctl, false);
  ctl.wrap.append(h('label', {class: 'check-row switch', for: id}, input, h('span', {class: 'track'}),
    h('span', {class: 'check-text'}, h('span', {class: 'label'}, f.label), ctl.helpEl)), ctl.errEl);
  ctl.get = () => input.checked;
  ctl.set = (v) => {
    input.checked = v === true || v === 'true' || v === 1;
  };
  ctl.reset = () => ctl.set(f.default === true);
  ctl.validate = () => null;
  ctl.focus = () => input.focus();
  ctl.reset();
  return ctl;
}

// ---------- file fields: drop zone, chosen files, previews ----------

function acceptKind(f) {
  const a = f.accept || '';
  return a.startsWith('image/') ? 'image' : a.startsWith('audio/') ? 'audio' : a.startsWith('video/') ? 'video' : null;
}

function accepts(f, file) {
  const kind = acceptKind(f);
  return !kind || (file.type || '').startsWith(kind + '/');
}

function makeFileControl(tool, f) {
  const multiple = f.type === 'files';
  const kind = acceptKind(f);
  const noun = NOUN[kind] || 'file';
  const ctl = baseControl(f, h('div', {class: 'field span'}));
  const {id} = ctl.ids;
  const count = multiple ? h('span', {class: 'req num'}) : null;
  const input = h('input', {type: 'file', id, name: f.name, accept: f.accept || null, multiple, class: 'sr-only', tabindex: '-1'});
  const list = h('div', {class: 'files'});
  const choose = h('button', {type: 'button', class: 'btn btn-sm', onclick: () => input.click()});
  const actions = h('div', {class: 'drop-actions'}, choose, h('span', null, 'or drop it here, or send one from Jobs'));
  const zone = h('div', {class: 'drop', role: 'group', 'aria-labelledby': id + '-label'}, list, actions);
  const label = h('span', {class: 'label', id: id + '-label'}, f.label, f.required ? h('span', {class: 'req'}, ' (required)') : null, count ? ' ' : null, count);
  let files = [];
  let urls = [];

  ctl.input = choose;
  describe(choose, ctl, false);

  function render() {
    for (const u of urls) URL.revokeObjectURL(u);
    urls = [];
    sessionFiles.set(ctl.key, files);
    list.replaceChildren();
    list.hidden = files.length === 0;
    files.forEach((file, i) => {
      const meta = h('span', {class: 'file-meta'});
      let thumb;
      if (file.type.startsWith('image/')) {
        const url = URL.createObjectURL(file);
        urls.push(url);
        thumb = h('span', {class: 'file-thumb'}, h('img', {src: url, alt: ''}));
      } else {
        const k = file.type.startsWith('audio/') ? 'audio' : file.type.startsWith('video/') ? 'video' : 'file';
        thumb = h('span', {class: 'file-thumb ' + k}, icon(k === 'audio' ? 'voice' : k === 'video' ? 'video' : 'file', 'ic-sm'));
      }
      const tag = multiple && kind === 'image' ? h('span', {class: 'ref-tag'}, '<ref_image_' + i + '>') : null;
      fillMeta(file, meta, tag);
      list.append(h('div', {class: 'file-chip'}, thumb,
        h('span', {class: 'file-info'}, h('span', {class: 'file-name', title: file.name}, file.name), meta),
        h('button', {type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Remove ' + file.name,
          onclick: () => {
            ctl.setFiles(files.filter((_, j) => j !== i));
            choose.focus();
          }}, icon('x', 'ic-sm'))));
    });
    const full = multiple ? f.max && files.length >= f.max : files.length > 0;
    choose.replaceChildren(icon('plus', 'ic-sm'), (multiple ? 'Choose ' + (files.length ? 'another ' : '') : files.length ? 'Replace the ' : 'Choose ') + noun);
    choose.hidden = !!(multiple && full);
    if (count) count.textContent = '(' + files.length + ' of ' + f.max + ')';
    if (multiple && f.max && files.length > f.max) {
      showFieldError(ctl, 'Choose at most ' + f.max + ' files (' + files.length + ' selected).');
    } else if (ctl.wrap.classList.contains('invalid') && ctl.validate() === null) {
      showFieldError(ctl, null);
    }
  }

  // Adds dropped or chosen files; returns an error text or null.
  function addMany(list) {
    let err = null;
    for (const file of list) {
      if (!accepts(f, file)) {
        err = file.name + ' is not ' + (noun === 'image' ? 'an image' : noun === 'audio file' ? 'an audio file' : 'a video') + '.';
        continue;
      }
      err = ctl.addFile(file) || err;
    }
    return err;
  }

  input.addEventListener('change', () => {
    const err = addMany([...input.files]);
    input.value = '';
    if (err) showFieldError(ctl, err);
  });
  zone.addEventListener('dragover', (e) => {
    if (![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault();
    zone.classList.add('over');
  });
  zone.addEventListener('dragleave', (e) => {
    if (!zone.contains(e.relatedTarget)) zone.classList.remove('over');
  });
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    const err = addMany([...e.dataTransfer.files]);
    if (err) showFieldError(ctl, err);
  });

  append(ctl.wrap, [label, input, zone, ctl.errEl, ctl.helpEl]);
  ctl.isFile = true;
  ctl.get = () => (multiple ? [...files] : files[0] || null);
  ctl.set = () => {};
  ctl.reset = () => ctl.setFiles([]);
  ctl.setFiles = (next) => {
    files = [...next];
    render();
  };
  // Single file fields replace, multi file fields append. Returns an error text or null.
  ctl.addFile = (file) => {
    if (!multiple) {
      ctl.setFiles([file]);
      return null;
    }
    if (f.max && files.length >= f.max) return f.label + ' already has ' + files.length + ' files (max ' + f.max + ').';
    ctl.setFiles([...files, file]);
    return null;
  };
  ctl.focus = () => choose.focus();
  ctl.validate = () => {
    const n = files.length;
    if (f.required && n === 0) return multiple ? 'Choose at least one file.' : 'Choose a file.';
    if (multiple && f.max && n > f.max) return 'Choose at most ' + f.max + ' files.';
    return null;
  };
  render();
  return ctl;
}

// Size, and pixel size or length once the browser has read the file.
function fillMeta(file, el, tag) {
  const show = (extra) => {
    el.replaceChildren(...[tag, tag ? ' · ' : null, fmtBytes(file.size), extra ? ' · ' + extra : null].filter(Boolean));
  };
  if (fileInfo.has(file)) return show(fileInfo.get(file));
  show(null);
  const done = (text) => {
    fileInfo.set(file, text);
    if (el.isConnected) show(text);
  };
  const url = URL.createObjectURL(file);
  const free = () => URL.revokeObjectURL(url);
  if (file.type.startsWith('image/')) {
    const img = new Image();
    img.onload = () => {
      done(img.naturalWidth + 'x' + img.naturalHeight);
      free();
    };
    img.onerror = free;
    img.src = url;
  } else if (file.type.startsWith('video/') || file.type.startsWith('audio/')) {
    const m = document.createElement(file.type.startsWith('video/') ? 'video' : 'audio');
    m.preload = 'metadata';
    m.onloadedmetadata = () => {
      const len = isFinite(m.duration) ? m.duration.toFixed(1) + ' s' : '';
      done([m.videoWidth ? m.videoWidth + 'x' + m.videoHeight : '', len].filter(Boolean).join(' · '));
      free();
    };
    m.onerror = free;
    m.src = url;
  } else {
    free();
  }
}

// ---------- values, saving, messages ----------

function onFormInput(e) {
  scheduleSave();
  const ctl = s.controls.get(e.target.name);
  if (ctl && ctl.counter) updateCounter(ctl);
  if (ctl && ctl.wrap.classList.contains('invalid') && ctl.validate() === null) showFieldError(ctl, null);
  const key = ctl && s.tool.id + ':' + ctl.field.name;
  if (ctl && ctl.improveBtn && ['empty', 'error', 'cancelled'].includes((improve.get(key) || {}).status)) {
    improve.delete(key);
    renderImprove(ctl);
  }
}

function updateCounter(ctl) {
  const words = (ctl.get().trim().match(/\S+/g) || []).length;
  const tokens = Math.round(words * 1.35);
  ctl.counter.textContent = 'about ' + tokens + ' of ' + ctl.limit + ' tokens';
  ctl.counter.classList.toggle('over', tokens > ctl.limit);
}

function updateCounters() {
  for (const ctl of s.controls.values()) if (ctl.counter) updateCounter(ctl);
}

function growAll() {
  requestAnimationFrame(() => {
    if (!s) return;
    for (const ctl of s.controls.values()) if (ctl.grow) ctl.grow();
  });
}

function applyValues(values) {
  for (const [name, v] of Object.entries(values)) {
    const ctl = s.controls.get(name);
    if (ctl && !ctl.isFile) ctl.set(v);
  }
}

function collectValues() {
  const values = {};
  for (const [name, ctl] of s.controls) if (!ctl.isFile) values[name] = ctl.get();
  return values;
}

function saveFormNow() {
  if (!s) return;
  clearTimeout(s.saveTimer);
  if (s.tool) lsSet(KEY.form(s.tool.id), collectValues());
}

function scheduleSave() {
  clearTimeout(s.saveTimer);
  s.saveTimer = setTimeout(saveFormNow, 300);
}

function showFieldError(ctl, msg) {
  ctl.errEl.replaceChildren(...(msg ? [icon('alert', 'ic-sm'), msg] : []));
  ctl.errEl.hidden = !msg;
  ctl.wrap.classList.toggle('invalid', !!msg);
  if (ctl.input) {
    if (msg) ctl.input.setAttribute('aria-invalid', 'true');
    else ctl.input.removeAttribute('aria-invalid');
    describe(ctl.input, ctl, !!msg);
  }
}

function showSummary(n) {
  const el = s.els.summary;
  el.hidden = !n;
  el.replaceChildren(...(n ? [icon('alert'), n === 1 ? 'Fix the field marked below.' : 'Fix the ' + n + ' fields marked below.'] : []));
}

function showSubmitError(msg) {
  const el = s.els.submitError;
  el.hidden = !msg;
  el.replaceChildren(...(msg ? [icon('alert'), h('div', {class: 'banner-body'}, h('strong', null, 'The job was not started'), h('span', null, msg))] : []));
}

function clearMessages() {
  for (const ctl of s.controls.values()) showFieldError(ctl, null);
  showSummary(0);
  showSubmitError(null);
}

function resetForm() {
  if (!s.tool) return;
  for (const ctl of s.controls.values()) ctl.reset();
  for (const name of s.controls.keys()) improve.delete(s.tool.id + ':' + name);
  lsRemove(KEY.form(s.tool.id));
  clearMessages();
  renderAllImprove();
  updateCounters();
  growAll();
  toast('Reset ' + s.tool.label + ' to the defaults.');
}

function buildFormData(tool) {
  const fd = new FormData();
  fd.append('tool', tool.id);
  for (const [name, ctl] of s.controls) {
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
  return fd;
}

// ---------- submit ----------

async function submit() {
  if (!s || !s.tool || s.submitting) return;
  const tool = s.tool;
  showSubmitError(null);
  let firstBad = null;
  let bad = 0;
  for (const ctl of s.controls.values()) {
    const msg = ctl.validate();
    showFieldError(ctl, msg);
    if (msg) {
      bad += 1;
      if (!firstBad) firstBad = ctl;
    }
  }
  showSummary(bad);
  if (firstBad) {
    if (firstBad.field.advanced && s.els.adv) s.els.adv.open = true;
    firstBad.focus();
    return;
  }

  const fd = buildFormData(tool);
  saveFormNow();
  const ahead = store.get('jobs').filter((j) => store.ACTIVE.has(j.status)).length;
  s.submitting = true;
  updateGenerate();
  try {
    await s.ctx.api.createJob(fd);
    toast(ahead ? tool.label + ' is in the queue (' + ahead + ' ahead).' : tool.label + ' starts now.',
      {action: {label: 'View in Jobs', run: () => s && s.ctx.router.go('jobs')}});
    store.refresh('jobs');
  } catch (e) {
    if (s) showSubmitError(e.offline ? 'Server offline. Try again when it is back.' : e.message);
  } finally {
    if (s) {
      s.submitting = false;
      updateGenerate();
    }
  }
}

// ---------- prompt help (Improve) ----------

function assistOn() {
  const a = store.get('assist');
  return !!(a && a.available);
}

async function runImprove(tool, f) {
  const key = tool.id + ':' + f.name;
  const ctl = s.controls.get(f.name);
  const cur = improve.get(key);
  if (cur && cur.status === 'working') return;
  if (ctl.get().trim() === '') {
    improve.set(key, {status: 'empty'});
    renderImprove(ctl);
    return;
  }
  const fd = buildFormData(tool);
  fd.append('field', f.name);
  const api = s.ctx.api;
  const router = s.ctx.router;
  const run = {id: crypto.randomUUID(), api, ctrl: new AbortController(), cancelled: false};
  fd.append('request_id', run.id);
  improveRuns.set(key, run);
  improve.set(key, {status: 'working'});
  renderImprove(ctl);
  const release = store.fast('llm');
  try {
    const res = await api.enhance(fd, {signal: run.ctrl.signal});
    let before;
    const live = s && s.tool && s.tool.id === tool.id ? s.controls.get(f.name) : null;
    if (live) {
      before = live.get();
      live.set(res.text);
      saveFormNow();
    } else {
      // The user left the view (choosing another tool cancels instead): the new text goes into the tool's saved
      // form, and Undo still works there. Going to Jobs while Improve waits for that job's memory is normal use.
      const saved = lsGet(KEY.form(tool.id)) || {};
      before = saved[f.name] ?? '';
      saved[f.name] = res.text;
      lsSet(KEY.form(tool.id), saved);
      toast('Prompt help finished for ' + tool.label + '.', {action: {label: 'Open', run: () => router.go('create', {tool: tool.id})}});
    }
    improve.set(key, {status: 'done', before, after: res.text, notes: res.notes, model: res.model, seconds: res.seconds, cost: res.cost_usd});
  } catch (e) {
    if (run.cancelled) {
      improve.set(key, {status: 'cancelled'});
    } else {
      const message = e.offline ? 'Server offline.' : e.message;
      improve.set(key, {status: 'error', message});
      if (!(s && s.tool && s.tool.id === tool.id)) toast('Prompt help for ' + tool.label + ' failed: ' + message, {kind: 'err'});
    }
  } finally {
    if (improveRuns.get(key) === run) improveRuns.delete(key);
    release();
    if (s && s.tool && s.tool.id === tool.id) renderImprove(s.controls.get(f.name));
  }
}

// Stops a running Improve on the server, whether it waits for memory, loads the model or writes, and drops its
// answer. The field keeps its text: Improve writes the field only when the answer comes, so there is nothing to undo.
function cancelImprove(key) {
  const run = improveRuns.get(key);
  if (!run) return;
  improveRuns.delete(key);
  run.cancelled = true;
  run.api.cancelEnhance(run.id).catch(() => {}); // the aborted fetch also cancels it when this call fails
  run.ctrl.abort();
}

function undoImprove(ctl, redo) {
  const key = s.tool.id + ':' + ctl.field.name;
  const st = improve.get(key);
  if (!st) return;
  ctl.set(redo ? st.after : st.before);
  saveFormNow();
  improve.set(key, Object.assign({}, st, {status: redo ? 'done' : 'undone'}));
  renderImprove(ctl);
  ctl.focus();
}

function note(kind, lead, body, small, action) {
  return h('div', {class: 'improve-note' + (kind ? ' is-' + kind : ''), role: kind === 'err' ? 'alert' : 'status'},
    lead, h('div', null, h('span', null, body), small ? h('small', null, small) : null), action || null);
}

function renderImprove(ctl) {
  if (!ctl || !ctl.improveBtn) return;
  const on = assistOn();
  ctl.improveBtn.hidden = !on;
  const key = s.tool.id + ':' + ctl.field.name;
  const st = improve.get(key) || {status: 'idle'};
  const working = st.status === 'working';
  ctl.improveBtn.disabled = working;
  ctl.improveBtn.replaceChildren(...(working ? [h('span', {class: 'spinner sm'}), 'Improving'] : [icon('wand', 'ic-sm'), 'Improve']));
  const slot = ctl.improveNote;
  // The note is rebuilt on every LLM poll: keep the keyboard focus on Cancel.
  const focused = slot.contains(document.activeElement) ? document.activeElement.dataset.improve : null;
  slot.replaceChildren();
  if (!on && st.status !== 'done' && st.status !== 'undone') return;
  const llm = store.get('llm') || {};
  const model = (store.get('assist') || {}).model || llm.model || 'The local model';
  const label = ctl.field.label.toLowerCase();
  if (working) {
    const cancel = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', 'data-improve': 'cancel',
      'aria-label': 'Cancel Improve for ' + ctl.field.label, onclick: () => {
        cancelImprove(key);
        ctl.focus();
      }}, icon('x', 'ic-sm'), 'Cancel');
    if (llm.waiting > 0) {
      const run = store.runningJob();
      const left = run && run.progress ? fmtLeft(run.progress.eta_s) : '';
      slot.append(note('wait', h('span', {class: 'spinner'}),
        'Waiting for memory. ' + model + ' does not fit next to ' + (run ? run.tool_label || run.tool : 'the running job') + '.',
        'Improve runs as soon as that job ends' + (left ? ' (' + left + ')' : '') + ', before the next queued job. Your text stays here.', cancel));
    } else if (llm.state === 'loading' || llm.state === 'off') {
      slot.append(note('wait', h('span', {class: 'spinner'}), 'Loading ' + model + ' into memory.',
        'The first request reads about ' + Math.round(llm.peak_gb || 16) + ' GB from disk. Your text stays here until the new one is ready.', cancel));
    } else {
      slot.append(note('wait', h('span', {class: 'spinner'}), model + ' is rewriting your ' + label + '.',
        'Your text stays here until the new one is ready.', cancel));
    }
    if (focused === 'cancel') cancel.focus();
  } else if (st.status === 'cancelled') {
    slot.append(note('quiet', icon('x'), 'Cancelled. Your text is unchanged.'));
  } else if (st.status === 'done') {
    const how = typeof st.cost === 'number' ? 'about $' + st.cost.toFixed(3) : typeof st.seconds === 'number' ? Math.round(st.seconds) + ' s' : '';
    slot.append(note('', icon('wand'), st.notes || 'Rewrote the ' + label + ' for this tool.',
      [st.model, how, 'your text is kept for Undo'].filter(Boolean).join(' · '),
      h('button', {type: 'button', class: 'btn btn-sm', onclick: () => undoImprove(ctl, false)}, icon('undo', 'ic-sm'), 'Undo')));
  } else if (st.status === 'undone') {
    slot.append(note('quiet', icon('undo'), 'Restored your text.', null,
      h('button', {type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => undoImprove(ctl, true)}, icon('redo', 'ic-sm'), 'Redo')));
  } else if (st.status === 'empty') {
    slot.append(note('err', icon('alert'), 'Write a short idea first. Improve turns a few words into the full text this tool needs.'));
  } else if (st.status === 'error') {
    slot.append(note('err', icon('alert'), st.message));
  }
}

function renderAllImprove() {
  if (!s || !s.tool) return;
  for (const ctl of s.controls.values()) renderImprove(ctl);
}

// ---------- rail: pre-flight card, Generate, last result ----------

function buildRail() {
  const els = s.els;
  els.pre = h('section', {class: 'card pre', 'aria-labelledby': 'pre-h'});
  els.generate = h('button', {type: 'button', class: 'btn btn-primary btn-lg btn-block', onclick: submit}, 'Generate');
  els.genWhy = h('p', {class: 'help', id: 'gen-why', hidden: true});
  els.reset = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', onclick: resetForm}, 'Reset to defaults');
  els.submitError = h('div', {class: 'banner banner-err', role: 'alert', hidden: true});
  els.last = h('div', {class: 'last-slot'});
  els.rail.append(els.pre,
    h('div', {class: 'gen'}, els.generate, els.genWhy,
      h('div', {class: 'gen-hint'}, h('span', null, h('span', {class: 'kbd'}, '⌘'), ' ', h('span', {class: 'kbd'}, 'Return')), els.reset),
      els.submitError),
    els.last);
}

function updateGenerate() {
  const els = s.els;
  const tool = s.tool;
  const installed = tool ? store.toolInstalled(tool.id) : null;
  const busy = store.get('jobs').some((j) => store.ACTIVE.has(j.status));
  els.generate.disabled = !tool || s.submitting;  // the server checks the files and says what is missing
  els.reset.disabled = !tool;
  els.generate.replaceChildren(...(s.submitting ? [h('span', {class: 'spinner'}), 'Submitting'] : [busy ? 'Add to queue' : 'Generate']));
  els.genWhy.hidden = installed !== false;
  if (installed === false) {
    els.genWhy.textContent = 'The Models screen shows a model of this tool as not installed, so the job may fail.';
    els.generate.setAttribute('aria-describedby', 'gen-why');
  } else {
    els.generate.removeAttribute('aria-describedby');
  }
}

function preRow(iconName, ...body) {
  return h('div', {class: 'pre-row'}, icon(iconName), h('div', null, body));
}

function renderPre() {
  const tool = s.tool;
  const f = toolFacts(tool.id);
  const rows = [h('h3', {id: 'pre-h'}, 'On this Mac')];
  if (f.peak !== null && f.mem && f.fit) {
    const reserveGb = store.reserve();
    const pct = Math.min(100, (f.peak / f.mem) * 100);
    const limit = Math.max(0, ((f.mem - reserveGb) / f.mem) * 100);
    let memNote;
    if (f.fit.k === 'ok') memNote = 'Leaves about ' + Math.max(0, Math.round(f.mem - f.peak)) + ' GB for macOS and other apps.';
    else if (f.fit.k === 'tight') memNote = 'Quit other large apps before you start, or macOS will swap and the job slows down.';
    else memNote = 'This Mac has ' + Math.round(f.mem) + ' GB. macOS will swap a lot: the job can take many times longer or stop.';
    rows.push(preRow('chip',
      h('div', {class: 'pre-line'}, h('strong', null, f.peak ? 'Peak about ' + Math.round(f.peak) + ' GB' : 'Peak under 1 GB'),
        h('span', {class: 'pill pill-' + f.fit.pill}, f.fit.short)),
      h('div', {class: 'fitbar fit-' + f.fit.k, role: 'img', 'aria-label': 'Peak ' + fmtGb(f.peak, 0) + ' of ' + fmtGb(f.mem, 0)},
        h('span', {style: {width: pct + '%'}}), h('i', {class: 'limit', style: {left: limit + '%'}})),
      h('div', {class: 'fitscale num'}, h('span', null, '0'), h('span', null, 'safe limit ' + Math.round(f.mem - reserveGb) + ' GB'), h('span', null, Math.round(f.mem) + ' GB')),
      h('p', null, memNote, f.peak ? ' Peaks were measured on a 48 GB M5 Pro.' : '')));
  } else {
    rows.push(preRow('chip', h('strong', null, 'Peak memory'), h('p', null, 'Loading the model list...')));
  }
  rows.push(f.time
    ? preRow('clock', h('strong', null, fmtAbout(f.time.seconds)),
      h('p', null, 'Median of ' + f.time.count + ' finished ' + (f.time.count === 1 ? 'job' : 'jobs') + ' on this Mac. Longer or larger settings take longer.'))
    : preRow('clock', h('strong', null, 'No time yet'), h('p', null, tool.description ? 'See the description above. The first job on this Mac sets the estimate.' : 'The first job on this Mac sets the estimate.')));

  const run = store.runningJob();
  const queued = store.queuedJobs();
  if (!run && !queued.length) {
    rows.push(preRow('jobs', h('strong', null, 'Starts now'), h('p', null, 'Nothing else is running.')));
  } else {
    const wait = store.queueSeconds();
    const parts = [run ? 'After ' + (run.tool_label || run.tool) : 'After', queued.length ? (run ? ' and ' : ' ') + queued.length + ' queued job' + (queued.length > 1 ? 's' : '') : ''];
    rows.push(preRow('jobs', h('strong', null, wait !== null ? 'Starts in ' + fmtAbout(wait) : 'Waits in the queue'),
      h('p', null, parts.join('') + '. One job runs at a time.')));
  }
  const llm = store.get('llm');
  if (llm && llm.installed && store.llmUnloadsFor(f.peak)) {
    rows.push(preRow('chat', h('strong', null, 'Chat pauses'),
      h('p', null, llm.model + ' unloads while this job runs and loads again after it. Prompt help waits until then.')));
  }
  rows.push(preRow('folder', h('p', null, 'Saves to ', h('code', null, 'outputs/<job id>/'))));
  s.els.pre.replaceChildren(...rows);
}

function renderLast() {
  const tool = s.tool;
  const job = store.get('jobs').find((j) => j.tool === tool.id && j.status === 'done' && (j.outputs || []).length);
  const sig = job ? job.id : null;
  if (sig === s.lastSig) return;
  s.lastSig = sig;
  s.els.last.replaceChildren();
  if (!job) return;
  const o = job.outputs[0];
  const url = s.ctx.api.safeFileUrl(o.url);
  const thumb = o.kind === 'image' && url
    ? h('span', {class: 'last-thumb'}, h('img', {src: url, alt: '', loading: 'lazy'}))
    : h('span', {class: 'file-thumb ' + o.kind, style: {width: '44px', height: '44px'}}, icon(o.kind === 'audio' ? 'voice' : o.kind === 'video' ? 'video' : 'file'));
  s.els.last.append(h('section', {class: 'card last', 'aria-label': 'Last result from this tool'},
    h('span', {class: 'eyebrow'}, 'Last result from this tool'),
    h('button', {type: 'button', class: 'last-row', onclick: () => s.ctx.router.go('jobs')}, thumb,
      h('span', {class: 'file-info'}, h('span', {class: 'file-name'}, o.name + (job.outputs.length > 1 ? ' + ' + (job.outputs.length - 1) : '')),
        h('span', {class: 'file-meta'}, fmtTime(job.finished || job.created) + (typeof job.elapsed === 'number' ? ' · took ' + fmtClock(job.elapsed) : '')))),
    h('button', {type: 'button', class: 'btn btn-sm', onclick: () => useSettings(job)}, icon('undo', 'ic-sm'), 'Use its settings')));
}

// Re-renders everything that depends on polled facts, without rebuilding the form.
function refreshFacts() {
  if (!s) return;
  updatePicker();
  renderSetup();
  if (!s.tool) return;
  renderHead();
  renderPre();
  renderLast();
  updateGenerate();
}

// First run: no model is installed yet (docs/DESIGN.md decision 11).
function renderSetup() {
  const models = store.get('models');
  const none = models && !models.models.some((m) => m.kind === 'download' && m.installed && (m.tools || []).length);
  s.els.setup.replaceChildren();
  if (!none) return;
  s.els.setup.append(h('div', {class: 'banner banner-info'}, icon('info'),
    h('div', {class: 'banner-body'}, h('strong', null, 'No models installed yet'),
      h('span', null, 'Workstation does not ship the models. Pick the ones that fit this Mac and it downloads them for you. Mix needs no model and works now.'),
      h('div', {class: 'banner-actions'}, h('button', {type: 'button', class: 'btn btn-primary btn-sm', onclick: () => s.ctx.router.go('models', {setup: '1'})}, 'Set up models')))));
}

// ---------- handoff: settings, text and files from other views ----------

function useSettings(job) {
  if (!s.tool || s.tool.id !== job.tool) {
    s.ctx.handoff.useSettings(job);
    return;
  }
  applyValues(job.params || {});
  const advChanged = [...s.controls.values()].some((c) => c.field.advanced && !c.isFile && job.params
    && Object.hasOwn(job.params, c.field.name) && fmtValue(c.get()) !== fmtValue(c.field.default));
  if (advChanged && s.els.adv) s.els.adv.open = true;
  saveFormNow();
  clearMessages();
  updateCounters();
  growAll();
  const fileFields = [...s.controls.values()].filter((c) => c.isFile && c.field.required).map((c) => c.field.label);
  toast('Loaded the settings of job ' + job.id + '.' + (fileFields.length ? ' Add the files again: ' + fileFields.join(', ') + '.' : ''));
  s.root.closest('.views').scrollTo({top: 0, behavior: 'smooth'});
}

function takeHandoff(toolId) {
  for (const item of s.ctx.handoff.take(toolId)) {
    if (item.type === 'settings') useSettings(item.job);
    else if (item.type === 'text') useText(item);
    else if (item.type === 'file') sendFile(item);
  }
}

function useText(item) {
  const ctl = s.controls.get(item.field);
  if (!ctl || ctl.isFile) return;
  ctl.set(item.text);
  saveFormNow();
  if (ctl.field.advanced && s.els.adv) s.els.adv.open = true;
  toast('Put the text into ' + s.tool.label + ' › ' + ctl.field.label + '.');
  ctl.focus();
}

async function sendFile(item) {
  const tool = s.tool;
  let blob;
  try {
    const res = await fetch(item.file.url, {cache: 'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    blob = await res.blob();
  } catch (e) {
    toast('Could not load ' + item.file.name + ': ' + e.message, {kind: 'err'});
    return;
  }
  if (!s || s.tool !== tool) return;
  const ctl = s.controls.get(item.field);
  if (!ctl || !ctl.isFile) {
    toast(tool.label + ' › ' + item.field + ' is not available.', {kind: 'err'});
    return;
  }
  const err = ctl.addFile(new File([blob], item.file.name, {type: blob.type}));
  if (err) {
    toast(err, {kind: 'err'});
    return;
  }
  if (ctl.field.advanced && s.els.adv) s.els.adv.open = true;
  toast('Sent ' + item.file.name + ' to ' + tool.label + ' › ' + ctl.field.label + '.');
  ctl.wrap.scrollIntoView({block: 'center', behavior: 'smooth'});
}

export default {
  id: 'create',
  title: 'Create',
  subtitle: (ctx) => {
    const n = ctx.store.get('tools').length;
    return (n ? n + ' tools. ' : '') + 'Everything runs on this Mac.';
  },
  mount,
  update,
  unmount,
};
