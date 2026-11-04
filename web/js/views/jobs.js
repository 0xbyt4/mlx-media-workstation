// Jobs: the running job with live progress, the queue (move up or down, remove), and finished jobs with their outputs,
// "Use these settings", "Send to" and delete. Cards are rebuilt only when a job changes, so media keeps playing while
// the list polls. A queue row keeps its card when only its position changes, so the focus stays on it.

import {store} from '../store.js';
import {
  h, icon, toolIcon, lsGet, lsSet, fmtAbout, fmtClock, fmtDuration, fmtGb, fmtLeft, fmtTime, fmtValue, toast,
  inlineConfirm, emptyState, errorBox, menu, media, downloadLink, lightbox,
} from '../ui.js';

const FILTERS = [['all', 'All'], ['active', 'Active'], ['done', 'Done'], ['failed', 'Failed or cancelled']];
const STATUS = {
  queued: ['Queued', 'idle'], running: ['Running', 'run'], done: ['Done', 'ok'], failed: ['Failed', 'err'],
  cancelled: ['Cancelled', 'idle'],
};
const KIND_ICON = {image: 'image', audio: 'voice', video: 'video'};

// Kept across mounts: details the user opened or closed, and queued jobs that are being removed.
const openDetails = new Set();
const removing = new Set();  // job ids being removed, hidden until the next refresh

let s = null;

function mount(root, ctx) {
  root.classList.add('v-jobs');
  s = {root, ctx, cards: new Map(), offs: [], filter: FILTERS.some(([k]) => k === lsGet('ws.jobsFilter')) ? lsGet('ws.jobsFilter') : 'all'};
  s.seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Show jobs'},
    FILTERS.map(([k]) => h('button', {type: 'button', 'data-filter': k, onclick: () => setFilter(k)})));
  s.sum = h('span', {class: 'jobs-sum', 'aria-live': 'polite'});
  s.live = h('span', {class: 'sr-only', role: 'status'});
  s.empty = h('div', {class: 'jobs-empty'});
  s.runSec = section('Running', 'h-run');
  s.queueSec = section('Up next', 'h-queue');
  s.queueList = h('div', {class: 'card q-list'});
  s.queueSec.body.append(s.queueList);
  s.finSec = section('Finished', 'h-fin');
  root.append(h('div', {class: 'jobs'},
    h('div', {class: 'jobs-head'}, s.seg, s.sum, s.live), s.empty, s.runSec.el, s.queueSec.el, s.finSec.el));

  const {on} = ctx.store;
  s.offs.push(
    on('jobs', render),
    on('tools', () => {
      for (const card of s.cards.values()) card.sig = null;
      render();
    }),
    on('tick', tick),
    on('signedout', render),
  );
  render();
}

function unmount() {
  if (!s) return;
  for (const off of s.offs) off();
  s = null;
}

function section(title, id) {
  const body = h('div', {class: 'jobs-list'});
  const el = h('section', {class: 'jobs-sec', 'aria-labelledby': id}, h('h2', {id}, title), body);
  return {el, body};
}

function setFilter(k) {
  s.filter = k;
  lsSet('ws.jobsFilter', k);
  render();
}

// ---------- rendering ----------

function render() {
  if (!s) return;
  const jobs = store.get('jobs').filter((j) => !removing.has(j.id));
  const running = jobs.filter((j) => j.status === 'running');
  const queued = jobs.filter((j) => j.status === 'queued').sort((a, b) => a.position - b.position);
  const finished = jobs.filter((j) => store.FINISHED.has(j.status));
  const counts = {
    all: jobs.length,
    active: running.length + queued.length,
    done: finished.filter((j) => j.status === 'done').length,
    failed: finished.filter((j) => j.status !== 'done').length,
  };
  for (const b of s.seg.children) {
    const k = b.getAttribute('data-filter');
    b.textContent = FILTERS.find((f) => f[0] === k)[1] + (k === 'all' ? '' : ' ' + counts[k]);
    b.setAttribute('aria-pressed', String(k === s.filter));
  }
  const sum = [running.length ? '1 running' : 'Nothing running', queued.length + ' queued', counts.done + counts.failed + ' finished'].join(' · ');
  if (s.sum.textContent !== sum) s.sum.textContent = sum;  // a live region: only a change is read out

  const showActive = s.filter === 'all' || s.filter === 'active';
  const showFinished = s.filter !== 'active';
  const shownFinished = finished.filter((j) => s.filter === 'all' || (s.filter === 'done' ? j.status === 'done' : j.status !== 'done'));

  s.empty.replaceChildren();
  if (!jobs.length && !store.get('jobsAt')) {
    // the first GET /api/jobs has not answered yet, or it answers HTTP 401 (the shell banner says what to do)
    s.empty.append(store.get('signedOut') ? h('p', {class: 'muted', role: 'status'}, 'The jobs cannot load: this window is signed out.')
      : h('p', {class: 'muted', role: 'status'}, h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), ' Loading the jobs...'));
  } else if (!jobs.length) {
    s.empty.append(emptyState({icon: 'jobs', title: 'No jobs yet',
      text: 'Jobs you start in Create run here one at a time, so each model gets the memory it needs.',
      actions: [h('button', {type: 'button', class: 'btn btn-primary btn-sm', onclick: () => s.ctx.router.go('create')}, 'Go to Create')]}));
  }
  const any = jobs.length > 0;
  s.seg.hidden = !any;
  s.runSec.el.hidden = !any || !showActive;
  s.queueSec.el.hidden = !any || !showActive || !queued.length;
  s.finSec.el.hidden = !any || !showFinished;

  const seen = new Set();
  place(s.runSec.body, running, seen);
  if (!running.length && !s.runSec.body.querySelector('.empty')) {
    s.runSec.body.append(emptyState({icon: 'jobs', title: 'Nothing is running',
      text: queued.length ? 'The next job starts in a moment.' : 'Start a job in Create. It runs here with its progress.'}));
  }
  if (running.length) s.runSec.body.querySelector(':scope > .empty')?.remove();
  place(s.queueList, queued, seen);
  place(s.finSec.body, shownFinished, seen);
  const finEmpty = s.finSec.body.querySelector(':scope > .empty');
  if (!shownFinished.length && !finEmpty) {
    s.finSec.body.append(emptyState({icon: 'check', title: s.filter === 'failed' ? 'No failed or cancelled jobs' : 'No finished jobs',
      text: s.filter === 'failed' ? 'Every job so far has worked.' : 'Finished jobs appear here with their results.'}));
  }
  if (shownFinished.length && finEmpty) finEmpty.remove();

  for (const [id, card] of s.cards) {
    if (!seen.has(id)) {
      card.el.remove();
      s.cards.delete(id);
    }
  }
  updateWhen(running[0], queued);
  restoreFocus(queued);
}

// Puts the cards for `list` into `container` in order, rebuilding a card only when its job changed. Moving a card
// in the DOM takes the focus away from it, so the focus goes back to the element that had it.
function place(container, list, seen) {
  const active = container.contains(document.activeElement) ? document.activeElement : null;
  list.forEach((job, i) => {
    seen.add(job.id);
    let card = s.cards.get(job.id);
    const sig = jobSig(job);
    if (!card || card.sig !== sig) {
      const fresh = build(job, sig);
      if (card) card.el.replaceWith(fresh.el);
      card = fresh;
      s.cards.set(job.id, card);
    }
    if (card.update) card.update(job);
    const items = [...container.children].filter((c) => !c.classList.contains('empty'));
    if (items[i] !== card.el) container.insertBefore(card.el, items[i] || null);
  });
  if (active && active.isConnected && document.activeElement !== active) active.focus({preventScroll: true});
}

// Fields that change while a job runs are updated in place; anything else rebuilds the card. A queued card updates
// its position in place.
function jobSig(j) {
  const position = j.status === 'queued' ? null : j.position;
  return JSON.stringify([j.status, position, j.tool_label, j.created, j.started, j.finished, j.outputs, j.error, j.params, j.peak_gb]);
}

function build(job, sig) {
  const card = job.status === 'running' ? buildRunning(job) : job.status === 'queued' ? buildQueued(job) : buildFinished(job);
  card.sig = sig;
  card.job = job;
  return card;
}

function tool(job) {
  return store.get('toolsById').get(job.tool) || {id: job.tool, label: job.tool_label || job.tool, group: 'Edit'};
}

function promptOf(job) {
  const p = job.params || {};
  const text = ['prompt', 'text', 'caption'].map((k) => p[k]).find((v) => typeof v === 'string' && v.trim());
  if (text) return text;
  return job.tool === 'edit.mix' ? 'Video and music mix' : '';
}

function paramChips(p) {
  p = p || {};
  const c = [];
  if (p.size) c.push(p.size);
  if (p.seconds) c.push(p.seconds + ' s');
  if (p.duration) c.push(p.duration + ' s');
  if (p.audio === true) c.push('sound');
  if (p.takes) c.push(p.takes + (Number(p.takes) === 1 ? ' take' : ' takes'));
  if (p.steps) c.push(p.steps + ' steps');
  if (p.hq_decoder === true) c.push('final-quality decoder');
  if (p.instrumental === true) c.push('instrumental');
  if ('seed' in p) c.push(p.seed === null || p.seed === '' ? 'random seed' : 'seed ' + p.seed);
  return c.length ? h('div', {class: 'chips'}, c.map((x) => h('span', {class: 'chip num'}, x))) : null;
}

function jobTop(job, side) {
  const t = tool(job);
  const prompt = promptOf(job);
  return h('div', {class: 'job-top'}, toolIcon(t.group),
    h('div', {class: 'job-title'}, h('h3', null, job.tool_label || t.label),
      prompt ? h('p', {class: 'job-prompt', title: prompt}, prompt) : null, paramChips(job.params)),
    side);
}

function pill(status, text) {
  const [label, kind] = STATUS[status] || [status, 'idle'];
  return h('span', {class: 'pill pill-' + kind}, text || label);
}

function trackedDetails(job, kind, summary, body, openByDefault, cls) {
  const key = job.id + ':' + kind;
  const d = h('details', {class: cls || 'tech'}, h('summary', null, summary), body);
  d.open = openDetails.has(key) || (openByDefault && !openDetails.has(key + ':closed'));
  d.addEventListener('toggle', () => {
    if (d.open) {
      openDetails.add(key);
      openDetails.delete(key + ':closed');
    } else {
      openDetails.delete(key);
      openDetails.add(key + ':closed');
    }
  });
  return d;
}

function setLog(pre, text) {
  if (pre.textContent === text) return;
  const follow = !pre.textContent || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  pre.textContent = text;
  if (follow) requestAnimationFrame(() => {
    pre.scrollTop = pre.scrollHeight;
  });
}

// ---------- running ----------

function buildRunning(job) {
  const t = tool(job);
  const r = {};
  r.fill = h('div', {class: 'progress-fill'});
  r.bar = h('div', {class: 'progress', role: 'progressbar', 'aria-label': (job.tool_label || t.label) + ' progress', 'aria-valuemin': '0', 'aria-valuemax': '100'}, r.fill);
  r.stage = h('strong');
  r.left = h('span', {class: 'left'});
  r.elapsed = h('span');
  r.log = h('pre', {class: 'log'});
  const peak = store.toolPeak(job.tool);
  const logKey = job.id + ':runlog';
  const logBtn = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': 'false'});
  const showLog = (open) => {
    r.log.hidden = !open;
    logBtn.setAttribute('aria-expanded', String(open));
    logBtn.replaceChildren(icon('file', 'ic-sm'), open ? 'Hide log' : 'Show log');
    if (open) {
      openDetails.add(logKey);
      r.log.scrollTop = r.log.scrollHeight;
    } else openDetails.delete(logKey);
  };
  logBtn.addEventListener('click', () => showLog(r.log.hidden));
  const confirmSlot = h('div');
  const errSlot = h('div', {class: 'field-err', role: 'alert', hidden: true});
  const cancelBtn = h('button', {type: 'button', class: 'btn btn-sm btn-danger'}, icon('stop', 'ic-sm fill'), 'Cancel');
  cancelBtn.addEventListener('click', () => {
    cancelBtn.hidden = true;
    confirmSlot.replaceChildren(inlineConfirm({
      text: 'Cancel ' + (job.tool_label || t.label) + '? The part that is done is discarded and the memory is freed.',
      confirmLabel: 'Cancel job', cancelLabel: 'Keep running',
      onConfirm: async () => {
        if (!(await cancelJob(job, errSlot))) {
          confirmSlot.replaceChildren();
          cancelBtn.hidden = false;
        }
      },
      onCancel: () => {
        cancelBtn.hidden = false;
        cancelBtn.focus();
      },
    }));
  });
  const el = h('article', {class: 'card job job-running', 'aria-label': (job.tool_label || t.label) + ', running'},
    jobTop(job, h('div', {class: 'job-side'}, pill('running'), h('span', null, 'Started ' + fmtTime(job.started)))),
    r.bar,
    // only the stage is read out when it changes; the bar has the percentage
    h('div', {class: 'stage-line'}, h('span', {'aria-live': 'polite'}, r.stage), r.left),
    h('div', {class: 'job-meta'}, r.elapsed, peak ? h('span', null, 'Expected peak about ' + Math.round(peak) + ' GB') : null),
    confirmSlot, errSlot,
    h('div', {class: 'job-actions'}, logBtn, h('span', {class: 'spacer'}), cancelBtn),
    r.log);
  showLog(openDetails.has(logKey));
  const card = {el, refs: r};
  card.update = (j) => {
    card.job = j;
    const p = j.progress || {};
    const known = typeof p.fraction === 'number';
    r.bar.classList.toggle('indeterminate', !known);
    if (known) {
      const pct = Math.round(p.fraction * 100);
      r.fill.style.width = (p.fraction * 100).toFixed(1) + '%';
      r.bar.setAttribute('aria-valuenow', String(pct));
      r.bar.setAttribute('aria-valuetext', pct + '%' + (p.stage ? ', ' + p.stage : ''));
    } else {
      r.bar.removeAttribute('aria-valuenow');
      r.bar.setAttribute('aria-valuetext', p.stage || 'Starting');
    }
    r.stage.textContent = p.stage || 'Starting';
    r.left.textContent = [known ? Math.round(p.fraction * 100) + '%' : '', fmtLeft(p.eta_s)].filter(Boolean).join(' · ');
    r.elapsed.textContent = fmtDuration(store.liveElapsed(j)) + ' elapsed';
    setLog(r.log, j.log_tail || 'No output yet.');
  };
  card.tick = () => {
    r.elapsed.textContent = fmtDuration(store.liveElapsed(card.job)) + ' elapsed';
  };
  return card;
}

// ---------- queue ----------

function buildQueued(job) {
  const t = tool(job);
  const when = h('span', {class: 'q-when num'});
  const pos = h('span', {class: 'q-pos num', 'aria-hidden': 'true'});
  const label = job.tool_label || t.label;
  const card = {};
  const moveBtn = (dir, text, keys) => h('button', {type: 'button', class: 'btn btn-ghost btn-icon btn-sm',
    title: text + ' (' + keys + ')', 'data-move': dir, onclick: () => moveQueued(card.job, dir, dir)}, icon(dir, 'ic-sm'));
  const up = moveBtn('up', 'Move up', 'Option-Up Arrow');
  const down = moveBtn('down', 'Move down', 'Option-Down Arrow');
  const el = h('div', {class: 'q-item', role: 'group', tabindex: '0', 'aria-keyshortcuts': 'Alt+ArrowUp Alt+ArrowDown',
    onkeydown: (e) => {
      if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      e.preventDefault();
      const dir = e.key === 'ArrowUp' ? 'up' : 'down';
      const on = e.target.closest('[data-move]');
      moveQueued(card.job, dir, on ? on.dataset.move : 'row');
    }},
    pos,
    toolIcon(t.group, true),
    h('div', {class: 'q-main'}, h('b', null, label), h('span', {class: 'job-prompt'}, promptOf(job))),
    when,
    h('span', {class: 'q-actions'}, up, down,
      h('button', {type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Remove ' + label + ' from the queue',
        onclick: () => removeQueued(card.job)}, icon('x', 'ic-sm'))));
  Object.assign(card, {el, refs: {when, up, down}});
  card.update = (j) => {
    card.job = j;
    pos.textContent = String(j.position);
    up.setAttribute('aria-label', 'Move ' + label + ' up from position ' + j.position);
    down.setAttribute('aria-label', 'Move ' + label + ' down from position ' + j.position);
    el.setAttribute('aria-label', 'Position ' + j.position + ': ' + label + (promptOf(j) ? ', ' + promptOf(j) : '') +
      '. Option-Up or Option-Down Arrow moves it.');
  };
  return card;
}

// Moves a queued job on the server. `focus` says what gets the focus when the list shows the new order: the same
// move button ('up' or 'down'), or the row when that button is now disabled or the move came from the row.
async function moveQueued(job, to, focus) {
  if (!s || s.moving) return;
  const queued = store.get('jobs').filter((j) => j.status === 'queued' && !removing.has(j.id));
  if ((to === 'up' && job.position <= 1) || (to === 'down' && job.position >= queued.length)) return;
  const label = job.tool_label || job.tool;
  const ctx = s.ctx;
  s.moving = true;
  try {
    const r = await ctx.api.request('/api/jobs/' + encodeURIComponent(job.id) + '/move', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({to}),
    });
    if (s) {
      s.focusAfter = {id: job.id, position: r.position, focus};
      s.live.textContent = label + ' moved to position ' + r.position + ' of ' + queued.length + '.';
    }
  } catch (e) {
    toast('Could not move ' + label + ': ' + e.message, {kind: 'err'});
  }
  await store.refresh('jobs');
  if (s) s.moving = false;
}

// After a move, once the list shows the job at its new place, focus its row or the move button that was used.
function restoreFocus(queued) {
  const f = s.focusAfter;
  if (!f) return;
  const job = queued.find((j) => j.id === f.id);
  const card = s.cards.get(f.id);
  if (!job || !card) {
    s.focusAfter = null;
    return;
  }
  if (job.position !== f.position) return;
  s.focusAfter = null;
  const btn = card.refs[f.focus];
  (btn && !btn.disabled ? btn : card.el).focus({preventScroll: false});
}

// Start estimates: the running job's time left plus the median time of each job before it. Also the move buttons:
// disabled at the ends of the queue.
function updateWhen(run, queued) {
  let acc = run && run.progress && typeof run.progress.eta_s === 'number' ? run.progress.eta_s : run ? null : 0;
  for (const [i, j] of queued.entries()) {
    const card = s.cards.get(j.id);
    if (card) {
      card.refs.up.disabled = i === 0;
      card.refs.down.disabled = i === queued.length - 1;
    }
    const t = store.toolTime(j.tool);
    let text;
    if (j.waiting) text = j.waiting;  // first in line, held by prompt help or chat answers
    else if (acc === null) text = run ? 'after ' + (run.tool_label || run.tool) : 'waiting';
    else text = acc < 30 ? 'starts next' : 'starts in ' + fmtAbout(acc);
    if (t) text += ' · takes ' + fmtAbout(t.seconds);
    if (card) card.refs.when.textContent = text;
    acc = acc !== null && t ? acc + t.seconds : null;
  }
}

// The card or row next to el (else the one before it), for the focus when el goes away.
function neighbour(el) {
  const ok = (n) => n && !n.classList.contains('empty');
  const next = ok(el.nextElementSibling) ? el.nextElementSibling : ok(el.previousElementSibling) ? el.previousElementSibling : null;
  if (!next) return null;
  return next.classList.contains('q-item') ? next : next.querySelector('.job-actions button');
}

// After a card went away: the focus goes to its neighbour, else to the chosen filter.
function focusAfter(target) {
  if (!s) return;
  if (target && target.isConnected) target.focus();
  else if (!s.seg.hidden) s.seg.querySelector('[aria-pressed="true"]')?.focus();
  else s.empty.querySelector('button')?.focus();
}

// Removal cancels the job on the server at once: the worker may pick it up within seconds, so there is no Undo. The
// toast offers its settings again instead.
async function removeQueued(job) {
  const label = job.tool_label || job.tool;
  const ctx = s.ctx;
  const card = s.cards.get(job.id);
  const hadFocus = !!card && card.el.contains(document.activeElement);
  const target = card ? neighbour(card.el) : null;
  removing.add(job.id);
  render();
  if (hadFocus) focusAfter(target);
  try {
    await ctx.api.cancelJob(job.id);
    toast(label + ' was removed from the queue.', {action: {label: 'Use these settings', run: () => ctx.handoff.useSettings(job)}});
  } catch (e) {
    toast('Could not remove ' + label + ': ' + e.message, {kind: 'err'});
  }
  removing.delete(job.id);
  store.refresh('jobs');
}

async function cancelJob(job, errSlot) {
  try {
    await s.ctx.api.cancelJob(job.id);
    toast((job.tool_label || job.tool) + ' was cancelled. Its memory is free.');
    store.refresh('jobs');
    return true;
  } catch (e) {
    errSlot.hidden = false;
    errSlot.textContent = 'Cancel failed: ' + e.message;
    store.refresh('jobs');
    return false;
  }
}

// ---------- finished ----------

// Plain titles and fixes for failure modes the logs show (docs/DESIGN.md decision 8).
function diagnose(job) {
  const all = (job.error || '') + '\n' + (job.log_tail || '');
  if (/LocalEntryNotFoundError|Cannot find the requested files/.test(all)) {
    return {title: 'A model file is missing', fixes: [
      'Open Models and check the model this tool uses. Download it again if it shows as not installed.',
      'Press Use these settings, then press Generate in Create.']};
  }
  if (/signal 9|SIGKILL|code -9\b|code 137\b|Killed: 9/.test(all)) {
    return {title: 'macOS stopped the job: memory may have run out', fixes: [
      'Quit other apps that use a lot of memory.',
      'Press Use these settings and generate again. If it stops again, pick a smaller size, a shorter length or fewer takes.']};
  }
  if (/multiple of 64/.test(all)) {
    return {title: 'This video size does not work here', fixes: [
      'Use a clip from the video tools: their sizes are multiples of 64.',
      'Or crop the clip so both sides are a multiple of 64. Then press Use these settings and generate again.']};
  }
  return {title: 'The job failed', fixes: job.log_tail ? [
    'Open Technical details below. The last lines of the log name the cause.',
    'Press Use these settings, change what the log names, and generate again.'] : [
    'Press Use these settings and generate again.']};
}

function sendButton(job, o) {
  const targets = s.ctx.handoff.targets(o.kind);
  if (!targets.length) return null;
  const btn = h('button', {type: 'button', class: 'btn btn-sm', 'aria-haspopup': 'menu', 'aria-expanded': 'false'},
    icon('arrow', 'ic-sm'), 'Send to', icon('chev-down', 'ic-sm'));
  const handoff = s.ctx.handoff;
  btn.addEventListener('click', () => menu(btn, targets.map((t) => ({
    label: t.label, sub: t.sub, icon: KIND_ICON[o.kind],
    run: () => handoff.send({tool: t.tool, field: t.field, file: {url: o.url, name: o.name, kind: o.kind}}),
  }))));
  return btn;
}

function openImage(job, output) {
  const images = (job.outputs || []).filter((o) => o.kind === 'image' && s.ctx.api.safeFileUrl(o.url));
  lightbox(images.map((o) => ({url: o.url, name: o.name, caption: job.tool_label || job.tool})), Math.max(0, images.indexOf(output)));
}

function buildFinished(job) {
  const t = tool(job);
  const label = job.tool_label || t.label;
  const outputs = (job.outputs || []).map((o) => media(o, {
    onOpen: (out) => openImage(job, out),
    actions: [sendButton(job, o), downloadLink(o)],
  })).filter(Boolean);
  const errSlot = h('div', {class: 'field-err', role: 'alert', hidden: true});
  const confirmSlot = h('div');
  const params = Object.entries(job.params || {});
  let body = null;
  if (job.status === 'done') {
    body = outputs.length ? h('div', {class: 'outputs' + (job.outputs.every((o) => o.kind === 'audio') ? ' wide' : '')}, outputs) : null;
  } else if (job.status === 'failed') {
    const d = diagnose(job);
    // the Python exception name says nothing to the user
    body = errorBox({title: d.title, text: (job.error || '').replace(/^[A-Z]\w*(Error|Exception): /, ''), fixes: d.fixes});
    if (job.log_tail) body.append(trackedDetails(job, 'log', 'Technical details', h('pre', {class: 'log'}, job.log_tail), false));
  } else if (job.error) {
    body = h('p', {class: 'muted small'}, job.error);
  }
  // Loads the job's settings into Create. It does not start a job.
  const useBtn = h('button', {type: 'button', class: 'btn btn-sm', onclick: () => s.ctx.handoff.useSettings(job)},
    icon(job.status === 'done' ? 'undo' : 'refresh', 'ic-sm'), 'Use these settings');
  const delBtn = h('button', {type: 'button', class: 'btn btn-sm btn-ghost btn-icon', 'aria-label': 'Delete this job and its files'}, icon('trash', 'ic-sm'));
  delBtn.addEventListener('click', () => {
    delBtn.hidden = true;
    confirmSlot.replaceChildren(inlineConfirm({
      text: 'Delete this job and its files in outputs/' + job.id + '/? This cannot be undone.',
      confirmLabel: 'Delete',
      onConfirm: async () => {
        if (!(await deleteJob(job, errSlot))) {
          confirmSlot.replaceChildren();
          delBtn.hidden = false;
        }
      },
      onCancel: () => {
        delBtn.hidden = false;
        delBtn.focus();
      },
    }));
  });
  const side = h('div', {class: 'job-side'}, pill(job.status),
    h('span', {class: 'num'}, fmtTime(job.finished || job.created) + (typeof job.elapsed === 'number' && job.elapsed > 0 ? ' · took ' + fmtClock(job.elapsed) : '')),
    // a process that stops at once reports a peak of about 0 GB, which says nothing
    typeof job.peak_gb === 'number' && job.peak_gb >= 0.1 ? h('span', {class: 'num'}, 'peak ' + fmtGb(job.peak_gb)) : null);
  const el = h('article', {class: 'card job', 'aria-label': label + ', ' + (STATUS[job.status] || [job.status])[0]},
    jobTop(job, side), body,
    params.length ? trackedDetails(job, 'params', 'All settings',
      h('dl', {class: 'kv'}, params.map(([k, v]) => [h('dt', null, k), h('dd', null, fmtValue(v))])), false, 'tech') : null,
    confirmSlot, errSlot,
    h('div', {class: 'job-actions'}, useBtn, h('span', {class: 'spacer'}), h('span', {class: 'faint small mono'}, job.id), delBtn));
  return {el, refs: {}};
}

async function deleteJob(job, errSlot) {
  const card = s.cards.get(job.id);
  const target = card ? neighbour(card.el) : null;
  try {
    await s.ctx.api.deleteJob(job.id);
    for (const k of [...openDetails]) if (k.startsWith(job.id + ':')) openDetails.delete(k);
    toast('Deleted the job and its files.');
    await store.refresh('jobs');
    focusAfter(target);
    return true;
  } catch (e) {
    errSlot.hidden = false;
    errSlot.textContent = 'Delete failed: ' + e.message;
    store.refresh('jobs');
    return false;
  }
}

function tick() {
  if (!s) return;
  for (const card of s.cards.values()) if (card.tick) card.tick();
}

export default {
  id: 'jobs',
  title: 'Jobs',
  subtitle: 'One job runs at a time, so each model gets the memory.',
  mount,
  unmount,
};
