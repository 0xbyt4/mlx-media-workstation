// Library: every output of finished jobs (GET /api/library) as a grid with search, type filter, tool filter and sort.
// A card opens a detail drawer with a large preview, Use these settings, Send to, Download, Show in Finder, delete,
// the prompt, the settings and the file path. Arrow keys move between cards; Escape closes the drawer.
// Route params {job, name} open that output (#/library?job=...&name=...).

import {store} from '../store.js';
import {
  h, icon, toolIcon, fmtBytes, fmtClock, fmtTime, fmtValue, toast, inlineConfirm, emptyState, errorBox, media,
  lightbox,
} from '../ui.js';

const KINDS = [['all', 'All'], ['image', 'Images'], ['video', 'Video'], ['audio', 'Audio']];
const TEXT_KEYS = ['prompt', 'text', 'caption'];
const TEXT_LABEL = {prompt: 'Prompt', text: 'Script', caption: 'Description'};
const KIND_ICON = {image: 'image', audio: 'voice', video: 'video'};
const WAVE_BARS = 28;

// Kept for the session: the filters the viewer chose.
const prefs = {q: '', kind: 'all', tool: 'all', sort: 'new'};

let s = null;

const keyOf = (item) => item.job + '/' + item.name;

// ---------- mount ----------

function mount(root, ctx, params) {
  root.classList.add('v-library');
  s = {
    root, ctx, items: null, byKey: new Map(), cards: new Map(), offs: [], seq: 0, active: null, open: null,
    want: wantOf(params), jobsSig: jobsSig(store.get('jobs')), audio: new Audio(), playing: null, searchTimer: null,
  };
  s.audio.preload = 'none';
  s.audio.addEventListener('play', () => markPlaying(s && s.playing, true));
  s.audio.addEventListener('pause', () => markPlaying(s && s.playing, false));
  s.audio.addEventListener('timeupdate', audioProgress);
  s.audio.addEventListener('ended', () => setPlaying(null));
  s.audio.addEventListener('error', () => {
    if (s && s.playing && s.audio.getAttribute('src')) toast('Could not play ' + s.playing.split('/').pop() + '.', {kind: 'err'});
    setPlaying(null);
  });
  s.io = new IntersectionObserver(loadVisible, {rootMargin: '300px 0px'});

  s.search = h('input', {class: 'input', type: 'search', placeholder: 'Search prompts, tools, file names',
    'aria-label': 'Search the library', value: prefs.q, autocomplete: 'off', spellcheck: 'false'});
  s.search.addEventListener('input', () => {
    clearTimeout(s.searchTimer);
    s.searchTimer = setTimeout(() => {
      prefs.q = s.search.value;
      render();
    }, 120);
  });
  s.seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Type'},
    KINDS.map(([k, label]) => h('button', {type: 'button', 'data-kind': k, onclick: () => {
      prefs.kind = k;
      render();
    }}, label)));
  s.toolSel = h('select', {class: 'select', 'aria-label': 'Tool', onchange: () => {
    prefs.tool = s.toolSel.value;
    render();
  }});
  s.sortSel = h('select', {class: 'select', 'aria-label': 'Sort', onchange: () => {
    prefs.sort = s.sortSel.value;
    render();
  }}, h('option', {value: 'new'}, 'Newest first'), h('option', {value: 'old'}, 'Oldest first'));
  s.sortSel.value = prefs.sort;
  s.count = h('span', {class: 'lib-count num', 'aria-live': 'polite'});
  s.tools = h('div', {class: 'lib-tools'},
    h('div', {class: 'lib-search'}, icon('search', 'ic-sm'), s.search), s.seg, s.toolSel, s.sortSel, s.count);
  s.grid = h('div', {class: 'lib-grid', role: 'list', 'aria-label': 'Outputs'});
  s.grid.addEventListener('keydown', gridKey);
  s.body = h('div', {class: 'lib-body'});
  s.drawerSlot = h('div');
  root.append(h('div', {class: 'lib'}, s.tools, s.body), s.drawerSlot);

  s.offs.push(
    store.on('jobs', (jobs) => {
      const sig = jobsSig(jobs);
      if (sig !== s.jobsSig) {
        s.jobsSig = sig;
        load();
      }
    }),
    store.on('tools', () => {
      if (s.items) render(true);
    }),
  );
  showLoading();
  load();
}

function unmount() {
  if (!s) return;
  for (const off of s.offs) off();
  clearTimeout(s.searchTimer);
  s.io.disconnect();
  const audio = s.audio;
  s.seq++;
  s = null;
  audio.pause();
  audio.removeAttribute('src');
}

function update(params) {
  if (!s) return;
  const want = wantOf(params);
  if (want === (s.open ? keyOf(s.open.item) : null)) return;
  if (!want) closeDrawer(true);
  else if (s.items) openKey(want);
  else s.want = want;
}

function wantOf(params) {
  return params && params.job && params.name ? params.job + '/' + params.name : null;
}

// Done jobs and their output names: the library reloads when this changes (a job finished or was deleted).
function jobsSig(jobs) {
  return (jobs || []).filter((j) => j.status === 'done')
    .map((j) => j.id + ':' + (j.outputs || []).map((o) => o.name).join(',')).join('|');
}

// ---------- data ----------

async function load() {
  const mine = ++s.seq;
  let items;
  try {
    items = await s.ctx.api.request('/api/library');
  } catch (e) {
    if (!s || mine !== s.seq) return;
    if (s.items) {
      toast('Could not refresh the library: ' + e.message, {kind: 'err'});
      return;
    }
    s.body.replaceChildren(errorBox({
      title: e.offline ? 'The app server is not answering' : 'The library could not load',
      text: e.message,
      actions: [h('button', {type: 'button', class: 'btn btn-sm', onclick: () => {
        showLoading();
        load();
      }}, icon('refresh', 'ic-sm'), 'Try again')],
    }));
    return;
  }
  if (!s || mine !== s.seq) return;
  s.items = (Array.isArray(items) ? items : []).filter((i) => s.ctx.api.safeFileUrl(i.url));
  s.byKey = new Map(s.items.map((i) => [keyOf(i), i]));
  if (s.playing && !s.byKey.has(s.playing)) setPlaying(null);
  render(true);
  if (s.open && !s.byKey.has(keyOf(s.open.item))) closeDrawer(false);
  else if (s.open) s.open.item = s.byKey.get(keyOf(s.open.item));
  if (s.want) {
    const want = s.want;
    s.want = null;
    openKey(want);
  }
}

function showLoading() {
  s.body.replaceChildren(h('div', {class: 'lib-loading', role: 'status'}, h('span', {class: 'spinner'}), 'Loading the library'));
}

function tool(item) {
  return store.get('toolsById').get(item.tool) || {id: item.tool, label: item.tool_label || item.tool, group: 'Edit', fields: []};
}

function filtered() {
  const q = prefs.q.trim().toLowerCase();
  const list = s.items.filter((i) => {
    if (prefs.kind !== 'all' && i.kind !== prefs.kind) return false;
    if (prefs.tool !== 'all' && i.tool !== prefs.tool) return false;
    if (!q) return true;
    const strings = Object.values(i.params || {}).filter((v) => typeof v === 'string');
    return [i.text, i.tool_label, tool(i).label, i.name, ...strings].some((v) => v && v.toLowerCase().includes(q));
  });
  return prefs.sort === 'old' ? list.reverse() : list;
}

// ---------- rendering ----------

// dataChanged: new items or tools arrived (the tool list and the cards are checked again).
function render(dataChanged) {
  if (!s || !s.items) return;
  const all = s.items;
  s.ctx.shell.setSubtitle(all.length + (all.length === 1 ? ' output' : ' outputs') + ' in outputs/');
  for (const b of s.seg.children) b.setAttribute('aria-pressed', String(b.getAttribute('data-kind') === prefs.kind));
  if (dataChanged) {
    fillTools();
    for (const [k, card] of s.cards) {
      if (!s.byKey.has(k)) dropCard(k, card);
    }
  }
  s.tools.hidden = !all.length;

  if (!all.length) {
    s.body.replaceChildren(emptyState({icon: 'library', title: 'No outputs yet',
      text: 'Images, voices, music and videos you make in Create appear here when their job is done.',
      actions: [h('button', {type: 'button', class: 'btn btn-primary btn-sm', onclick: () => s.ctx.router.go('create')}, 'Go to Create')]}));
    return;
  }
  const list = filtered();
  s.count.textContent = list.length === all.length ? all.length + ' outputs' : list.length + ' of ' + all.length + ' outputs';
  if (!list.length) {
    const q = prefs.q.trim();
    s.body.replaceChildren(emptyState({icon: 'search', title: q ? 'No outputs match "' + q + '"' : 'No outputs match these filters',
      text: 'The search looks in prompts, scripts, descriptions, tool names and file names.',
      actions: [h('button', {type: 'button', class: 'btn btn-sm', onclick: clearFilters}, 'Clear search and filters')]}));
    return;
  }
  const focused = s.grid.contains(document.activeElement) ? document.activeElement.closest('.lib-card') : null;
  const focusKey = focused && focused.getAttribute('data-key');
  const cards = list.map((item) => {
    const k = keyOf(item);
    const sig = cardSig(item);
    const old = s.cards.get(k);
    if (!old || old.sig !== sig) {
      if (old) dropCard(k, old);
      s.cards.set(k, Object.assign(buildCard(item), {sig}));
    }
    return s.cards.get(k).el;
  });
  if (s.grid.parentNode !== s.body) s.body.replaceChildren(s.grid);
  const same = cards.length === s.grid.children.length && cards.every((c, i) => s.grid.children[i] === c);
  if (!same) s.grid.replaceChildren(...cards);
  if (focusKey && s.cards.has(focusKey) && !s.grid.contains(document.activeElement)) s.cards.get(focusKey).open.focus();
  const keys = list.map(keyOf);
  setActive(keys.includes(s.active) ? s.active : keys[0], false);
}

// A card is built again only when its output or its tool's label or group changed.
function cardSig(item) {
  const t = tool(item);
  return JSON.stringify(item) + '|' + t.label + '|' + t.group;
}

function dropCard(k, card) {
  const v = card.el.querySelector('video');
  if (v) s.io.unobserve(v);
  s.cards.delete(k);
}

function fillTools() {
  const seen = new Map();
  for (const i of s.items) if (!seen.has(i.tool)) seen.set(i.tool, tool(i).label || i.tool_label || i.tool);
  if (prefs.tool !== 'all' && !seen.has(prefs.tool)) prefs.tool = 'all';
  const opts = [...seen].sort((a, b) => a[1].localeCompare(b[1]));
  s.toolSel.replaceChildren(h('option', {value: 'all'}, 'All tools'), ...opts.map(([id, label]) => h('option', {value: id}, label)));
  s.toolSel.value = prefs.tool;
}

function clearFilters() {
  prefs.q = '';
  prefs.kind = 'all';
  prefs.tool = 'all';
  s.search.value = '';
  s.toolSel.value = 'all';
  render();
  s.search.focus();
}

function metaLine(item) {
  const bits = [fmtTime(item.created)];
  if (item.duration) bits.push(fmtClock(item.duration));
  else if (item.width && item.height) bits.push(item.width + '×' + item.height);
  bits.push(fmtBytes(item.size));
  return bits.join(' · ');
}

function buildCard(item) {
  const t = tool(item);
  const k = keyOf(item);
  const label = item.tool_label || t.label;
  let thumb;
  if (item.kind === 'image') {
    thumb = h('img', {src: item.url, alt: '', loading: 'lazy', decoding: 'async'});
    thumb.addEventListener('error', () => thumb.replaceWith(placeholder(item)), {once: true});
  } else if (item.kind === 'video') {
    // the source is set when the card comes near the screen; preload=metadata then shows the first frame
    thumb = h('video', {muted: true, playsinline: true, preload: 'metadata', 'data-src': item.url + '#t=0.1',
      'aria-hidden': 'true', tabindex: '-1'});
    thumb.addEventListener('error', () => thumb.replaceWith(placeholder(item)), {once: true});
    s.io.observe(thumb);
  } else {
    thumb = audioThumb(item, t);
  }
  const open = h('button', {type: 'button', class: 'lib-open', tabindex: '-1', 'data-key': k,
    'aria-label': label + ', ' + item.kind + ', ' + item.name + (item.text ? ': ' + item.text.slice(0, 120) : ''),
    onclick: () => openKey(k)},
  h('span', {class: 'thumb k-' + item.kind}, thumb,
    h('span', {class: 'kind-tag', 'aria-hidden': 'true'}, item.kind),
    item.kind === 'video' && item.duration ? h('span', {class: 'lib-dur num', 'aria-hidden': 'true'}, fmtClock(item.duration)) : null,
    item.kind === 'audio' ? h('span', {class: 'lib-prog', 'aria-hidden': 'true'}, h('i')) : null),
  h('span', {class: 'lib-meta'}, h('b', null, label),
    h('span', {class: 'lib-text'}, item.text || item.note || item.name),
    h('small', null, metaLine(item))));
  const el = h('div', {class: 'lib-card', role: 'listitem', 'data-key': k}, open);
  if (item.kind === 'audio') {
    el.append(h('button', {type: 'button', class: 'lib-play', tabindex: '-1', 'data-name': item.name,
      'aria-label': 'Play ' + item.name, 'aria-pressed': 'false', onclick: () => togglePlay(item)}, icon('play', 'ic-sm fill')));
  }
  const card = {el, open};
  if (s.playing === k) queueMicrotask(() => markPlaying(k, !s.audio.paused));
  return card;
}

function placeholder(item) {
  return h('span', {class: 'lib-ph'}, icon(KIND_ICON[item.kind] || 'file', 'ic-lg'));
}

// A stand-in waveform: bar heights come from the file key, so a file always looks the same.
function audioThumb(item, t) {
  let seed = 0;
  for (const c of keyOf(item)) seed = (seed * 31 + c.charCodeAt(0)) >>> 0;
  const bars = [];
  for (let i = 0; i < WAVE_BARS; i++) {
    seed = (seed * 1103515245 + 12345) >>> 0;
    const env = Math.sin(Math.PI * (i + 0.5) / WAVE_BARS);
    bars.push(h('i', {style: {height: Math.round(18 + 70 * env * (0.35 + ((seed >>> 16) % 1000) / 1540)) + '%'}}));
  }
  const group = t.group === 'Voice' ? 'voice' : t.group === 'Music' ? 'music' : 'edit';
  return h('span', {class: 'audio-thumb a-' + group}, icon(group === 'edit' ? 'music' : group), h('span', {class: 'wave'}, bars));
}

function loadVisible(entries) {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const v = e.target;
    s.io.unobserve(v);
    if (v.dataset.src) {
      v.src = v.dataset.src;
      delete v.dataset.src;
    }
  }
}

// ---------- grid keyboard (roving tabindex) ----------

function cardEls() {
  return [...s.grid.children];
}

function setActive(k, focus) {
  if (!k) return;
  s.active = k;
  for (const el of cardEls()) {
    const on = el.getAttribute('data-key') === k;
    el.querySelector('.lib-open').tabIndex = on ? 0 : -1;
    const p = el.querySelector('.lib-play');
    if (p) p.tabIndex = on ? 0 : -1;
    if (on && focus) el.querySelector('.lib-open').focus();
  }
}

function columns(els) {
  if (els.length < 2) return 1;
  const top = els[0].offsetTop;
  const n = els.findIndex((el) => el.offsetTop !== top);
  return n < 0 ? els.length : n;
}

function gridKey(e) {
  const card = e.target.closest('.lib-card');
  if (!card || e.altKey || e.metaKey || e.ctrlKey) return;
  const els = cardEls();
  const i = els.indexOf(card);
  const cols = columns(els);
  const moves = {
    ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: i + cols, ArrowUp: i - cols, Home: 0, End: els.length - 1,
    PageDown: i + cols * 3, PageUp: i - cols * 3,
  };
  if (!(e.key in moves)) return;
  e.preventDefault();
  const j = Math.max(0, Math.min(els.length - 1, moves[e.key]));
  setActive(els[j].getAttribute('data-key'), true);
}

// ---------- audio in the grid ----------

function togglePlay(item) {
  const k = keyOf(item);
  setActive(k, false);
  if (s.playing === k) {
    if (s.audio.paused) s.audio.play().catch(() => {});
    else s.audio.pause();
    return;
  }
  setPlaying(k);
  s.audio.src = item.url;
  s.audio.play().catch(() => {});
}

function setPlaying(k) {
  if (!s) return;
  const prev = s.playing;
  if (prev && prev !== k) {
    s.audio.pause();
    markPlaying(prev, false);
    const bar = s.cards.has(prev) && s.cards.get(prev).el.querySelector('.lib-prog i');
    if (bar) bar.style.width = '0';
  }
  s.playing = k;
  if (!k) {
    s.audio.pause();
    s.audio.removeAttribute('src');
  }
}

function markPlaying(k, on) {
  if (!s || !k || !s.cards.has(k)) return;
  const el = s.cards.get(k).el;
  const btn = el.querySelector('.lib-play');
  if (!btn) return;
  const name = btn.getAttribute('data-name');
  btn.setAttribute('aria-pressed', String(on));
  btn.setAttribute('aria-label', (on ? 'Pause ' : 'Play ') + name);
  btn.replaceChildren(icon(on ? 'pause' : 'play', 'ic-sm fill'));
  el.classList.toggle('playing', on);
}

function audioProgress() {
  if (!s || !s.playing || !s.cards.has(s.playing)) return;
  const d = s.audio.duration;
  const bar = s.cards.get(s.playing).el.querySelector('.lib-prog i');
  if (bar && d && isFinite(d)) bar.style.width = (100 * s.audio.currentTime / d).toFixed(1) + '%';
}

// ---------- detail drawer ----------

function openKey(k) {
  const item = s.byKey.get(k);
  if (!item) {
    toast('That output is not in the library any more.', {kind: 'err'});
    s.ctx.router.replace('library');
    return;
  }
  openDrawer(item);
}

function textKey(params) {
  return TEXT_KEYS.find((k) => typeof params[k] === 'string' && params[k].trim()) || null;
}

function fieldValue(field, key, v) {
  if (v === true) return 'on';
  if (v === false) return 'off';
  if (v === null || v === undefined || v === '') return key === 'seed' ? 'random' : '-';
  if (field && field.options) {
    const o = field.options.find((x) => String(x.value) === String(v));
    if (o) return o.label;
  }
  return fmtValue(v);
}

function sendLabel(tg, targets) {
  // a tool with two file fields for this kind (Drama voice references) names the field too
  return 'Send to ' + tg.label + (targets.filter((x) => x.tool === tg.tool).length > 1 ? ' · ' + tg.sub : '');
}

function openDrawer(item) {
  const k = keyOf(item);
  const t = tool(item);
  const label = item.tool_label || t.label;
  const back = s.open ? s.open.back : document.activeElement;
  closeDrawer(false, true);
  setPlaying(null);
  setActive(k, false);

  const closeBtn = h('button', {type: 'button', class: 'btn btn-ghost btn-icon', 'aria-label': 'Close', onclick: () => closeDrawer(true)}, icon('x'));
  const errSlot = h('div', {class: 'field-err', role: 'alert', hidden: true});
  const confirmSlot = h('div');
  const params = item.params || {};
  const tk = textKey(params);

  const preview = media(item, {onOpen: () => openImages(item)});
  const targets = s.ctx.handoff.targets(item.kind);
  const sendRow = targets.length ? h('div', {class: 'lib-row'}, targets.map((tg) => h('button', {type: 'button', class: 'btn btn-sm',
    onclick: () => s.ctx.handoff.send({tool: tg.tool, field: tg.field, file: {url: item.url, name: item.name, kind: item.kind}})},
  icon('arrow', 'ic-sm'), sendLabel(tg, targets)))) : null;
  const known = store.get('toolsById').has(item.tool);
  const useBtn = h('button', {type: 'button', class: 'btn btn-primary', disabled: !known,
    title: known ? null : 'This tool is not available',
    onclick: () => s.ctx.handoff.useSettings({id: item.job, tool: item.tool, params})}, icon('undo', 'ic-sm'), 'Use these settings');
  const download = h('a', {class: 'btn btn-sm', href: item.url, download: item.name}, icon('download', 'ic-sm'), 'Download');
  const reveal = h('button', {type: 'button', class: 'btn btn-sm', onclick: () => revealItem(item, errSlot)}, icon('reveal', 'ic-sm'), 'Show in Finder');
  const del = h('button', {type: 'button', class: 'btn btn-sm btn-danger'}, icon('trash', 'ic-sm'), 'Delete');
  del.addEventListener('click', () => {
    del.hidden = true;
    confirmSlot.replaceChildren(inlineConfirm({
      text: 'Delete ' + item.name + '? The file is removed from outputs/' + item.job + '/ and cannot be restored.',
      confirmLabel: 'Delete',
      onConfirm: async () => {
        if (!(await deleteItem(item, errSlot))) {
          confirmSlot.replaceChildren();
          del.hidden = false;
        }
      },
      onCancel: () => {
        del.hidden = false;
        del.focus();
      },
    }));
  });

  const fields = new Map((t.fields || []).map((f) => [f.name, f]));
  const rows = [['Made', fmtTime(item.created)]];
  if (typeof item.elapsed === 'number' && item.elapsed > 0) rows.push(['Took', fmtClock(item.elapsed)]);
  if (typeof item.peak_gb === 'number') rows.push(['Peak memory', item.peak_gb.toFixed(1) + ' GB']);
  if (item.width && item.height) rows.push(['Dimensions', item.width + ' × ' + item.height]);
  if (item.duration) rows.push(['Length', fmtClock(item.duration) + ' (' + item.duration.toFixed(1) + ' s)']);
  rows.push(['File size', fmtBytes(item.size)]);
  for (const [key, v] of Object.entries(params)) {
    if (key === tk) continue;
    const f = fields.get(key);
    rows.push([f ? f.label : key, fieldValue(f, key, v)]);
  }

  const drawer = h('aside', {class: 'lib-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'lib-dr-h', tabindex: '-1'},
    h('div', {class: 'lib-drawer-head'}, toolIcon(t.group, true),
      h('h2', {id: 'lib-dr-h', title: label + ' · ' + item.name}, label + ' · ' + item.name), closeBtn),
    h('div', {class: 'lib-drawer-body'},
      preview ? h('div', {class: 'lib-preview k-' + item.kind}, preview) : null,
      h('div', {class: 'lib-actions'}, useBtn, sendRow),
      tk ? h('div', {class: 'lib-prompt-wrap'}, h('span', {class: 'eyebrow'}, TEXT_LABEL[tk]), h('div', {class: 'lib-prompt'}, params[tk])) : null,
      item.note ? h('p', {class: 'help'}, item.note) : null,
      h('dl', {class: 'kv'}, rows.map(([a, b]) => [h('dt', null, a), h('dd', null, b)])),
      h('div', {class: 'lib-file'}, h('span', {class: 'eyebrow'}, 'File'), h('code', {class: 'lib-path'}, item.path)),
      h('div', {class: 'lib-row'}, download, reveal, h('span', {class: 'spacer'}), del),
      confirmSlot, errSlot));
  const scrim = h('div', {class: 'lib-scrim', onclick: () => closeDrawer(true)});
  drawer.addEventListener('keydown', drawerKey);
  s.drawerSlot.replaceChildren(scrim, drawer);
  s.open = {item, back, drawer};
  s.ctx.router.replace('library', {job: item.job, name: item.name});
  closeBtn.focus();
}

function drawerKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    closeDrawer(true);
    return;
  }
  if (e.key !== 'Tab' || !s.open) return;
  const f = [...s.open.drawer.querySelectorAll('button, a[href], video, audio, input, select, textarea')]
    .filter((el) => !el.disabled && el.tabIndex >= 0 && el.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && (document.activeElement === first || document.activeElement === s.open.drawer)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// restoreFocus: put the focus back on the output's card. keepRoute: another output opens right after.
function closeDrawer(restoreFocus, keepRoute) {
  if (!s || !s.open) return;
  const {item, back, drawer} = s.open;
  for (const m of drawer.querySelectorAll('video, audio')) m.pause();
  s.open = null;
  s.drawerSlot.replaceChildren();
  if (!keepRoute) s.ctx.router.replace('library');
  if (!restoreFocus) return;
  const card = s.cards.get(keyOf(item));
  const target = card && card.el.isConnected ? card.open : back;
  if (target && target.isConnected) target.focus();
}

function openImages(item) {
  const list = filtered().filter((i) => i.kind === 'image');
  const at = Math.max(0, list.findIndex((i) => keyOf(i) === keyOf(item)));
  lightbox(list.map((i) => ({url: i.url, name: i.name, caption: i.tool_label || i.tool})), at);
}

async function revealItem(item, errSlot) {
  errSlot.hidden = true;
  try {
    await s.ctx.api.request('/api/library/reveal', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({job: item.job, name: item.name})});
  } catch (e) {
    if (!errSlot.isConnected) return;
    errSlot.hidden = false;
    errSlot.textContent = 'Show in Finder failed: ' + e.message;
  }
}

async function deleteItem(item, errSlot) {
  try {
    await s.ctx.api.request('/api/library/' + encodeURIComponent(item.job) + '/' + encodeURIComponent(item.name), {method: 'DELETE'});
  } catch (e) {
    errSlot.hidden = false;
    errSlot.textContent = 'Delete failed: ' + e.message;
    return false;
  }
  toast('Deleted ' + item.name + '.');
  if (s) {
    const k = keyOf(item);
    const els = cardEls();
    const i = els.findIndex((el) => el.getAttribute('data-key') === k);
    const next = els[i + 1] || els[i - 1];
    s.active = next ? next.getAttribute('data-key') : null;
    s.items = s.items.filter((x) => keyOf(x) !== k);
    s.byKey.delete(k);
    if (s.cards.has(k)) dropCard(k, s.cards.get(k));
    if (s.playing === k) setPlaying(null);
    closeDrawer(false);
    render();
    if (s.active && s.cards.has(s.active)) s.cards.get(s.active).open.focus();
    else if (!s.tools.hidden) s.search.focus();
  }
  store.refresh('jobs');
  return true;
}

export default {
  id: 'library',
  title: 'Library',
  subtitle: 'Every output in outputs/',
  mount,
  update,
  unmount,
};
