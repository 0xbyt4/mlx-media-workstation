// Models: every catalog entry from GET /api/models, grouped by type, with install state, fit against this Mac,
// licence, in-app download (a server-side queue: one runs, the others wait; progress, speed, time left, Cancel) and
// the gated-licence step. Manual entries (environments and checkouts) show their README commands. The app never
// deletes a model and never asks for a Hugging Face token: removal is a command the user runs, and gated downloads
// use the login on this Mac. Cards are rebuilt only when their state changes; the live download numbers update in
// place.
// The "Set up this Mac" card on top picks a set of models for this Mac (step 1), asks for the gated licences (step 2)
// and shows the download queue (step 3). The queue comes from GET /api/models, so it survives leaving the view.
// Route params: #/models?model=<id> scrolls to that card and highlights it; #/models?setup=1 opens the setup card.
// The chat profile shows only the entries Chat needs (store.shownModel: the LLM environment and weights), in the card
// list and in the setup card.

import {h, icon, toolIcon, GROUP_ORDER, fmtGb, fmtLeft, fmtAbout, fit, toast, errorBox, inlineConfirm} from '../ui.js';

// README "Model licences": the one-line summary per licence. The full text is on each model page.
const LICENCE_NOTES = {
  'Apache-2.0': 'Commercial use allowed.',
  'MIT': 'Commercial use allowed.',
  'FLUX Kommunity License': 'Output generation only, under US$5M revenue; other commercial use needs a licence from Black Forest Labs.',
  'Fish Audio Community License': 'Commercial use under US$500k revenue; no hosted speech service; no imitation of a voice without consent; keep its watermark; show "Built with Fish Audio".',
  'LTX-2.x Community License Agreement': 'Free under US$10M revenue; keep its watermarks and AI disclosures.',
};
const CHAT_GROUP = 'Chat';
const GROUP_TITLE = {Image: 'Image', Voice: 'Voice', Music: 'Music', Video: 'Video', Edit: 'Edit', [CHAT_GROUP]: 'Chat and prompt help'};
const CHAT_TITLE = 'Chat'; // the chat group in the chat profile, which has no prompt help
const OFFLINE_TEXT = 'The Workstation server does not answer. Try again when it is back.';

// Kept across mounts for this session only (never stored): the gated models whose licence step is open, the ones
// whose "I accepted the licence" box is ticked (the user's statement; the app cannot check it), and open disclosures.
const gateOpen = new Set();
const gateOk = new Set();
const openDetails = new Set();

// The setup card, kept across mounts for this session only. picks is null until the defaults for this Mac are set.
// closedBatch is the key (batch_id and ids) of the run of downloads the user closed or hid.
// adding: the pick step shows over a running queue ("Add more models"). fresh: the card was just opened.
const setup = {
  open: false, step: 'pick', picks: null, closedBatch: '', adding: false, fresh: false, error: null, sending: false,
  confirmAll: false,
};

let s = null; // the mounted view

// ---------- mount ----------

function mount(root, ctx, params) {
  root.classList.add('v-models');
  s = {
    root, ctx, offs: [], cards: new Map(), groups: new Map(), pending: new Set(), actionErr: new Map(),
    lastStatus: null, loadErr: null, focusId: params && params.model ? params.model : null,
    setupSig: null, setupLive: null, setupFocus: false,
  };
  s.status = h('div', {class: 'models-status'});
  s.setup = h('div', {class: 'setup-slot'});
  s.stats = h('div', {class: 'mac-stats'});
  s.setupBtn = h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'setup-open', onclick: () => openSetup(true)},
    icon('download', 'ic-sm'), 'Set up models');
  s.summary = h('div', {class: 'card mac-sum', role: 'group', 'aria-label': 'This Mac', hidden: true}, s.stats, s.setupBtn);
  s.list = h('div', {class: 'models-groups'});
  root.append(h('div', {class: 'models'}, s.status, s.setup, s.summary, s.list));
  if (params && params.setup === '1') openSetup(false);

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
    on('profile', () => {
      // other entries and group titles: build the list again, and pick the defaults of the new profile
      s.cards.clear();
      s.groups.clear();
      s.list.replaceChildren();
      s.setupSig = null;
      setup.picks = null;
      render();
    }),
  );
  if (ctx.store.get('models')) render();
  else load();
}

function update(params) {
  if (!s) return;
  s.focusId = params && params.model ? params.model : null;
  if (params && params.setup === '1') openSetup(false);
  render();
}

function unmount() {
  if (!s) return;
  if (s.fastOff) s.fastOff();
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

// GET /api/models as this profile shows it: in the chat profile only the entries Chat needs, and only those in the
// download queue's run.
let shownCache = {raw: null, profile: null, value: null};
function data() {
  const {store} = s.ctx;
  const raw = store.get('models');
  if (!raw || store.profile() !== 'chat') return raw;
  if (shownCache.raw !== raw || shownCache.profile !== store.profile()) {
    const models = raw.models.filter(store.shownModel);
    const ids = new Set(models.map((m) => m.id));
    const queue = raw.queue ? {...raw.queue, batch: raw.queue.batch.filter((id) => ids.has(id))} : raw.queue;
    shownCache = {raw, profile: store.profile(), value: {...raw, models, queue}};
  }
  return shownCache.value;
}

function chatOnly() {
  return s.ctx.store.profile() === 'chat';
}

function groupTitle(g) {
  return g === CHAT_GROUP && chatOnly() ? CHAT_TITLE : GROUP_TITLE[g] || g;
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

function isQueued(m) {
  return !!m.download && m.download.status === 'queued';
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
  renderSetup(d);
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
  s.setupBtn.hidden = setupVisible();
  s.stats.replaceChildren(
    stat(mem ? Math.round(mem) + ' GB' : '-', 'memory in this Mac'),
    stat(frees.length ? fmtGb(frees[0]) : '-', frees.length > 1 ? 'free on the fullest of ' + frees.length + ' disks' : 'free on the disk'),
    stat(installed.length + ' of ' + models.length, 'installed'),
    stat('about ' + Math.round(used) + ' GB', 'used by downloaded models'),
    h('p', {class: 'help mac-note'},
      (chatOnly() ? '' : 'Each tool runs alone, so only its peak has to fit the memory. ') + '"Fits" keeps ' +
      d.machine.mem_reserve_gb + ' GB free for macOS. The peaks were measured on a 48 GB M5 Pro. ' +
      'The used space adds up the download sizes.'),
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
      sec = {list, el: h('section', {class: 'model-group', 'aria-labelledby': id}, h('h2', {id}, groupIcon(g), groupTitle(g)), list)};
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
  const dl = m.download ? [m.download.status, m.download.position, m.download.error, m.download.total_gb !== null] : null;
  const need = missingNeed(m, byId);
  return JSON.stringify([
    m.label, m.kind, m.installed, m.fits, m.peak_gb, m.size_gb, m.licence, m.licence_url, m.gated, m.repo, m.path,
    m.path_key, m.install, m.used_by, dl, need && need.label, !!busy && busy.id !== m.id, lowDisk(m),
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
  const queued = isQueued(m);
  const pending = s.pending.has(m.id);
  const need = missingNeed(m, byId);
  const titleId = 'm-' + m.id.replace(/[^a-z0-9-]/gi, '-');
  const live = {};

  let status;
  if (m.installed) status = pill('ok', 'Installed');
  else if (isRunning) status = pill('run', 'Downloading');
  else if (queued) status = pill('idle', 'Queued, number ' + dl.position);
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

  const path = h('div', {class: 'model-path'},
    h('span', {class: 'faint'}, m.installed ? 'At ' : m.kind === 'manual' ? 'Looks for it at ' : 'Installs to '), m.path);

  const actions = [];
  let reason = null;
  const later = !!busy && busy.id !== m.id;
  if (isRunning) {
    actions.push(h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'cancel', onclick: () => cancel(m)}, icon('x', 'ic-sm'), 'Cancel'));
  } else if (queued) {
    actions.push(h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'cancel', onclick: () => cancel(m)}, icon('x', 'ic-sm'), 'Remove from queue'));
  } else if (!m.installed && m.kind === 'download') {
    if (need) reason = 'Install ' + need.label + ' first. Its commands are on its card.';
    actions.push(h('button', {
      type: 'button', class: 'btn btn-sm' + (f && f.k !== 'no' ? ' btn-primary' : ''), 'data-k': 'download',
      disabled: !!reason || pending, title: reason, 'aria-describedby': reason ? titleId + '-why' : null,
      'aria-expanded': m.gated ? String(gateOpen.has(m.id)) : null,
      onclick: () => (m.gated ? openGate(m) : start(m)),
    }, icon('download', 'ic-sm'), pending ? 'Starting...' : later ? 'Add to queue' : 'Download'));
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
  if (later && !isRunning && !queued && !m.installed && m.kind === 'download' && !need) {
    extra.push(h('p', {class: 'help model-wide'}, busy.label + ' is downloading. This one starts after it and any others in the queue.'));
  }
  if (dl) extra.push(downloadBlock(m, dl, live));
  if (!m.installed && !isRunning && !queued && m.gated && gateOpen.has(m.id)) extra.push(gate(m, !!reason || pending, later));
  if (lowDisk(m)) {
    extra.push(h('div', {class: 'banner banner-warn model-wide'}, icon('alert'), h('div', {class: 'banner-body'},
      h('span', null, 'The disk has ' + fmtGb(m.disk_free_gb) + ' free, and this download is about ' + fmtGb(m.size_gb) +
        '. Free some space first. Files that finished in an earlier try are not downloaded again.'))));
  }
  if (!m.installed && !isRunning && !queued && f && f.k === 'no') {
    extra.push(h('p', {class: 'help model-wide'},
      'This model needs more memory than this Mac has. It can still run, but macOS will swap, and jobs may take many times longer or stop.'));
  }
  if (m.kind === 'manual' && m.install) {
    const cmds = commandBlock(m.install, 'Copy the install commands');
    if (m.installed) extra.push(disclosure(m.id + ':install', 'Install commands', cmds));
    else {
      extra.push(h('div', {class: 'model-wide manual'},
        h('p', {class: 'help'}, 'Workstation does not install this one. Run these commands in Terminal, then press Check again. ',
          pathHelp(m, true)),
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
  paintProgress(live, m.download);
}

// Fills a progress block (live.bar, live.fill, live.done, live.left) from a running download.
function paintProgress(live, dl) {
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
  live.done.textContent = doneText + ' (' + Math.floor(dl.percent) + '%)' + fmtSpeed(dl.speed_mbs);
  if (typeof dl.eta_s === 'number') live.left.textContent = fmtLeft(dl.eta_s);
  else if (dl.stalled) live.left.textContent = 'No data for ' + fmtIdle(dl.idle_s) + '. Check the network connection.';
  else if (dl.speed_mbs === 0) live.left.textContent = 'Waiting for data...';
  else live.left.textContent = 'Working out the time left...';
}

// The speed after the GB done; nothing while it is unknown or 0 (the time-left text explains that).
function fmtSpeed(mbs) {
  if (typeof mbs !== 'number' || mbs <= 0) return '';
  if (mbs < 1) return ' · ' + Math.max(1, Math.round(mbs * 1000)) + ' kB/s';
  return ' · ' + (mbs >= 100 ? Math.round(mbs) : mbs.toFixed(1)) + ' MB/s';
}

function fmtIdle(sec) {
  return sec < 120 ? sec + ' s' : Math.floor(sec / 60) + ' min';
}

function progressBlock(m, live, cls) {
  live.fill = h('div', {class: 'progress-fill'});
  live.bar = h('div', {class: 'progress', role: 'progressbar', 'aria-label': m.label + ' download', 'aria-valuemin': '0', 'aria-valuemax': '100'}, live.fill);
  live.done = h('span', {class: 'num'});
  live.left = h('span', {class: 'num'});
  return h('div', {class: cls}, live.bar, h('div', {class: 'dl-line'}, live.done, live.left));
}

function downloadBlock(m, dl, live) {
  if (dl.status === 'running') return progressBlock(m, live, 'model-dl model-wide');
  if (dl.status === 'failed') {
    const again = 'Download again. Files that finished stay and are not downloaded again.';
    const refused = /refused|gated|not logged in/i.test(dl.error || '');
    return h('div', {class: 'model-wide'}, errorBox({
      title: 'The download of ' + m.label + ' failed',
      text: dl.error || 'No reason was given.',
      fixes: refused ? [
        h('span', null, 'Open ', extLink(hfPage(m), 'the model page'), ' and accept its licence.'),
        h('span', null, 'In Terminal, in the app folder, run ', h('code', null, '.venv/bin/hf auth login'), ' once.'),
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
function gate(m, blocked, later) {
  const boxId = 'gate-' + m.id.replace(/[^a-z0-9-]/gi, '-');
  const go = h('button', {
    type: 'button', class: 'btn btn-sm btn-primary', 'data-k': 'gate-go', disabled: blocked || !gateOk.has(m.id),
    onclick: () => start(m),
  }, icon('download', 'ic-sm'), (later ? 'Add to queue, about ' : 'Download about ') + fmtGb(m.size_gb));
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
      h('li', null, 'In Terminal, in the app folder, log in once on this Mac:', commandBlock('.venv/bin/hf auth login', 'Copy the login command')),
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

// ---------- set up this Mac ----------

function queueOf(d) {
  return d.queue || {running: null, queued: [], count: 0, left_gb: 0, batch: [], batch_id: 0, disk_margin_gb: 10};
}

// The queue step shows while a run of several downloads goes on, or while the card is open and the last run is not
// closed yet. A single download from a card shows on its card only.
// The server's batch_id starts at 0 again after a restart, so the key also holds the ids.
function batchKey(q) {
  return q.batch_id + '|' + q.batch.join(',');
}

function queueShown(d) {
  const q = queueOf(d);
  if (!q.batch.length || batchKey(q) === setup.closedBatch || setup.adding) return false;
  return setup.open || (q.count > 0 && q.batch.length > 1);
}

function setupVisible() {
  const d = data();
  if (!d) return false;
  return setup.open || queueShown(d) || d.models.some((m) => m.kind === 'download' && !m.installed);
}

// Opens the card and moves focus to its heading. A finished run of downloads is not shown again on open.
function openSetup(renderNow) {
  setup.open = true;
  setup.fresh = true;
  setup.error = null;
  if (s) s.setupFocus = true;
  if (renderNow) render();
}

function closeSetup() {
  setup.open = false;
  setup.adding = false;
  setup.step = 'pick';
  setup.error = null;
  setup.confirmAll = false;
  render();
  const btn = s && s.root.querySelector('[data-k="setup-open"]');
  if (btn) btn.focus();
}

// True when a and b serve a common tool or app feature (an environment and the weights it runs).
function shareUse(a, b) {
  return (a.used_by || []).some((u) => (b.used_by || []).includes(u));
}

// Manual entries (environments, checkouts) that a tool or feature of m also needs and that are not installed.
function missingEnvs(m, d) {
  return d.models.filter((e) => e.kind === 'manual' && !e.installed && e.id !== m.needs && shareUse(e, m));
}

// Why an entry cannot be picked now, or null.
function blockedWhy(m, byId) {
  if (m.installed) return 'Installed';
  if (m.download && (m.download.status === 'running' || m.download.status === 'queued')) return 'In the queue';
  const need = missingNeed(m, byId);
  if (need) return 'Install ' + need.label + ' first (below)';
  return null;
}

// The weights that fit this Mac. Weights whose runtime is missing are left out: they cannot run after the download.
function defaultPicks(d) {
  const byId = new Map(d.models.map((m) => [m.id, m]));
  const picks = new Set();
  for (const m of d.models) {
    // the full profile leaves out what no tool uses (the LLM); in the chat profile the LLM is the point
    if (m.kind !== 'download' || blockedWhy(m, byId) || (!(m.tools || []).length && !chatOnly())) continue;
    if (missingEnvs(m, d).length) continue;
    const f = fit(m.peak_gb, d.machine.mem_total_gb, d.machine.mem_reserve_gb);
    if (f && f.k !== 'no') picks.add(m.id);
  }
  return picks;
}

// The picked entries that can still be queued, in the order of the list.
function pickedModels(d) {
  if (!setup.picks) return [];
  const byId = new Map(d.models.map((m) => [m.id, m]));
  return setupOrder(d).filter((m) => setup.picks.has(m.id) && !blockedWhy(m, byId));
}

function setupOrder(d) {
  const order = [...GROUP_ORDER, CHAT_GROUP];
  const rank = (m) => {
    const i = order.indexOf(groupOf(m));
    return i < 0 ? 98 : i;
  };
  return d.models.filter((m) => m.kind === 'download').map((m, i) => [m, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([m]) => m);
}

// Download size and the free space left after it, on the fullest disk. Entries on the same disk report the same free
// space, so that value stands for the disk. The queue's own downloads come off every disk (an upper bound).
function totals(d, picked) {
  const q = queueOf(d);
  const disks = new Map();
  for (const m of picked) {
    if (typeof m.disk_free_gb !== 'number') continue;
    disks.set(m.disk_free_gb, (disks.get(m.disk_free_gb) || 0) + (m.size_gb || 0));
  }
  if (!disks.size) {
    const frees = d.models.map((m) => m.disk_free_gb).filter((v) => typeof v === 'number');
    if (frees.length) disks.set(Math.min(...frees), 0);
  }
  let after = null;
  for (const [free, gb] of disks) {
    const left = free - gb - (q.left_gb || 0);
    if (after === null || left < after) after = left;
  }
  const gb = picked.reduce((sum, m) => sum + (m.size_gb || 0), 0);
  return {gb, after, margin: q.disk_margin_gb, low: after !== null && after < q.disk_margin_gb};
}

function renderSetup(d) {
  const q = queueOf(d);
  // between two downloads nothing runs for a moment, and the store would fall back to its slow poll
  if (q.count && !s.fastOff) s.fastOff = s.ctx.store.fast('models');
  else if (!q.count && s.fastOff) {
    s.fastOff();
    s.fastOff = null;
  }
  if (setup.fresh) {
    setup.fresh = false;
    if (!q.count) setup.closedBatch = batchKey(q);
  }
  if (!q.count && setup.adding) {
    // the run ended while the user picks more: keep the pick step
    setup.adding = false;
    setup.closedBatch = batchKey(q);
  }
  let mode = null;
  const showQueue = queueShown(d);
  // the defaults are set when the pick step shows, so they leave out what is installed or in the queue by then
  if (!showQueue && setup.open && setup.picks === null) setup.picks = defaultPicks(d);
  if (showQueue) mode = 'queue';
  else if (setup.open) mode = setup.step === 'licence' && pickedModels(d).some((m) => m.gated) ? 'licence' : 'pick';
  else if (q.count || d.models.some((m) => m.kind === 'download' && !m.installed)) mode = 'teaser';
  if (mode === 'queue') {
    setup.step = 'pick';
    setup.open = true; // the list stays when the run ends, until Done or Hide
  }

  const sig = JSON.stringify([mode, setup.error, setup.sending, setup.confirmAll, setup.adding, d.machine,
    d.models.map((m) => [m.id, m.installed, m.fits, m.size_gb, m.download && [m.download.status, m.download.position, m.download.error]]),
    mode === 'queue' || mode === 'teaser' ? [q.batch, q.batch_id, q.count, q.running] : null,
    mode === 'licence' ? pickedModels(d).map((m) => m.id) : null]);
  if (sig !== s.setupSig) {
    s.setupSig = sig;
    const active = document.activeElement;
    const focused = s.setup.contains(active) ? active.getAttribute('data-k') : null;
    const confirmWasOpen = s.confirmOpen;
    s.setupLive = {};
    const el = mode === 'teaser' ? setupTeaser(d) : mode === 'pick' ? pickStep(d) : mode === 'licence' ? licenceStep(d) :
      mode === 'queue' ? queueStep(d) : null;
    if (el) keyControls(el);
    s.setup.replaceChildren(...(el ? [el] : []));
    s.confirmOpen = mode === 'queue' && setup.confirmAll;
    if (focused) {
      const again = s.setup.querySelector('[data-k="' + CSS.escape(focused) + '"]');
      if (again && !again.disabled) again.focus();
    }
    // a rebuilt "Cancel all" confirm focuses its Keep button; give focus back when the confirm was open already
    if (confirmWasOpen && s.confirmOpen) {
      queueMicrotask(() => {
        if (focused) {
          const again = s && s.setup.querySelector('[data-k="' + CSS.escape(focused) + '"]');
          if (again && !again.disabled) again.focus();
        } else if (active && active.isConnected) active.focus();
      });
    }
  }
  paintSetupLive(d);
  if (s.setupFocus && mode && mode !== 'teaser') {
    s.setupFocus = false;
    const head = s.setup.querySelector('h2');
    if (head && head.isConnected) {
      const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      head.scrollIntoView({block: 'start', behavior: smooth ? 'smooth' : 'auto'});
      head.focus({preventScroll: true});
    }
  }
}

// Gives every control without a data-k a key from its text, so focus survives a rebuild.
function keyControls(el) {
  for (const n of el.querySelectorAll('a, button, input, pre[tabindex]')) {
    if (n.hasAttribute('data-k')) continue;
    const text = (n.getAttribute('aria-label') || n.getAttribute('href') || n.textContent || '').trim().slice(0, 80);
    n.setAttribute('data-k', 'auto:' + n.tagName.toLowerCase() + ':' + text);
  }
}

function setupShell(eyebrow, title, step, ...body) {
  const names = ['Pick', 'Licences', 'Download'];
  return h('section', {class: 'card setup', 'aria-labelledby': 'setup-h'},
    h('div', {class: 'setup-head'},
      h('div', {class: 'setup-title'}, h('span', {class: 'eyebrow'}, eyebrow), h('h2', {id: 'setup-h', tabindex: '-1'}, title)),
      step ? h('ol', {class: 'setup-steps', 'aria-label': 'Steps'}, names.map((n, i) => h('li', {
        class: i + 1 === step ? 'now' : i + 1 < step ? 'past' : null, 'aria-current': i + 1 === step ? 'step' : null,
      }, h('span', {class: 'n', 'aria-hidden': 'true'}, String(i + 1)), h('span', {class: 'sr-only'}, 'Step ' + (i + 1) + ' of 3: '), n))) : null),
    body,
    setup.error ? h('div', {class: 'banner banner-err', role: 'alert'}, icon('alert'), h('div', {class: 'banner-body'}, h('span', null, setup.error))) : null);
}

// The closed card: what is missing, or what the queue does.
function setupTeaser(d) {
  const q = queueOf(d);
  const missing = d.models.filter((m) => m.kind === 'download' && !m.installed);
  const gb = missing.reduce((sum, m) => sum + (m.size_gb || 0), 0);
  const run = q.running ? d.models.find((m) => m.id === q.running) : null;
  const text = q.count ?
    (run ? run.label + ' is downloading' : 'Downloads are starting') + (q.count > 1 ? ', ' + (q.count - 1) + ' more in the queue' : '') +
      ' (about ' + fmtGb(q.left_gb) + ' left).' :
    missing.length + (missing.length === 1 ? ' model is' : ' models are') + ' not downloaded yet (about ' + Math.round(gb) +
      ' GB in all). Pick the ones for this Mac and download them in one go.';
  return h('section', {class: 'card setup setup-teaser', 'aria-labelledby': 'setup-h'},
    h('span', {class: 'tool-ico t-edit', 'aria-hidden': 'true'}, icon('download')),
    h('div', {class: 'setup-teaser-text'}, h('h2', {id: 'setup-h'}, 'Set up this Mac'), h('p', {class: 'help'}, text)),
    h('button', {type: 'button', class: 'btn btn-primary btn-sm', 'data-k': 'setup-open', onclick: () => {
      setup.closedBatch = '';
      openSetup(true);
    }}, icon(q.count ? 'arrow' : 'download', 'ic-sm'), q.count ? 'Show downloads' : 'Set up models'));
}

// Step 1: every downloadable entry by type, with the manual entries below.
function pickStep(d) {
  const byId = new Map(d.models.map((m) => [m.id, m]));
  const mem = d.machine.mem_total_gb;
  const all = setupOrder(d);
  const groups = new Map();
  for (const m of all) {
    const g = groupOf(m);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(m);
  }
  const live = s.setupLive;
  const sets = [...groups].map(([g, ms]) => h('fieldset', {class: 'setup-group'},
    h('legend', null, groupIcon(g), groupTitle(g)),
    h('div', {class: 'setup-items'}, ms.map((m) => pickItem(m, d, byId)))));

  const anyMissing = all.some((m) => !m.installed);
  const noEnv = all.some((m) => !m.installed && missingEnvs(m, d).length);
  live.total = h('p', {class: 'setup-total num', id: 'setup-total'});
  live.say = h('span', {class: 'sr-only', 'aria-live': 'polite'});
  live.go = h('button', {type: 'button', class: 'btn btn-primary', 'data-k': 'setup-go', 'aria-describedby': 'setup-total',
    onclick: () => pickNext(d)});
  const intro = anyMissing ?
    'Workstation runs open models on this Mac and uploads nothing. The models are not included: you download each one once. ' +
      'The picks below fit ' + (mem ? Math.round(mem) + ' GB' : 'this Mac') +
      (chatOnly() ? ' and are what Chat needs.' : ' and are used by a tool. Change them if you like.') +
      (noEnv ? ' A model whose environment is missing is not picked: install the environment below first.' : '') :
    'Every model that Workstation can download is installed.';
  return setupShell('Set up this Mac', 'Pick the models for this Mac', 1,
    h('p', {class: 'setup-intro'}, intro),
    sets,
    manualBlock(d),
    h('p', {class: 'help'}, 'Downloads run one at a time; you can leave this screen meanwhile. ' +
      'Files that finished in an earlier try are not downloaded again.'),
    h('div', {class: 'setup-foot sticky'}, live.total, live.say,
      setup.adding ?
        h('button', {type: 'button', class: 'btn btn-ghost', 'data-k': 'setup-close', onclick: () => {
          setup.adding = false;
          s.setupFocus = true;
          render();
        }}, 'Back to the downloads') :
        h('button', {type: 'button', class: 'btn btn-ghost', 'data-k': 'setup-close', onclick: closeSetup}, 'Choose later'),
      live.go));
}

function pickItem(m, d, byId) {
  const f = fit(m.peak_gb, d.machine.mem_total_gb, d.machine.mem_reserve_gb);
  const why = blockedWhy(m, byId);
  const boxId = 'pick-' + m.id.replace(/[^a-z0-9-]/gi, '-');
  const note = LICENCE_NOTES[m.licence];
  const envs = missingEnvs(m, d);
  const box = h('input', {
    type: 'checkbox', id: boxId, 'data-k': 'pick-' + m.id, checked: why ? m.installed || why === 'In the queue' : setup.picks.has(m.id),
    disabled: !!why, 'aria-describedby': boxId + '-d',
    onchange: (e) => {
      if (e.target.checked) setup.picks.add(m.id);
      else setup.picks.delete(m.id);
      paintSetupLive(data());
      // spoken only on a change by the user, not on every poll
      const live = s && s.setupLive;
      if (live && live.say) live.say.textContent = live.total.textContent;
    },
  });
  let state = null;
  if (m.installed) state = pill('ok', 'Installed');
  else if (m.download && m.download.status === 'running') state = pill('run', 'Downloading');
  else if (m.download && m.download.status === 'queued') state = pill('idle', 'Queued');
  return h('div', {class: 'setup-item' + (why ? ' off' : '')},
    box,
    h('div', {class: 'setup-item-main'},
      h('label', {for: boxId, class: 'setup-item-name'}, m.label),
      h('div', {class: 'setup-item-meta', id: boxId + '-d'},
        h('span', null, 'For ', (m.used_by || []).join(', ') || '-'),
        m.licence ? h('span', null, m.gated ? h('span', {class: 'gated'}, icon('lock', 'ic-sm'), ' Licence to accept: ') : null,
          m.licence + (note ? ' (' + note.replace(/\.$/, '') + ')' : '')) : null,
        why && why !== 'Installed' && why !== 'In the queue' ? h('span', {class: 'setup-why'}, why + '.') : null,
        !m.installed && envs.length ? h('span', {class: 'setup-why'}, 'Also needs ' + envs.map((e) => e.label).join(' and ') +
          ' to run. Workstation cannot install ' + (envs.length > 1 ? 'those' : 'that') + '; see below.') : null)),
    h('div', {class: 'setup-item-side'},
      state || (f ? h('span', {class: 'pill pill-' + f.pill, title: f.text}, f.short) : null),
      h('span', {class: 'num'}, 'about ' + fmtGb(m.size_gb)),
      typeof m.peak_gb === 'number' ? h('span', {class: 'num faint'}, 'peak ' + fmtGb(m.peak_gb, 0)) : null));
}

// Where the commands put a manual entry. When that is not the folder Workstation checks, the user sets the path key.
// The card shows the path above, so it gets the short text.
function pathHelp(m, short) {
  if (!m.path_key) return null;
  if (m.install_path && m.install_path !== m.path) {
    return ['The commands install it to ', h('code', {class: 'mono'}, m.install_path), '. Then set ', h('code', null, m.path_key),
      ' to that path in Settings.'];
  }
  if (short) return ['If it is in another folder, set ', h('code', null, m.path_key), ' in Settings.'];
  return ['It goes to ', h('code', {class: 'mono'}, m.path), '. For another folder, set ', h('code', null, m.path_key), ' in Settings.'];
}

// The environments and checkouts: Workstation cannot install them, and a model without its environment cannot run.
function manualBlock(d) {
  const manual = d.models.filter((m) => m.kind === 'manual');
  if (!manual.length) return null;
  const missing = manual.filter((m) => !m.installed);
  const rows = manual.map((m) => {
    const users = d.models.filter((x) => x.kind === 'download' && (x.id !== m.id) &&
      (x.needs === m.id || shareUse(x, m))).map((x) => x.label);
    const head = h('div', {class: 'setup-env-head'}, h('b', null, m.label),
      m.installed ? pill('ok', 'Installed') : pill('idle', 'Not installed'),
      users.length ? h('span', {class: 'faint'}, 'Runs ' + users.join(', ')) : null);
    if (m.installed) return h('div', {class: 'setup-env'}, head);
    return h('div', {class: 'setup-env'}, head, commandBlock(m.install || '', 'Copy the install commands for ' + m.label),
      m.path_key ? h('p', {class: 'help'}, pathHelp(m)) : null);
  });
  return h('section', {class: 'setup-envs', 'aria-labelledby': 'setup-envs-h'},
    h('h3', {id: 'setup-envs-h'}, 'Environments you install by hand'),
    h('p', {class: 'help'}, missing.length ?
      'Workstation cannot install these. A model cannot run without its environment, so run these commands in Terminal ' +
        '(they are in README.md too), then press Check again.' :
      'All environments are installed.'),
    rows,
    missing.length ? h('div', null, h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'setup-recheck', onclick: recheckAll},
      icon('refresh', 'ic-sm'), 'Check again')) : null);
}

// The totals line and the primary button of step 1, updated in place when a box changes.
function paintSetupLive(d) {
  const live = s && s.setupLive;
  if (!live || !d) return;
  if (live.total) {
    const picked = pickedModels(d);
    const t = totals(d, picked);
    const gated = picked.filter((m) => m.gated);
    let text = picked.length ? picked.length + (picked.length === 1 ? ' model' : ' models') + ' · about ' + fmtGb(t.gb) +
      ' to download' : 'Nothing picked';
    if (t.after !== null) text += ' · about ' + fmtGb(Math.max(0, t.after)) + ' free after';
    if (t.low && picked.length) text += '. That is under the ' + fmtGb(t.margin, 0) + ' Workstation keeps free: pick fewer models or free some space.';
    live.total.textContent = text;
    live.total.classList.toggle('low', t.low && picked.length > 0);
    live.go.disabled = !picked.length || (t.low && picked.length > 0) || setup.sending;
    live.go.replaceChildren(icon(gated.length ? 'arrow' : 'download', 'ic-sm'),
      setup.sending ? 'Starting...' : gated.length ? 'Next: licences' : 'Download selected');
  }
  if (live.licGo) {
    const picked = pickedModels(d);
    const ok = picked.filter((m) => m.gated).every((m) => gateOk.has(m.id));
    live.licGo.disabled = !ok || setup.sending;
    live.licWhy.hidden = ok;
  }
  if (live.items) paintQueueLive(d);
}

function pickNext(d) {
  const picked = pickedModels(d);
  if (!picked.length) return;
  if (picked.some((m) => m.gated)) {
    setup.step = 'licence';
    s.setupFocus = true;
    render();
    return;
  }
  sendQueue(picked);
}

// Step 2: the licence step of the existing card, once for every gated pick. The app never asks for a token.
function licenceStep(d) {
  const picked = pickedModels(d);
  const gated = picked.filter((m) => m.gated);
  const live = s.setupLive;
  live.licWhy = h('p', {class: 'help', id: 'setup-lic-why'}, 'Tick the box of each model first.');
  live.licGo = h('button', {type: 'button', class: 'btn btn-primary', 'data-k': 'setup-lic-go', 'aria-describedby': 'setup-lic-why',
    onclick: () => sendQueue(pickedModels(data()))}, icon('download', 'ic-sm'), 'Download ' + picked.length +
      (picked.length === 1 ? ' model' : ' models'));
  const items = gated.map((m) => {
    const boxId = 'setup-gate-' + m.id.replace(/[^a-z0-9-]/gi, '-');
    return h('div', {class: 'gate', role: 'group', 'aria-label': 'Licence of ' + m.label},
      h('b', null, icon('lock', 'ic-sm'), ' ' + m.label + ': ' + (m.licence || 'licence')),
      LICENCE_NOTES[m.licence] ? h('span', {class: 'help'}, LICENCE_NOTES[m.licence]) : null,
      h('span', null, 'Open ', extLink(hfPage(m), 'huggingface.co/' + m.repo), ', log in there, and accept the licence. ',
        m.licence_url ? extLink(m.licence_url, 'Read the licence') : null),
      h('label', {class: 'gate-check', for: boxId}, h('input', {
        type: 'checkbox', id: boxId, 'data-k': 'gate-ok-' + m.id, checked: gateOk.has(m.id),
        onchange: (e) => {
          if (e.target.checked) gateOk.add(m.id);
          else gateOk.delete(m.id);
          paintSetupLive(data());
        },
      }), 'I accepted the licence of ' + m.label));
  });
  return setupShell('Set up this Mac', 'Accept the licences', 2,
    h('p', {class: 'setup-intro'}, (gated.length === 1 ? gated[0].label + ' is' : gated.map((m) => m.label).join(' and ') + ' are') +
      ' gated on Hugging Face: the download works only after you accept the licence there, with your Hugging Face account.'),
    h('ol', {class: 'gate-steps setup-lic-steps'},
      h('li', null, 'Accept the licence of each model on its page:', h('div', {class: 'setup-gates'}, items)),
      h('li', null, 'In Terminal, in the app folder, log in once on this Mac:', commandBlock('.venv/bin/hf auth login', 'Copy the login command')),
      h('li', null, 'Download. The download uses that login.')),
    h('p', {class: 'help'}, 'Workstation never asks for your Hugging Face token and never stores it. If the licence is not accepted, ' +
      'that download fails with the reason, and the others go on.'),
    h('div', {class: 'setup-foot sticky'}, live.licWhy,
      h('button', {type: 'button', class: 'btn btn-ghost', 'data-k': 'setup-back', onclick: () => {
        setup.step = 'pick';
        s.setupFocus = true;
        render();
      }}, 'Back'),
      live.licGo));
}

async function sendQueue(picked) {
  if (!picked.length || setup.sending) return;
  const view = s;
  setup.sending = true;
  setup.error = null;
  paintSetupLive(data());
  try {
    const r = await view.ctx.api.request('/api/models/queue', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: picked.map((m) => m.id)}),
    });
    const n = r && r.added ? r.added.length : picked.length;
    const others = r && r.queue ? r.queue.count - n : 0;
    const what = n + (n === 1 ? ' model' : ' models');
    if (!n) toast('Those models are installed or in the queue already.');
    else if (others > 0) toast('Added ' + what + ' to the queue.');
    else toast('Downloading ' + what + ', one at a time.');
    setup.step = 'pick';
    setup.picks = null;
    setup.closedBatch = '';
    setup.adding = false;
    if (s === view) s.setupFocus = true;
  } catch (e) {
    setup.error = e.offline ? OFFLINE_TEXT : e.message;
  } finally {
    setup.sending = false;
  }
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  render();
}

// Step 3: the queue from GET /api/models.
function queueStep(d) {
  const q = queueOf(d);
  const byId = new Map(d.models.map((m) => [m.id, m]));
  const live = s.setupLive;
  live.items = new Map();
  const active = q.count > 0;
  const items = q.batch.map((id) => byId.get(id)).filter(Boolean);
  const done = items.filter((m) => m.installed).length;
  const failed = items.filter((m) => !m.installed && m.download && m.download.status === 'failed').length;
  live.sum = h('p', {class: 'setup-total num'});
  const rows = items.map((m) => queueItem(m, live));
  const foot = [];
  if (active) {
    if (setup.confirmAll) {
      foot.push(inlineConfirm({
        text: 'Cancel every download in this list? Finished files stay.',
        confirmLabel: 'Cancel all', cancelLabel: 'Keep downloading',
        onConfirm: cancelAll,
        onCancel: () => {
          setup.confirmAll = false;
          render();
          focusSetup('setup-cancel-all');
        },
      }));
    } else {
      foot.push(h('button', {type: 'button', class: 'btn', 'data-k': 'setup-cancel-all', onclick: () => {
        setup.confirmAll = true;
        render();
      }}, icon('x', 'ic-sm'), 'Cancel all'));
    }
  }
  foot.push(h('button', {type: 'button', class: 'btn', 'data-k': 'setup-more', onclick: () => {
    if (active) setup.adding = true;
    else setup.closedBatch = batchKey(q);
    setup.open = true;
    setup.step = 'pick';
    setup.picks = new Set();
    s.setupFocus = true;
    render();
  }}, icon('plus', 'ic-sm'), 'Add more models'));
  if (!active) {
    foot.push(h('button', {type: 'button', class: 'btn btn-primary', 'data-k': 'setup-done', onclick: () => {
      setup.closedBatch = batchKey(q);
      setup.picks = null;
      closeSetup();
    }}, icon('check', 'ic-sm'), 'Done'));
  } else {
    foot.push(h('button', {type: 'button', class: 'btn btn-ghost', 'data-k': 'setup-hide', onclick: () => {
      setup.closedBatch = batchKey(q);
      closeSetup();
    }}, 'Hide'));
  }
  const cancelled = items.filter((m) => !m.installed && m.download && m.download.status === 'cancelled').length;
  let title = 'Downloading the models';
  if (!active) title = (cancelled ? 'Downloads stopped' : 'Downloads finished') + (failed ? ', ' + failed + ' failed' : '');
  return setupShell('Set up this Mac', title, 3,
    live.sum,
    h('ol', {class: 'q-list', 'aria-label': 'Downloads'}, rows),
    active ? h('p', {class: 'help'}, 'You can leave this screen: the downloads go on, and this list comes back here. ' +
      'A failed download does not stop the others.') :
      h('p', {class: 'help'}, done + ' of ' + items.length + ' installed. ' + (failed ? 'The reason of each failure is on its row. ' : '') +
        'Models need their environment to run; see the cards below.'),
    h('div', {class: 'setup-foot'}, foot));
}

function queueItem(m, live) {
  const dl = m.download;
  const st = m.installed ? 'done' : dl ? dl.status : 'skipped';
  const rowLive = {};
  live.items.set(m.id, rowLive);
  let mark;
  let body = null;
  const acts = [];
  if (st === 'done') mark = pill('ok', 'Installed');
  else if (st === 'running') {
    mark = pill('run', 'Downloading');
    body = progressBlock(m, rowLive, 'q-progress');
  } else if (st === 'queued') mark = pill('idle', 'Waits, number ' + dl.position);
  else if (st === 'failed') {
    mark = pill('err', 'Failed');
    const refused = /refused|gated|not logged in/i.test(dl.error || '');
    body = h('div', {class: 'q-err'}, h('p', null, dl.error || 'No reason was given.'),
      refused ? h('p', {class: 'help'}, extLink(hfPage(m), 'Open the model page of ' + m.label)) : null);
  } else if (st === 'cancelled') mark = pill('idle', 'Cancelled');
  else mark = pill('idle', 'Not downloaded');
  if (st === 'running' || st === 'queued') {
    acts.push(h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'q-cancel-' + m.id, 'aria-label': (st === 'running' ? 'Cancel ' : 'Remove from the queue: ') + m.label,
      onclick: () => queueCancel(m)}, icon('x', 'ic-sm'), st === 'running' ? 'Cancel' : 'Remove'));
  } else if (st === 'failed' || st === 'cancelled') {
    acts.push(h('button', {type: 'button', class: 'btn btn-sm', 'data-k': 'q-again-' + m.id, 'aria-label': 'Download again: ' + m.label,
      onclick: () => retry(m)}, icon('refresh', 'ic-sm'), 'Try again'));
  }
  return h('li', {class: 'q-item q-' + st},
    h('div', {class: 'q-name'}, h('b', null, m.label), h('span', {class: 'num faint'}, 'about ' + fmtGb(m.size_gb)), mark),
    acts.length ? h('div', {class: 'q-act'}, acts) : null,
    body);
}

// Queues m again; a gated model whose box is not ticked in this session goes through the licence step first.
function retry(m) {
  if (!m.gated || gateOk.has(m.id)) {
    sendQueue([m]);
    return;
  }
  const q = queueOf(data());
  if (q.count) setup.adding = true;
  else setup.closedBatch = batchKey(q);
  setup.open = true;
  setup.picks = new Set([m.id]);
  setup.step = 'licence';
  s.setupFocus = true;
  render();
}

function paintQueueLive(d) {
  const live = s.setupLive;
  const q = queueOf(d);
  const byId = new Map(d.models.map((m) => [m.id, m]));
  for (const [id, rowLive] of live.items) {
    const m = byId.get(id);
    if (m) paintProgress(rowLive, m.download);
  }
  if (!live.sum) return;
  const items = q.batch.map((id) => byId.get(id)).filter(Boolean);
  const done = items.filter((m) => m.installed).length;
  if (!q.count) {
    live.sum.textContent = done + ' of ' + items.length + ' installed';
    return;
  }
  const run = q.running ? byId.get(q.running) : null;
  const pos = run ? items.indexOf(run) + 1 : 0;
  let text = (pos ? 'Model ' + pos + ' of ' + items.length : items.length + ' models') + ' · about ' + fmtGb(q.left_gb) + ' left to download';
  const speed = run && run.download ? run.download.speed_mbs : null;
  if (typeof speed === 'number' && speed > 0) text += ' · ' + fmtAbout(q.left_gb * 1000 / speed) + ' for all, at the current speed';
  live.sum.textContent = text;
}

function focusSetup(k) {
  const el = s && s.setup.querySelector('[data-k="' + k + '"]');
  if (el && !el.disabled) el.focus();
}

async function queueCancel(m) {
  const view = s;
  setup.error = null;
  const wasQueued = isQueued(m);
  try {
    await view.ctx.api.cancelDownload(m.id);
    toast(wasQueued ? m.label + ' is out of the queue.' : 'Download of ' + m.label + ' cancelled.');
  } catch (e) {
    setup.error = e.offline ? OFFLINE_TEXT : e.message;
  }
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  render();
  focusSetup('q-again-' + m.id);
}

async function cancelAll() {
  const view = s;
  setup.error = null;
  try {
    const r = await view.ctx.api.request('/api/models/queue/clear', {method: 'POST'});
    const running = r && r.queue ? r.queue.running : null;
    if (running) await view.ctx.api.cancelDownload(running);
    toast('Downloads cancelled. Finished files stay.');
  } catch (e) {
    setup.error = e.offline ? OFFLINE_TEXT : e.message;
  }
  setup.confirmAll = false;
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  render();
  focusSetup('setup-done');
}

async function recheckAll() {
  const view = s;
  await view.ctx.store.refresh('models');
  if (s !== view) return;
  const d = data();
  const missing = d ? d.models.filter((m) => m.kind === 'manual' && !m.installed) : [];
  toast(missing.length ? missing.length + ' still not found. A path changed in Settings counts after a restart.' : 'All environments are installed.',
    {kind: missing.length ? 'err' : null});
  render();
  focusSetup('setup-recheck');
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
    const r = await view.ctx.api.downloadModel(m.id);
    gateOpen.delete(m.id);
    toast(r && r.status === 'queued' ? m.label + ' is in the queue, number ' + r.position + '.' : 'Downloading ' + m.label + '.');
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
    const queued = isQueued(m);
    const r = await view.ctx.api.cancelDownload(m.id);
    if (r && r.status === 'cancelled') toast(queued ? m.label + ' is out of the queue.' : 'Download of ' + m.label + ' cancelled.');
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
  toast(now && now.installed ? m.label + ' is installed.' : m.label + ' is still not found at ' + m.path + '. A path changed in Settings counts after a restart.', {kind: now && now.installed ? null : 'err'});
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
