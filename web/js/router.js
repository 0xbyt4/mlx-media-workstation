// Hash routes: #/create, #/jobs, #/chat, #/library, #/models, #/settings, with optional params
// (#/create?tool=video.extend). Cmd-1 to Cmd-6 (Ctrl on other systems) switch sections.

import {lsGet, lsSet} from './ui.js';

export const VIEWS = [
  {id: 'create', title: 'Create', icon: 'create'},
  {id: 'jobs', title: 'Jobs', icon: 'jobs'},
  {id: 'chat', title: 'Chat', icon: 'chat'},
  {id: 'library', title: 'Library', icon: 'library'},
  {id: 'models', title: 'Models', icon: 'models'},
  {id: 'settings', title: 'Settings', icon: 'settings'},
];

let listener = null;
let route = {view: null, params: {}};

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
  if (!r) {
    history.replaceState(null, '', href(route.view || 'create'));
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

export function start(onRoute) {
  listener = onRoute;
  window.addEventListener('hashchange', handle);
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || !/^[1-6]$/.test(e.key)) return;
    if (document.querySelector('dialog[open], .lightbox')) return;
    e.preventDefault();
    go(VIEWS[Number(e.key) - 1].id);
  });
  if (!parse(location.hash)) {
    const last = lsGet('ws.view');
    history.replaceState(null, '', href(VIEWS.some((v) => v.id === last) ? last : 'create'));
  }
  handle();
}

export const router = {VIEWS, parse, href, go, replace, current, start};

export default router;
