// Boots the app: theme, store, router, the sidebar (sections, job dock, memory meter), the toolbar and the views.
// The profile (store.profile()) picks the sections: the chat profile shows Chat, Models and Settings, with no job dock,
// no job toasts and no job part in the memory meter unless a job runs. The router starts once the profile is known.

import {api} from './api.js';
import {store} from './store.js';
import {ui, h, append, icon, toolIcon, fmtLeft} from './ui.js';
import {router} from './router.js';
import {handoff} from './handoff.js';
import create from './views/create.js';
import jobs from './views/jobs.js';
import chat from './views/chat.js';
import library from './views/library.js';
import models from './views/models.js';
import settings from './views/settings.js';

const MODULES = {create, jobs, chat, library, models, settings};
const THEME_ICON = {system: 'monitor', light: 'sun', dark: 'moon'};
const NEXT_THEME = {system: 'light', light: 'dark', dark: 'system'};
const BRAND_SUB = {full: 'Local media models on MLX', chat: 'Local chat on MLX'};
const $ = (id) => document.getElementById(id);

const shell = {
  setTitle(text) {
    $('view-title').textContent = text;
  },
  setSubtitle(text) {
    $('view-sub').textContent = text || '';
  },
};
const ctx = {api, store, ui, router, handoff, shell};
let active = null;
let started = false; // the router runs once the profile is known

const chatOnly = () => store.profile() === 'chat';

// ---------- views ----------

function subtitleOf(mod) {
  return typeof mod.subtitle === 'function' ? mod.subtitle(ctx) : mod.subtitle || '';
}

function show(route) {
  const mod = MODULES[route.view];
  if (active && active.id === route.view) {  // the same view stays mounted, so no draft or playback is lost
    if (mod.update) mod.update(route.params);
    return;
  }
  const switching = !!active;
  if (active) {
    try {
      active.mod.unmount();
    } catch (e) {
      console.error('Unmount of ' + active.id + ' failed', e);
    }
  }
  ui.closeMenu();
  const area = $('view');
  area.replaceChildren();
  area.scrollTop = 0;
  shell.setTitle(mod.title);
  shell.setSubtitle(subtitleOf(mod));
  const root = h('section', {class: 'view'});
  area.append(root);
  active = {id: route.view, mod};
  renderNav();
  try {
    mod.mount(root, ctx, route.params);
  } catch (e) {
    console.error('Mount of ' + route.view + ' failed', e);
    root.replaceChildren(ui.errorBox({title: 'This section could not open', text: String(e && e.message ? e.message : e)}));
  }
  // The focus was in the old view (Cmd-1 to Cmd-6 from a field): start the new one from its top, not from the page.
  if (switching && (!document.activeElement || document.activeElement === document.body)) area.focus({preventScroll: true});
}

// ---------- sidebar: sections ----------

function renderNav() {
  const nav = $('nav');
  const jobsList = store.get('jobs');
  const activeJobs = jobsList.filter((j) => store.ACTIVE.has(j.status)).length;
  const modelsData = store.get('models');
  const missing = !!(modelsData && modelsData.models.some((m) => store.shownModel(m) && !m.installed));
  const focused = document.activeElement && nav.contains(document.activeElement) ? document.activeElement.getAttribute('data-view') : null;
  const list = started ? router.views() : []; // no sections before the profile is known, so none flash
  nav.replaceChildren(...list.map((v, i) => {
    let extra;
    if (v.id === 'jobs' && activeJobs) extra = [h('span', {class: 'badge', 'aria-hidden': 'true'}, String(activeJobs)), h('span', {class: 'sr-only'}, ', ' + activeJobs + ' active')];
    else if (v.id === 'models' && missing) extra = [h('span', {class: 'nav-dot', title: 'Some models are not installed'}), h('span', {class: 'sr-only'}, 'Some models are not installed')];
    else extra = h('span', {class: 'nav-kbd', 'aria-hidden': 'true'}, '⌘' + (i + 1));
    return h('button', {
      type: 'button', class: 'nav-item', 'data-view': v.id,
      'aria-current': active && active.id === v.id ? 'page' : null,
      'aria-keyshortcuts': 'Meta+' + (i + 1),
      onclick: () => router.go(v.id),
    }, icon(v.icon), h('span', {class: 'nav-label'}, v.title), extra);
  }));
  const again = focused && nav.querySelector('[data-view="' + focused + '"]');
  if (again) again.focus();
}

// Arrow keys move between sections; Home and End jump to the first and last.
function navKeys(e) {
  const items = [...$('nav').querySelectorAll('.nav-item')];
  const i = items.indexOf(document.activeElement);
  if (i < 0) return;
  let next = null;
  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (i + 1) % items.length;
  else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (i - 1 + items.length) % items.length;
  else if (e.key === 'Home') next = 0;
  else if (e.key === 'End') next = items.length - 1;
  if (next === null) return;
  e.preventDefault();
  items[next].focus();
}

// ---------- sidebar: job dock ----------

function renderDock() {
  const dock = $('dock');
  const pillEl = $('run-pill');
  dock.hidden = !started || chatOnly();
  if (dock.hidden) {
    dock.replaceChildren();
    pillEl.hidden = true;
    return;
  }
  const run = store.runningJob();
  const queued = store.queuedJobs().length;
  if (!run) {
    dock.replaceChildren(h('div', {class: 'dock-empty'}, queued ? queued + ' queued. The next job starts in a moment.' : 'No job running. The queue is empty.'));
    pillEl.hidden = true;
    return;
  }
  const tool = store.get('toolsById').get(run.tool);
  const label = run.tool_label || (tool ? tool.label : run.tool);
  const p = run.progress || {};
  const known = typeof p.fraction === 'number';
  const pct = known ? Math.round(p.fraction * 100) : null;
  const fill = h('div', {class: 'progress-fill', style: {width: known ? (p.fraction * 100).toFixed(1) + '%' : null}});
  dock.replaceChildren(h('button', {
    type: 'button', class: 'dock', onclick: () => router.go('jobs'),
    'aria-label': 'Running: ' + label + (known ? ', ' + pct + ' percent' : '') + '. Open Jobs',
  },
  h('span', {class: 'dock-top'}, toolIcon(tool ? tool.group : 'Edit', true), h('span', {class: 'dock-label'}, label),
    known ? h('span', {class: 'num faint'}, pct + '%') : h('span', {class: 'spinner sm faint'})),
  h('div', {class: 'progress sm' + (known ? '' : ' indeterminate')}, fill),
  h('span', {class: 'dock-sub'}, h('span', null, p.stage || 'Starting'), h('span', null, fmtLeft(p.eta_s))),
  queued ? h('span', {class: 'dock-sub'}, h('span', null, queued + ' queued')) : null));
  pillEl.hidden = false;
  pillEl.replaceChildren(h('span', {class: 'spinner sm'}), h('span', {class: 'num'}, label + (known ? ' ' + pct + '%' : '')));
  pillEl.setAttribute('aria-label', 'Running: ' + label + '. Open Jobs');
}

// ---------- sidebar: memory meter ----------

function renderMem() {
  const box = $('mem');
  const sys = store.get('system');
  if (!sys) {
    box.replaceChildren(h('div', {class: 'mem-head'}, h('span', null, 'Memory'), h('span', null, store.get('offline') ? 'offline' : '...')));
    return;
  }
  const total = sys.mem_total_gb;
  const used = Math.max(0, total - sys.mem_free_gb);
  const llm = store.get('llm');
  const qwen = llm && llm.state === 'ready' ? Math.min(llm.peak_gb, used) : 0;
  const run = store.runningJob();
  const showJob = !chatOnly() || !!run; // the chat profile shows a job only while one runs (from a full window)
  const jobPeak = run ? (llm && llm.media_peak_gb) || store.toolPeak(run.tool) || 0 : 0;
  const job = Math.min(jobPeak, Math.max(0, used - qwen));
  const other = Math.max(0, used - job - qwen);
  const w = (x) => (total > 0 ? Math.min(100, (x / total) * 100).toFixed(1) + '%' : '0%');
  const g = (x) => x.toFixed(1);
  const swap = sys.swap_used_gb || 0;
  box.replaceChildren();
  append(box, [
    h('div', {class: 'mem-head'}, h('span', null, 'Memory'), h('span', {class: 'num'}, g(used) + ' of ' + Math.round(total) + ' GB')),
    h('div', {class: 'meter', role: 'img',
      'aria-label': 'Memory: ' + (showJob ? 'job ' + g(job) + ' GB, ' : '') + (llm ? llm.model : 'local model') + ' ' + g(qwen) + ' GB, other apps ' + g(other) + ' GB of ' + Math.round(total) + ' GB'},
    showJob ? h('span', {class: 'seg-job', style: {width: w(job)}}) : null, h('span', {class: 'seg-qwen', style: {width: w(qwen)}}), h('span', {class: 'seg-other', style: {width: w(other)}})),
    h('div', {class: 'mem-legend'},
      showJob ? h('span', {title: 'The running job, at its expected peak'}, h('i', {class: 'seg-job'}), 'Job ' + g(job)) : null,
      h('span', {title: llm ? llm.model + ' (' + llm.state + ')' : ''}, h('i', {class: 'seg-qwen'}), llm && llm.state === 'loading' ? 'Qwen loading' : 'Qwen ' + g(qwen)),
      h('span', null, h('i', {class: 'seg-other'}), 'Other ' + g(other))),
    swap >= 0.5 ? h('div', {class: 'mem-swap' + (swap >= 4 ? ' warn' : '')}, 'Swap ' + g(swap) + ' GB' +
      (swap >= 4 ? (chatOnly() ? ': answers come slower' : ': jobs run slower') : ' in use')) : null]);
}

// ---------- toolbar ----------

function renderTheme(mode) {
  const btn = $('theme-btn');
  btn.replaceChildren(icon(THEME_ICON[mode]));
  const name = mode[0].toUpperCase() + mode.slice(1);
  btn.setAttribute('aria-label', 'Theme: ' + name + '. Change theme');
  btn.title = 'Theme: ' + name;
}

// Server offline, or a server that restarted with a new session: this page's cookie no longer works (HTTP 401).
function renderOffline() {
  const el = $('offline');
  const off = store.get('offline');
  const out = !off && store.get('signedOut');
  el.hidden = !off && !out;
  el.replaceChildren(...(off ? [icon('alert'), 'Server offline. Retrying...']
    : out ? [icon('alert'), 'This window is signed out: the app restarted. Open Workstation again, or open the new link that start.py printed.']
      : []));
}

function renderTitle() {
  if (chatOnly()) {
    document.title = 'Workstation Chat';
    return;
  }
  const n = store.get('jobs').filter((j) => store.ACTIVE.has(j.status)).length;
  document.title = (n ? '(' + n + ') ' : '') + 'Workstation';
}

function renderBrand() {
  document.querySelector('.brand-sub').textContent = BRAND_SUB[store.profile()];
}

// Shows the sections of the profile; starts the router the first time.
function applyProfile() {
  router.setProfile(store.profile());
  if (!started) {
    started = true;
    router.start(show);
  }
  renderBrand();
  renderNav();
  renderDock();
  renderMem();
  renderTitle();
  refreshSubtitle();
}

function refreshSubtitle() {
  if (active && typeof active.mod.subtitle === 'function') shell.setSubtitle(subtitleOf(active.mod));
}

// ---------- boot ----------

function guardNavigation() {
  // Nothing may navigate the app window away (pywebview has no back button): /files links only download, and files
  // dropped outside a drop zone or file input are ignored.
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (a && a.getAttribute('href').startsWith('/files/') && !a.hasAttribute('download')) e.preventDefault();
  }, true);
  const isFileInput = (t) => t instanceof HTMLInputElement && t.type === 'file';
  window.addEventListener('dragover', (e) => {
    if (!isFileInput(e.target)) e.preventDefault();
  });
  window.addEventListener('drop', (e) => {
    if (!isFileInput(e.target)) e.preventDefault();
  });
}

function boot() {
  renderTheme(ui.getTheme());
  ui.onTheme(renderTheme);
  $('theme-btn').addEventListener('click', () => ui.setTheme(NEXT_THEME[ui.getTheme()]));
  $('run-pill').addEventListener('click', () => router.go('jobs'));
  $('nav').addEventListener('keydown', navKeys);
  // the skip link moves focus only: changing the hash to #view would be read as a route
  document.querySelector('.skip').addEventListener('click', (e) => {
    e.preventDefault();
    $('view').focus();
  });
  guardNavigation();

  store.on('jobs', () => {
    renderNav();
    renderDock();
    renderMem();
    renderTitle();
    refreshSubtitle();
  });
  store.on('tools', () => {
    renderDock();
    refreshSubtitle();
  });
  store.on('models', () => {
    renderNav();
    refreshSubtitle();
  });
  store.on('system', renderMem);
  store.on('llm', renderMem);
  store.on('offline', () => {
    renderOffline();
    renderMem();
  });
  store.on('signedout', renderOffline);
  store.on('job:finished', (job) => {
    if (chatOnly() || (active && active.id === 'jobs')) return;
    const label = job.tool_label || job.tool;
    const text = job.status === 'done' ? label + ' finished.' : job.status === 'failed' ? label + ' failed.' : label + ' was cancelled.';
    ui.toast(text, {kind: job.status === 'failed' ? 'err' : null, action: {label: 'Open in Jobs', run: () => router.go('jobs')}});
  });
  setInterval(() => store.emit('tick'), 1000);
  store.on('profile', applyProfile);

  renderNav();
  renderDock();
  renderMem();
  store.start();
  if (store.get('profile')) applyProfile(); // a window with ?profile= in its address
  else {
    // the profile comes with the first /api/system answer; while the server does not answer, the full profile shows
    const off = store.on('offline', (isOff) => {
      off();
      if (isOff && !started) applyProfile();
    });
  }
}

boot();
