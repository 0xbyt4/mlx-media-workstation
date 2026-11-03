// Hash routes: #/create, #/jobs, #/chat, #/library, #/models, #/settings, with optional params
// (#/create?tool=video.extend). Cmd-1 to Cmd-6 (Ctrl on other systems) switch sections.
// The chat profile shows only Chat, Models and Settings (Cmd-1 to Cmd-3); a route to another section opens Chat.

import {lsGet, lsSet} from './ui.js';

export const VIEWS = [
  {id: 'create', title: 'Create', icon: 'create'},
  {id: 'jobs', title: 'Jobs', icon: 'jobs'},
  {id: 'chat', title: 'Chat', icon: 'chat'},
  {id: 'library', title: 'Library', icon: 'library'},
  {id: 'models', title: 'Models', icon: 'models'},
  {id: 'settings', title: 'Settings', icon: 'settings'},
];
// the sections of each profile, and the one a profile opens on
const SHOWN = {full: VIEWS.map((v) => v.id), chat: ['chat', 'models', 'settings']};
const HOME = {full: 'create', chat: 'chat'};

let listener = null;
let route = {view: null, params: {}};
let profile = 'full';

// The sections of the current profile, in sidebar order.
export function views() {
  return VIEWS.filter((v) => SHOWN[profile].includes(v.id));
}

export function home() {
  return HOME[profile];
}

function shown(view) {
  return SHOWN[profile].includes(view);
}

export function parse(hash) {
  const m = /^#\/([a-z]+)(?:\?(.*))?$/.exec(hash || '');
  if (!m || !VIEWS.some((v) => v.id === m[1])) return null;
  return {view: m[1], params: Object.fromEntries(new URLSearchParams(m[2] || ''))};
}

export function href(view, params) {
  const q = params ? new URLSearchParams(Object.entries(params).filter(([, v]) => v !== null && v !== undefined)).toString() : '';
  return '#/' + view + (q ? '?' + q : '');
}

function handle() {
  const r = parse(location.hash);
  if (!r || !shown(r.view)) {
    // an unknown route stays on the open section; a section this profile does not show opens its home
    history.replaceState(null, '', href(!r && route.view && shown(route.view) ? route.view : home()));
    return handle();
  }
  route = r;
  lsSet('ws.view', r.view);
  if (listener) listener(r);
}

// Opens a view. The same route again still notifies, so a handoff reaches a Create view that is already open.
export function go(view, params) {
  const target = href(view, params);
  if (location.hash === target) handle();
  else location.hash = target;
}

// Changes the params in the address without a new history entry and without notifying (a view keeps its place).
export function replace(view, params) {
  const target = href(view, params);
  if (location.hash !== target) history.replaceState(null, '', target);
  route = parse(target);
}

export function current() {
  return route;
}

// Switches the profile. When the open section is not in the new profile, its home opens.
export function setProfile(next) {
  if (!SHOWN[next] || next === profile) return;
  profile = next;
  if (listener && route.view && !shown(route.view)) handle();
}

export function start(onRoute) {
  listener = onRoute;
  window.addEventListener('hashchange', handle);
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || !/^[1-9]$/.test(e.key)) return;
    const list = views();
    const n = Number(e.key);
    if (n > list.length) return;
    if (document.querySelector('dialog[open], .lightbox')) return;
    e.preventDefault();
    go(list[n - 1].id);
  });
  if (!parse(location.hash)) {
    const last = lsGet('ws.view');
    history.replaceState(null, '', href(shown(last) ? last : home()));
  }
  handle();
}

export const router = {VIEWS, views, home, parse, href, go, replace, current, setProfile, start};

export default router;
