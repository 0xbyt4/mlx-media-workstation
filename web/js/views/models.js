// Models: every catalog entry from GET /api/models, grouped by type, with install state, fit against this Mac,
// licence, in-app download (one at a time, progress, speed, time left, Cancel) and the gated-licence step.
// Manual entries (environments and checkouts) show their README commands. The app never deletes a model and never
// asks for a Hugging Face token: removal is a command the user runs, and gated downloads use the login on this Mac.
// Cards are rebuilt only when their state changes; the live download numbers update in place.
// Route param: #/models?model=<id> scrolls to that card and highlights it.

import {h, icon, toolIcon, GROUP_ORDER, fmtGb, fmtLeft, fit, toast, errorBox} from '../ui.js';

// README "Model licences": the one-line summary per licence. The full text is on each model page.
const LICENCE_NOTES = {
  'Apache-2.0': 'Commercial use allowed.',
  'MIT': 'Commercial use allowed.',
  'FLUX Kommunity License': 'Commercial output under US$5M revenue; review outputs; AI disclosure where the law asks.',
  'Fish Audio Community License': 'Commercial use under US$500k revenue; no imitation of real people; keep its watermark.',
  'LTX-2 Community License': 'Free under US$10M revenue.',
};
const CHAT_GROUP = 'Chat';
const GROUP_TITLE = {Image: 'Image', Voice: 'Voice', Music: 'Music', Video: 'Video', Edit: 'Edit', [CHAT_GROUP]: 'Chat and prompt help'};
const OFFLINE_TEXT = 'The Workstation server does not answer. Try again when it is back.';

// Kept across mounts for this session only (never stored): the gated models whose licence step is open, the ones
// whose "I accepted the licence" box is ticked (the user's statement; the app cannot check it), and open disclosures.
const gateOpen = new Set();
const gateOk = new Set();
const openDetails = new Set();

let s = null; // the mounted view

// ---------- mount ----------

function mount(root, ctx, params) {
  root.classList.add('v-models');
  s = {
    root, ctx, offs: [], cards: new Map(), groups: new Map(), pending: new Set(), actionErr: new Map(),
    lastStatus: null, loadErr: null, focusId: params && params.model ? params.model : null,
  };
  s.status = h('div', {class: 'models-status'});
  s.summary = h('div', {class: 'card mac-sum', role: 'group', 'aria-label': 'This Mac', hidden: true});
  s.list = h('div', {class: 'models-groups'});
  root.append(h('div', {class: 'models'}, s.status, s.summary, s.list));

  const {on} = ctx.store;
  s.offs.push(
    on('models', () => {
      s.loadErr = null;
      render();
    }),
    on('tools', () => {
      for (const c of s.cards.values()) c.sig = null;
      render();
    }),
  );
  if (ctx.store.get('models')) render();
  else load();
}

function update(params) {
  if (!s) return;
  s.focusId = params && params.model ? params.model : null;
  render();
}

function unmount() {
  if (!s) return;
  for (const off of s.offs) off();
  s = null;
}

// The store's poll does not report HTTP errors; on the first load (and Try again) this view asks itself, to show them.
async function load() {
  const view = s;
  view.loadErr = null;
  renderStatus();
  try {
    await view.ctx.api.models();
    if (s !== view) return;
    await view.ctx.store.refresh('models');
  } catch (e) {
    if (s !== view) return;
    s.loadErr = e;
    render();
  }
}

// ---------- data helpers ----------

function data() {
  return s.ctx.store.get('models');
}

function groupOf(m) {
  if (!m.tools || !m.tools.length) return CHAT_GROUP;
  const t = s.ctx.store.get('toolsById').get(m.tools[0]);
  if (t && t.group) return t.group;
  const prefix = m.tools[0].split('.')[0];
  return prefix.charAt(0).toUpperCase() + prefix.slice(1);
}

function groupIcon(group) {
  if (group === CHAT_GROUP) return h('span', {class: 'tool-ico t-edit', 'aria-hidden': 'true'}, icon('chat'));
  return toolIcon(group);
}

function runningOf(models) {
  return models.find((m) => m.download && m.download.status === 'running') || null;
}

// The entry m.needs, when it is not installed yet.
function missingNeed(m, byId) {
  const need = m.needs ? byId.get(m.needs) : null;
  return need && !need.installed ? need : null;
}

function hfPage(m) {
  return 'https://huggingface.co/' + m.repo;
}

function extLink(href, text) {
  return h('a', {href, target: '_blank', rel: 'noopener noreferrer'}, text);
}

// Single quotes keep spaces and $ in the path literal; a quote inside becomes '\''.
function shellQuote(p) {
  return "'" + String(p).replace(/'/g, "'\\''") + "'";
}

// The folder to delete: the whole repo folder for a Hugging Face cache snapshot, else the install folder.
function removeTarget(m) {
  const at = m.path.indexOf('/snapshots/');
  return m.path_key === null && at > 0 ? m.path.slice(0, at) : m.path;
}

function lowDisk(m) {
  return !m.installed && m.kind === 'download' && typeof m.size_gb === 'number' && typeof m.disk_free_gb === 'number' &&
    m.size_gb > m.disk_free_gb;
}

// ---------- render ----------

function render() {
  if (!s) return;
  renderStatus();
  const d = data();
  if (!d) return;
  s.summary.hidden = false;
  renderSummary(d);
  renderCards(d);
  noteTransitions(d);
  if (s.focusId && s.cards.has(s.focusId)) {
    const el = s.cards.get(s.focusId).el;
    s.focusId = null;
    el.classList.add('flash');
    requestAnimationFrame(() => {
      const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      el.scrollIntoView({block: 'center', behavior: smooth ? 'smooth' : 'auto'});
      el.focus({preventScroll: true});
    });
    setTimeout(() => el.classList.remove('flash'), 1600);
  }
}

function renderStatus() {
  if (data()) {
    s.status.replaceChildren();
    return;
  }
  const e = s.loadErr;
  if (e) {
    s.status.replaceChildren(errorBox({
      title: 'The model list did not load',
      text: e.offline ? 'The Workstation server does not answer.' : e.message,
      fixes: e.offline ? ['Check that Workstation is still running.', 'Press Try again.'] : null,
      actions: [h('button', {type: 'button', class: 'btn btn-sm', onclick: load}, icon('refresh', 'ic-sm'), 'Try again')],
    }));
    return;
  }
  s.status.replaceChildren(h('div', {class: 'models-loading', role: 'status'}, h('span', {class: 'spinner'}), 'Loading the model list...'));
}

function renderSummary(d) {
  const models = d.models;
  const installed = models.filter((m) => m.installed);
  const used = installed.reduce((sum, m) => sum + (m.kind === 'download' && m.size_gb ? m.size_gb : 0), 0);
  const frees = [...new Set(models.map((m) => m.disk_free_gb).filter((v) => typeof v === 'number'))].sort((a, b) => a - b);
  const mem = d.machine.mem_total_gb;
  const stat = (value, label) => h('div', {class: 'stat'}, h('b', {class: 'num'}, value), h('span', null, label));
  s.summary.replaceChildren(
    stat(mem ? Math.round(mem) + ' GB' : '-', 'memory in this Mac'),
    stat(frees.length ? fmtGb(frees[0]) : '-', frees.length > 1 ? 'free on the fullest of ' + frees.length + ' disks' : 'free on the disk'),
    stat(installed.length + ' of ' + models.length, 'installed'),
    stat('about ' + Math.round(used) + ' GB', 'used by downloaded models'),
    h('p', {class: 'help mac-note'},
      'Each tool runs alone, so only its peak has to fit the memory. "Fits" keeps ' + d.machine.mem_reserve_gb +
      ' GB free for macOS. The peaks were measured on a 48 GB M5 Pro. The used space adds up the download sizes.'),
  );
}

function renderCards(d) {
  const byId = new Map(d.models.map((m) => [m.id, m]));
  const busy = runningOf(d.models);
  const order = [...GROUP_ORDER, CHAT_GROUP];
  const groups = new Map();
  for (const m of d.models) {
    const g = groupOf(m);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(m);
  }
  const rank = (g) => (order.indexOf(g) < 0 ? 98 : order.indexOf(g));
  const names = [...groups.keys()].sort((a, b) => rank(a) - rank(b));

  const seen = new Set();
  const sections = names.map((g) => {
    let sec = s.groups.get(g);
    if (!sec) {
      const id = 'mg-' + g.toLowerCase();
      const list = h('div', {class: 'model-list'});
      sec = {list, el: h('section', {class: 'model-group', 'aria-labelledby': id}, h('h2', {id}, groupIcon(g), GROUP_TITLE[g] || g), list)};
      s.groups.set(g, sec);
    }
    syncChildren(sec.list, groups.get(g).map((m) => {
      seen.add(m.id);
      return card(m, byId, busy);
    }));
    return sec.el;
  });
  for (const id of [...s.cards.keys()]) if (!seen.has(id)) s.cards.delete(id);
  syncChildren(s.list, sections);
}

// Puts nodes into parent in this order, moving only what is out of place (keeps focus and open details).
function syncChildren(parent, nodes) {
  nodes.forEach((n, i) => {
    if (parent.children[i] !== n) parent.insertBefore(n, parent.children[i] || null);
  });
  while (parent.children.length > nodes.length) parent.lastChild.remove();
}

// The card's state without the numbers that change during a download (those update in place).
function signature(m, byId, busy) {
  const dl = m.download ? [m.download.status, m.download.error, m.download.total_gb !== null] : null;
  const need = missingNeed(m, byId);
  return JSON.stringify([
    m.label, m.kind, m.installed, m.fits, m.peak_gb, m.size_gb, m.licence, m.licence_url, m.gated, m.repo, m.path,
    m.path_key, m.install, m.used_by, dl, need && need.label, busy && busy.id !== m.id ? busy.label : null, lowDisk(m),
    s.pending.has(m.id), s.actionErr.get(m.id) || null, gateOpen.has(m.id),
  ]);
}

function card(m, byId, busy) {
  const sig = signature(m, byId, busy);
  const old = s.cards.get(m.id);
  if (old && old.sig === sig) {
    updateLive(old, m);
    return old.el;
  }
  const fresh = build(m, byId, busy);
  fresh.sig = sig;
  if (old) {
    const focused = old.el.contains(document.activeElement) ? document.activeElement.getAttribute('data-k') : null;
    old.el.replaceWith(fresh.el);
    if (focused) {
      const again = fresh.el.querySelector('[data-k="' + focused + '"]');
      if (again && !again.disabled) again.focus();
    }
  }
  s.cards.set(m.id, fresh);
  updateLive(fresh, m);
  return fresh.el;
}

// ---------- one card ----------

function build(m, byId, busy) {
  const d = data();
  const f = typeof m.peak_gb === 'number' ? fit(m.peak_gb, d.machine.mem_total_gb, d.machine.mem_reserve_gb) : null;
  const dl = m.download;
  const isRunning = !!dl && dl.status === 'running';
  const pending = s.pending.has(m.id);
  const need = missingNeed(m, byId);
  const titleId = 'm-' + m.id.replace(/[^a-z0-9-]/gi, '-');
  const live = {};

  let status;
  if (m.installed) status = pill('ok', 'Installed');
  else if (isRunning) status = pill('run', 'Downloading');
  else if (pending) status = pill('run', 'Starting');
  else if (need) status = pill('idle', 'Needs ' + need.label + ' first');
  else status = pill('idle', 'Not installed');

  const facts = [h('span', null, 'Used by ', h('b', null, m.used_by.join(', ') || '-'))];
  if (m.kind === 'download') {
    facts.push(h('span', null, 'Download ', h('b', null, 'about ' + fmtGb(m.size_gb))));
    if (typeof m.peak_gb === 'number') facts.push(h('span', null, 'Peak ', h('b', null, fmtGb(m.peak_gb, 0))));
    live.free = h('b');
    facts.push(h('span', null, 'Disk free ', live.free));
  } else {
    facts.push(h('span', null, 'Installed by hand with the README commands'));
  }

  let lic = null;
  if (m.licence) {
    const note = LICENCE_NOTES[m.licence];
    lic = h('div', {class: 'model-lic'},
      m.gated ? h('span', {class: 'gated', title: 'Gated on Hugging Face'}, icon('lock', 'ic-sm'), h('span', {class: 'sr-only'}, 'Gated. ')) : null,
      h('b', null, m.licence), note ? ': ' + note + ' ' : '. ',
      m.licence_url ? extLink(m.licence_url, 'Read the licence') : null);
  }

  const path = h('div', {class: 'model-path'}, h('span', {class: 'faint'}, m.installed ? 'At ' : 'Installs to '), m.path);

  const actions = [];
  let reason = null;
  if (isRunning) {
    actions.push(h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'cancel', onclick: () => cancel(m)}, icon('x', 'ic-sm'), 'Cancel'));
  } else if (!m.installed && m.kind === 'download') {
    if (busy && busy.id !== m.id) reason = 'One download at a time: wait for ' + busy.label + ' to finish, or cancel it.';
    else if (need) reason = 'Install ' + need.label + ' first. Its commands are on its card.';
    actions.push(h('button', {
      type: 'button', class: 'btn btn-sm' + (f && f.k !== 'no' ? ' btn-primary' : ''), 'data-k': 'download',
      disabled: !!reason || pending, title: reason, 'aria-describedby': reason ? titleId + '-why' : null,
      'aria-expanded': m.gated ? String(gateOpen.has(m.id)) : null,
      onclick: () => (m.gated ? openGate(m) : start(m)),
    }, icon('download', 'ic-sm'), pending ? 'Starting...' : 'Download'));
  }

  const main = h('div', {class: 'model-main'},
    h('div', {class: 'model-name'}, h('h3', {id: titleId}, m.label), status, f ? h('span', {class: 'pill pill-' + f.pill}, f.text) : null),
    h('div', {class: 'model-facts'}, facts),
    lic,
    path);

  const extra = [];
  if (reason) extra.push(h('p', {class: 'help model-wide', id: titleId + '-why'}, reason));
  const err = s.actionErr.get(m.id);
  if (err) {
    extra.push(h('div', {class: 'banner banner-err model-wide', role: 'alert'}, icon('alert'),
      h('div', {class: 'banner-body'}, h('span', null, err))));
  }
  if (dl) extra.push(downloadBlock(m, dl, live));
  if (!m.installed && !isRunning && m.gated && gateOpen.has(m.id)) extra.push(gate(m, !!reason || pending));
  if (lowDisk(m)) {
    extra.push(h('div', {class: 'banner banner-warn model-wide'}, icon('alert'), h('div', {class: 'banner-body'},
      h('span', null, 'The disk has ' + fmtGb(m.disk_free_gb) + ' free, and this download is about ' + fmtGb(m.size_gb) +
        '. Free some space first. Files that finished in an earlier try are not downloaded again.'))));
  }
  if (!m.installed && !isRunning && f && f.k === 'no') {
    extra.push(h('p', {class: 'help model-wide'},
      'This model needs more memory than this Mac has. It can still run, but macOS will swap, and jobs may take many times longer or stop.'));
  }
  if (m.kind === 'manual' && m.install) {
    const cmds = commandBlock(m.install, 'Copy the install commands');
    if (m.installed) extra.push(disclosure(m.id + ':install', 'Install commands', cmds));
    else {
      extra.push(h('div', {class: 'model-wide manual'},
        h('p', {class: 'help'}, 'Workstation does not install this one. Run these commands in Terminal, then press Check again.',
          m.path_key ? [' If it is in another folder, set ', h('code', null, m.path_key), ' in Settings.'] : null),
        cmds,
        h('div', null, h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'recheck', onclick: () => recheck(m)},
          icon('refresh', 'ic-sm'), 'Check again'))));
    }
  }
  if (m.installed && m.kind === 'download') extra.push(removeBlock(m));

  const el = h('article', {class: 'card model', 'aria-labelledby': titleId, tabindex: '-1', 'data-model': m.id},
    groupIcon(groupOf(m)), main, h('div', {class: 'model-act'}, actions), extra);
  return {el, live, sig: null};
}

function pill(kind, text) {
  return h('span', {class: 'pill pill-' + kind}, text);
}

// The live numbers: disk free and the download progress.
function updateLive(c, m) {
  const {live} = c;
  if (live.free) live.free.textContent = fmtGb(m.disk_free_gb);
  const dl = m.download;
  if (!live.bar || !dl || dl.status !== 'running') return;
  const sized = dl.total_gb !== null;
  live.bar.classList.toggle('indeterminate', !sized);
  live.fill.style.width = (sized ? dl.percent : 0) + '%';
  if (!sized) {
    live.bar.removeAttribute('aria-valuenow');
    live.bar.setAttribute('aria-valuetext', 'Preparing');
    live.done.textContent = 'Checking the file list and the free disk space...';
    live.left.textContent = '';
    return;
  }
  const doneText = dl.done_gb.toFixed(1) + ' of ' + dl.total_gb.toFixed(1) + ' GB';
  live.bar.setAttribute('aria-valuenow', String(Math.floor(dl.percent)));
  live.bar.setAttribute('aria-valuetext', doneText);
  let speed = '';
  if (typeof dl.speed_mbs === 'number') speed = ' · ' + (dl.speed_mbs >= 100 ? Math.round(dl.speed_mbs) : dl.speed_mbs.toFixed(1)) + ' MB/s';
  live.done.textContent = doneText + ' (' + Math.floor(dl.percent) + '%)' + speed;
  if (typeof dl.eta_s === 'number') live.left.textContent = fmtLeft(dl.eta_s);
  else if (dl.speed_mbs === 0) live.left.textContent = 'No new data for 10 s';
  else live.left.textContent = 'Working out the time left...';
}

function downloadBlock(m, dl, live) {
  if (dl.status === 'running') {
    live.fill = h('div', {class: 'progress-fill'});
    live.bar = h('div', {class: 'progress', role: 'progressbar', 'aria-label': m.label + ' download', 'aria-valuemin': '0', 'aria-valuemax': '100'}, live.fill);
    live.done = h('span', {class: 'num'});
    live.left = h('span', {class: 'num'});
    return h('div', {class: 'model-dl model-wide'}, live.bar, h('div', {class: 'dl-line'}, live.done, live.left));
  }
  if (dl.status === 'failed') {
    const again = 'Download again. Files that finished stay and are not downloaded again.';
    const refused = /refused|gated|not logged in/i.test(dl.error || '');
    return h('div', {class: 'model-wide'}, errorBox({
      title: 'The download of ' + m.label + ' failed',
      text: dl.error || 'No reason was given.',
      fixes: refused ? [
        h('span', null, 'Open ', extLink(hfPage(m), 'the model page'), ' and accept its licence.'),
        h('span', null, 'In Terminal, run ', h('code', null, 'hf auth login'), ' once.'),
        again,
      ] : [again],
    }));
  }
  if (dl.status === 'cancelled' && !m.installed && !gateOpen.has(m.id)) {
    return h('p', {class: 'help model-wide'}, 'Download cancelled. The files that finished stay, and the next download skips them.');
  }
  return null;
}

// The gated-licence step (docs/DESIGN.md decision 10). The app never asks for, shows or stores a token.
function gate(m, blocked) {
  const boxId = 'gate-' + m.id.replace(/[^a-z0-9-]/gi, '-');
  const go = h('button', {
    type: 'button', class: 'btn btn-sm btn-primary', 'data-k': 'gate-go', disabled: blocked || !gateOk.has(m.id),
    onclick: () => start(m),
  }, icon('download', 'ic-sm'), 'Download about ' + fmtGb(m.size_gb));
  const box = h('input', {
    type: 'checkbox', id: boxId, 'data-k': 'gate-ok', checked: gateOk.has(m.id),
    onchange: (e) => {
      if (e.target.checked) gateOk.add(m.id);
      else gateOk.delete(m.id);
      go.disabled = blocked || !e.target.checked;
    },
  });
  return h('div', {class: 'gate model-wide', role: 'group', 'aria-label': 'Licence step for ' + m.label},
    h('b', null, m.label + ' is gated on Hugging Face.'),
    h('ol', {class: 'gate-steps'},
      h('li', null, 'Open ', extLink(hfPage(m), 'huggingface.co/' + m.repo), ', log in there, and accept the ',
        m.licence || 'licence', '.'),
      h('li', null, 'In Terminal, log in once on this Mac:', commandBlock('hf auth login', 'Copy the login command')),
      h('li', null, 'Tick the box below and download. The download uses that login.')),
    h('p', {class: 'help'}, 'Workstation never asks for your Hugging Face token and never stores it.'),
    h('label', {class: 'gate-check', for: boxId}, box, 'I accepted the licence on Hugging Face'),
    h('div', {class: 'gate-actions'}, go,
      h('button', {type: 'button', class: 'btn btn-sm btn-ghost', 'data-k': 'gate-close', onclick: () => closeGate(m)}, 'Close')));
}

function removeBlock(m) {
  return disclosure(m.id + ':remove', 'Remove from disk',
    h('p', {class: 'help'}, 'Workstation does not delete models. To free about ' + fmtGb(m.size_gb) +
      ', quit Workstation and run this command in Terminal. It deletes the folder for good.'),
    commandBlock('rm -rf ' + shellQuote(removeTarget(m)), 'Copy the remove command'));
}

function disclosure(key, title, ...body) {
  const el = h('details', {
    class: 'model-more model-wide', open: openDetails.has(key),
    ontoggle: () => (el.open ? openDetails.add(key) : openDetails.delete(key)),
  }, h('summary', {'data-k': 'more-' + key.split(':')[1]}, title), h('div', {class: 'model-more-body'}, body));
  return el;
}

function commandBlock(text, label) {
  return h('div', {class: 'cmd'},
    h('pre', {class: 'cmd-text', tabindex: '0', 'aria-label': label.replace(/^Copy the /, '')}, h('code', null, text)),
    h('button', {type: 'button', class: 'btn btn-sm btn-ghost btn-icon cmd-copy', title: label, 'aria-label': label,
      onclick: () => copy(text)}, icon('copy', 'ic-sm')));
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied.');
    return;
  } catch {
    // no clipboard API in this window: fall back to a selection copy
  }
  const ta = h('textarea', {class: 'sr-only', readonly: true, 'aria-hidden': 'true'});
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  ta.remove();
  toast(ok ? 'Copied.' : 'Copy did not work here. Select the text and press Cmd-C.', {kind: ok ? null : 'err'});
}

// ---------- actions ----------

function focusIn(id, sel) {
  const c = s && s.cards.get(id);
  const el = c && c.el.querySelector(sel);
  if (el && !el.disabled) el.focus();
}

function openGate(m) {
  gateOpen.add(m.id);
  s.actionErr.delete(m.id);
  render();
  focusIn(m.id, '[data-k="gate-ok"]');
}

function closeGate(m) {
  gateOpen.delete(m.id);
  render();
  focusIn(m.id, '[data-k="download"]');
}

async function start(m) {
  if (m.gated && !gateOk.has(m.id)) return; // the licence step comes first
  const view = s;
  view.pending.add(m.id);
  view.actionErr.delete(m.id);
  render();
  try {
    await view.ctx.api.downloadModel(m.id);
    gateOpen.delete(m.id);
    toast('Downloading ' + m.label + '.');
  } catch (e) {
    view.actionErr.set(m.id, e.offline ? OFFLINE_TEXT : e.message);
  } finally {
    view.pending.delete(m.id);
  }
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  render();
  focusIn(m.id, '[data-k="cancel"], [data-k="gate-go"], [data-k="download"]');
}

async function cancel(m) {
  const view = s;
  view.actionErr.delete(m.id);
  try {
    const r = await view.ctx.api.cancelDownload(m.id);
    if (r && r.status === 'cancelled') toast('Download of ' + m.label + ' cancelled.');
  } catch (e) {
    view.actionErr.set(m.id, e.offline ? OFFLINE_TEXT : e.message);
  }
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  render();
  focusIn(m.id, '[data-k="download"]');
}

async function recheck(m) {
  const view = s;
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  const now = data() && data().models.find((x) => x.id === m.id);
  toast(now && now.installed ? m.label + ' is installed.' : m.label + ' is still not found at ' + m.path + '.', {kind: now && now.installed ? null : 'err'});
}

// A toast when a download ends while this view is open.
function noteTransitions(d) {
  const now = new Map(d.models.map((m) => [m.id, m.download ? m.download.status : null]));
  if (s.lastStatus) {
    for (const m of d.models) {
      if (s.lastStatus.get(m.id) !== 'running') continue;
      const st = now.get(m.id);
      if (st === 'done') toast(m.label + ' is installed.');
      else if (st === 'failed') toast('The download of ' + m.label + ' failed. The reason is on its card.', {kind: 'err'});
    }
  }
  s.lastStatus = now;
}

export default {
  id: 'models',
  title: 'Models',
  subtitle: (ctx) => {
    const mem = ctx.store.memTotal();
    return 'What is installed, and what fits ' + (mem ? Math.round(mem) + ' GB' : 'this Mac');
  },
  mount,
  update,
  unmount,
};
