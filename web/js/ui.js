// DOM helpers, formatters, toasts, dialogs, menus, the fit rule, media renderers and the theme.

import {safeFileUrl} from './api.js';

// ---------- DOM ----------

// Builds an element. String attributes go through setAttribute, booleans and functions are set as
// properties/listeners (onclick, oninput, ...), `style` may be an object, children become text nodes.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (v === true) {
        if (k in el) el[k] = true;
        else el.setAttribute(k, '');
      } else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  return el;
}

const SVG = 'http://www.w3.org/2000/svg';

// An icon from the sprite in index.html (ids "i-<name>").
export function icon(name, cls) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', 'ic' + (cls ? ' ' + cls : ''));
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}

export const GROUP_ORDER = ['Image', 'Voice', 'Music', 'Video', 'Edit'];
const GROUP_ICON = {Image: 'image', Voice: 'voice', Music: 'music', Video: 'video', Edit: 'edit'};

export function toolIcon(group, small) {
  const g = GROUP_ICON[group] ? group : 'Edit';
  return h('span', {class: 'tool-ico t-' + g.toLowerCase() + (small ? ' sm' : ''), 'aria-hidden': 'true'},
    icon(GROUP_ICON[g], small ? 'ic-sm' : null));
}

// Tools in sidebar order, grouped: [{name, tools}].
export function groupTools(tools) {
  const groups = new Map();
  for (const t of tools) {
    if (!groups.has(t.group)) groups.set(t.group, []);
    groups.get(t.group).push(t);
  }
  const rank = (g) => (GROUP_ORDER.indexOf(g) < 0 ? 99 : GROUP_ORDER.indexOf(g));
  return [...groups.keys()].sort((a, b) => rank(a) - rank(b)).map((name) => ({name, tools: groups.get(name)}));
}

export function grow(ta) {
  if (!ta.isConnected) return;
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * 0.6)) + 'px';
}

// ---------- storage (per viewer conveniences only) ----------

export function lsGet(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch {
    return null;
  }
}

export function lsSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage unavailable: nothing to remember
  }
}

export function lsRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // storage unavailable
  }
}

// ---------- formatters ----------

// 42s, 3m 05s, 1h 02m
export function fmtDuration(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '';
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
}

// 3:05
export function fmtClock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

// "a few seconds", "about 40 s", "about 3.5 min"
export function fmtAbout(sec) {
  if (sec < 15) return 'a few seconds';
  if (sec < 60) return 'about ' + Math.round(sec / 5) * 5 + ' s';
  if (sec >= 5400) return 'about ' + (Math.round(sec / 1800) / 2) + ' h';
  const m = Math.round(sec / 30) / 2;
  return 'about ' + m + ' min';
}

export function fmtLeft(sec) {
  if (typeof sec !== 'number' || !isFinite(sec)) return '';
  return sec < 60 ? 'under 1 min left' : fmtAbout(sec) + ' left';
}

export function fmtGb(v, digits = 1) {
  return typeof v === 'number' && isFinite(v) ? v.toFixed(digits) + ' GB' : '-';
}

export function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
}

// 21:31 today, "Oct 31 21:31" on other days
export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const time = d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
  if (d.toDateString() === new Date().toDateString()) return time;
  return d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'}) + ' ' + time;
}

export function timeAgo(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  if (s < 7 * 86400) return Math.floor(s / 86400) + (s < 2 * 86400 ? ' day ago' : ' days ago');
  return fmtTime(iso);
}

export function fmtValue(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

// ---------- the fit rule (docs/DESIGN.md decision 3) ----------

const MAC_SIZES = [16, 24, 32, 36, 48, 64, 96, 128, 192, 256];

// Peak at most memory minus the reserve: Fits; up to memory minus 2 GB: Tight; else Needs N GB (next Mac size).
export function fit(peakGb, memGb, reserveGb = 8) {
  if (typeof peakGb !== 'number' || !memGb) return null;
  const mem = Math.round(memGb);
  if (peakGb <= memGb - reserveGb) return {k: 'ok', short: 'Fits', text: 'Fits this Mac (' + mem + ' GB)', pill: 'ok'};
  if (peakGb <= memGb - 2) {
    return {k: 'tight', short: 'Tight', text: 'Tight on ' + mem + ' GB: quit other apps first', pill: 'warn'};
  }
  const need = MAC_SIZES.find((t) => peakGb <= t - reserveGb) || Math.ceil(peakGb + reserveGb);
  return {k: 'no', short: 'Needs ' + need + ' GB', text: 'Needs ' + need + ' GB', pill: 'err', need};
}

// ---------- text ----------

// A prompt as people read it, for display only: the stored text, "Use these settings", the model and the Library
// search keep the raw text. Klein Edit's <ref_image_N> (0-based, in upload order) becomes "reference N+1". Fish
// Drama 3's <|speaker:N|> tags go, or become "Speaker N:" (as the voice reference fields name them) when the text has
// more than one speaker.
export function readablePrompt(text) {
  if (typeof text !== 'string') return text;
  const t = text.replace(/<ref_image_(\d+)>/g, (_, n) => 'reference ' + (Number(n) + 1));
  const speakers = new Set([...t.matchAll(/<\|speaker:(\d+)\|>/g)].map((m) => m[1]));
  return t.replace(/<\|speaker:(\d+)\|>[ \t]*/g, (_, n) => (speakers.size > 1 ? 'Speaker ' + n + ': ' : '')).trim();
}

// ---------- leaving ----------

const leaving = new WeakMap(); // element -> the token of its running leave

// Plays the leave animation of el (motion.css: .is-leaving), then runs done, which removes el by default. Nothing
// waits for it: el is inert at once (no clicks, no focus), and a timer finishes also when no animation runs.
// stayOpen(el) keeps an element that opens again before the end.
export function leave(el, done = () => el.remove()) {
  if (!el.isConnected || leaving.has(el)) return;
  const token = {};
  leaving.set(el, token);
  el.classList.add('is-leaving');
  el.inert = true;
  const finish = (e) => {
    if (e && e.target !== el) return; // the end of a child's animation
    el.removeEventListener('animationend', finish);
    clearTimeout(timer);
    if (leaving.get(el) !== token) return;
    stayOpen(el);
    done();
  };
  const timer = setTimeout(finish, 400);
  el.addEventListener('animationend', finish);
}

export function stayOpen(el) {
  leaving.delete(el);
  el.classList.remove('is-leaving');
  el.inert = false;
}

// ---------- toasts ----------

// toast('Saved') or toast('Failed', {kind: 'err'}) or toast('Removed', {action: {label: 'Undo', run}})
export function toast(msg, opts = {}) {
  const box = document.getElementById('toasts');
  if (!box) return () => {};
  const el = h('div', {class: 'toast' + (opts.kind ? ' ' + opts.kind : '')}, h('span', null, msg));
  const close = () => {
    clearTimeout(timer);
    leave(el); // fades out (motion.css), then goes
  };
  if (opts.action) {
    el.appendChild(h('button', {type: 'button', onclick: () => {
      if (el.classList.contains('is-leaving')) return; // a second Enter during the fade-out
      close();
      opts.action.run();
    }}, opts.action.label));
  }
  box.appendChild(el);
  while (box.children.length > 4) box.firstChild.remove();
  const ms = opts.timeout || (opts.action ? 8000 : opts.kind === 'err' ? 7000 : 4500);
  let timer = setTimeout(close, ms);
  // the toast stays while the pointer or the focus is on it, so its action button can still be reached
  const hold = () => clearTimeout(timer);
  const release = () => {
    clearTimeout(timer);
    if (!el.matches(':hover') && !el.contains(document.activeElement)) timer = setTimeout(close, 3000);
  };
  el.addEventListener('mouseenter', hold);
  el.addEventListener('focusin', hold);
  el.addEventListener('mouseleave', release);
  el.addEventListener('focusout', () => setTimeout(release));
  return close;
}

// ---------- dialogs and inline confirms ----------

// A modal confirm in a <dialog> (Escape cancels). Resolves true when confirmed.
export function confirm({title, text, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false}) {
  return new Promise((resolve) => {
    const back = document.activeElement;
    const ok = h('button', {type: 'button', class: 'btn ' + (danger ? 'btn-danger-solid' : 'btn-primary')}, confirmLabel);
    const no = h('button', {type: 'button', class: 'btn'}, cancelLabel);
    const dlg = h('dialog', {class: 'dialog', 'aria-labelledby': 'dlg-title'},
      h('h2', {id: 'dlg-title'}, title),
      text ? h('p', null, text) : null,
      h('div', {class: 'dialog-actions'}, no, ok));
    let result = false;
    // a short leave animation first; the result comes after the focus is back, so the caller can move it on
    const shut = () => leave(dlg, () => dlg.close());
    ok.addEventListener('click', () => {
      result = true;
      shut();
    });
    no.addEventListener('click', shut);
    dlg.addEventListener('cancel', (e) => {
      e.preventDefault(); // Escape: the same leave as Cancel
      shut();
    });
    dlg.addEventListener('close', () => {
      dlg.remove();
      if (back && back.isConnected) back.focus();
      resolve(result);
    });
    document.body.appendChild(dlg);
    dlg.showModal();
    no.focus();
  });
}

// An inline confirm row (docs/DESIGN.md decision 12). Returns the element; the caller places it.
export function inlineConfirm({text, confirmLabel, cancelLabel = 'Keep', onConfirm, onCancel}) {
  const yes = h('button', {type: 'button', class: 'btn btn-sm btn-danger-solid'}, confirmLabel);
  const no = h('button', {type: 'button', class: 'btn btn-sm'}, cancelLabel);
  const el = h('div', {class: 'confirm', role: 'alert'}, h('span', null, text), no, yes);
  yes.addEventListener('click', async () => {
    yes.disabled = no.disabled = true;
    try {
      await onConfirm();
    } finally {
      if (yes.isConnected) yes.disabled = no.disabled = false;
    }
  });
  no.addEventListener('click', () => {
    el.remove();
    if (onCancel) onCancel();
  });
  queueMicrotask(() => {
    no.focus({preventScroll: true});
    el.scrollIntoView({block: 'nearest'});  // the whole row, also at the bottom of a scrolled drawer
  });
  return el;
}

// ---------- empty states and error boxes ----------

export function emptyState({icon: name = 'info', title, text, actions}) {
  return h('div', {class: 'empty'}, icon(name), h('h3', null, title), text ? h('p', null, text) : null,
    actions && actions.length ? h('div', {class: 'banner-actions'}, actions) : null);
}

// Title, what happened, numbered fixes, action buttons and a collapsed technical log.
export function errorBox({title, text, fixes, actions, log}) {
  return h('div', {class: 'err-box', role: 'group', 'aria-label': 'Error'},
    h('h4', null, icon('alert'), title),
    text ? h('p', null, text) : null,
    fixes && fixes.length ? h('div', null, h('b', null, 'How to fix it'), h('ol', null, fixes.map((f) => h('li', null, f)))) : null,
    actions && actions.length ? h('div', {class: 'banner-actions'}, actions) : null,
    log ? h('details', {class: 'tech'}, h('summary', null, 'Technical details'), h('pre', {class: 'log'}, log)) : null);
}

// ---------- popup menu ----------

let openMenu = null;

export function closeMenu() {
  if (!openMenu) return;
  const {el, anchor, cleanup} = openMenu;
  openMenu = null;
  cleanup();
  leave(el);
  anchor.setAttribute('aria-expanded', 'false');
}

// Opens a menu under anchor; items: [{label, sub, icon, run, checked}]. Items with a boolean `checked` are a choice
// of one (menuitemradio, a check mark on the chosen one, which gets the focus). A second call on the same anchor
// closes it.
export function menu(anchor, items) {
  const again = openMenu && openMenu.anchor === anchor;
  closeMenu();
  if (again || !items.length) return;
  const buttons = items.map((it) => {
    const radio = typeof it.checked === 'boolean';
    const mark = radio ? icon('check', 'ic-sm' + (it.checked ? '' : ' menu-blank')) : it.icon ? icon(it.icon, 'ic-sm') : null;
    return h('button', {
      type: 'button', role: radio ? 'menuitemradio' : 'menuitem', 'aria-checked': radio ? String(it.checked) : null,
      onclick: () => {
        closeMenu();
        anchor.focus();
        it.run();
      },
    }, mark, h('span', null, it.label), it.sub ? h('span', {class: 'menu-sub'}, it.sub) : null);
  });
  const el = h('div', {class: 'menu-pop', role: 'menu'}, buttons);
  document.body.appendChild(el);
  const r = anchor.getBoundingClientRect();
  const mh = el.offsetHeight, mw = el.offsetWidth;
  const top = r.bottom + 4 + mh > window.innerHeight ? r.top - 4 - mh : r.bottom + 4;
  el.style.top = Math.max(4, top) + 'px';
  el.style.left = Math.max(4, Math.min(r.left, window.innerWidth - mw - 4)) + 'px';
  anchor.setAttribute('aria-expanded', 'true');

  const onDown = (e) => {
    if (!el.contains(e.target) && !anchor.contains(e.target)) closeMenu();
  };
  const onKey = (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeMenu();
      anchor.focus();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = buttons.length;
      const down = e.key === 'ArrowDown';
      buttons[i < 0 ? (down ? 0 : n - 1) : (i + (down ? 1 : n - 1)) % n].focus();
    } else if (e.key === 'Tab') {
      closeMenu();
    }
  };
  const onScroll = (e) => {
    if (!el.contains(e.target)) closeMenu();
  };
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', closeMenu);
  openMenu = {
    el, anchor,
    cleanup() {
      document.removeEventListener('mousedown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', closeMenu);
    },
  };
  (buttons[items.findIndex((it) => it.checked === true)] || buttons[0]).focus();
}

// ---------- media ----------

// Audio, video and text previews load only when they come near the screen: with preload=metadata, each player asks the
// server for its file at once, and Jobs with 2,000 finished jobs made about 1,000 such requests when it opened.
let nearScreen = null;
const onNear = new WeakMap(); // element -> its load function
function whenNear(el, load) {
  if (typeof IntersectionObserver !== 'function') {
    load();
    return el;
  }
  nearScreen = nearScreen || new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting && e.target.isConnected) continue;
      nearScreen.unobserve(e.target);
      const run = onNear.get(e.target);
      onNear.delete(e.target);
      if (e.isIntersecting && run) run();
    }
  }, {rootMargin: '600px 0px'});
  onNear.set(el, load);
  nearScreen.observe(el);
  return el;
}

function lazySrc(el, url) {
  return whenNear(el, () => (el.src = url));
}

const TEXT_PREVIEW = 20000; // characters of a text output that media() shows

// The URL of a small JPEG of a done job's image or video output (app/library.py get_thumb).
export function thumbUrl(job, name) {
  return '/api/library/thumbs/' + encodeURIComponent(job) + '/' + encodeURIComponent(name);
}

// A job output ({url, kind, name, note}) as real media. opts.onOpen(output) runs when an image is clicked;
// opts.actions are nodes placed in the caption (Send to, Download); opts.thumb is the URL of a small JPEG of an image
// or video (GET /api/library/thumbs): the image shows it (the file itself when it fails), the video shows it as its
// poster and loads nothing before Play. Returns null for a URL off this origin.
export function media(output, opts = {}) {
  const url = safeFileUrl(output.url);
  if (!url) return null;
  const name = output.name || url.split('/').pop();
  let el;
  if (output.kind === 'image') {
    const img = h('img', {src: opts.thumb || url, alt: name, loading: 'lazy'});
    if (opts.thumb) img.addEventListener('error', () => (img.src = url), {once: true});
    el = h('button', {type: 'button', class: 'media-img', 'aria-label': 'Enlarge ' + name,
      onclick: () => (opts.onOpen ? opts.onOpen(output) : lightbox([{url, name}], 0))}, img);
  } else if (output.kind === 'audio') {
    el = h('div', {class: 'media-audio'}, lazySrc(h('audio', {controls: true, preload: 'metadata', 'aria-label': name}), url));
  } else if (output.kind === 'video') {
    const video = h('video', {controls: true, playsinline: true, preload: opts.thumb ? 'none' : 'metadata', 'aria-label': name});
    el = whenNear(video, () => {
      if (opts.thumb) video.poster = opts.thumb;
      video.src = url;
    });
  } else if (output.kind === 'text') {
    // a transcript: the file itself is the result, so show its text (the start of a very long one)
    // loads like the players: only near the screen
    const pre = h('pre', {class: 'media-pre', tabindex: '0', 'aria-label': name}, 'Loading the text...');
    el = whenNear(pre, () => fetch(url, {cache: 'no-store'})
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error('HTTP ' + r.status))))
      .then((text) => {
        pre.textContent = text.length > TEXT_PREVIEW ? text.slice(0, TEXT_PREVIEW) + '\n...\nDownload the file for the rest.' : text;
      })
      .catch(() => {
        pre.textContent = 'The text could not load. Download the file to read it.';
      }));
  } else {
    el = h('div', {class: 'art r11'}, icon('file', 'ic-lg'));
  }
  return h('figure', {class: 'media media-' + (output.kind || 'file')}, el,
    h('figcaption', {class: 'media-cap'}, h('span', {class: 'file-name', title: name}, name), opts.actions || null),
    output.note ? h('div', {class: 'media-note'}, output.note) : null);
}

export function downloadLink(output) {
  const url = safeFileUrl(output.url);
  if (!url) return null;
  const name = output.name || url.split('/').pop();
  return h('a', {href: url, download: name, class: 'btn btn-ghost btn-sm btn-icon', title: 'Download ' + name,
    'aria-label': 'Download ' + name}, icon('download', 'ic-sm'));
}

// Full-size images with previous/next; items: [{url, name, caption}].
export function lightbox(items, index) {
  if (!items.length) return;
  const back = document.activeElement;
  let i = index || 0;
  const img = h('img', {alt: ''});
  const cap = h('figcaption');
  const prev = h('button', {type: 'button', 'aria-label': 'Previous image', onclick: () => step(-1)}, icon('chev', 'ic-lg'));
  prev.firstChild.style.transform = 'rotate(180deg)';
  const next = h('button', {type: 'button', 'aria-label': 'Next image', onclick: () => step(1)}, icon('chev', 'ic-lg'));
  const close = h('button', {type: 'button', class: 'lb-close', 'aria-label': 'Close', onclick: () => done()}, icon('x', 'ic-lg'));
  const box = h('div', {class: 'lightbox', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Image'},
    prev, h('figure', null, img, cap), next, close);
  const show = () => {
    const it = items[i];
    img.src = it.url;
    img.alt = it.name;
    cap.textContent = (it.caption ? it.caption + ' · ' : '') + it.name + (items.length > 1 ? ' · ' + (i + 1) + '/' + items.length : '');
    prev.hidden = next.hidden = items.length < 2;
  };
  const step = (d) => {
    i = (i + d + items.length) % items.length;
    show();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') done();
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'Tab') {
      const f = [prev, next, close].filter((b) => !b.hidden);
      const at = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(at + (e.shiftKey ? f.length - 1 : 1)) % f.length].focus();
      return;
    } else return;
    e.preventDefault();
    e.stopPropagation();
  };
  function done() {
    document.removeEventListener('keydown', onKey, true);
    leave(box);
    if (back && back.isConnected) back.focus();
  }
  box.addEventListener('click', (e) => {
    if (e.target === box || e.target.tagName === 'FIGURE') done();
  });
  document.addEventListener('keydown', onKey, true);
  document.body.appendChild(box);
  show();
  close.focus();
}

// ---------- theme ----------

const THEMES = ['system', 'light', 'dark'];
const themeListeners = new Set();

export function getTheme() {
  const t = lsGet('ws.theme');
  return THEMES.includes(t) ? t : 'system';
}

// mode: 'system' | 'light' | 'dark'. Saved per viewer; System follows prefers-color-scheme.
export function setTheme(mode) {
  const t = THEMES.includes(mode) ? mode : 'system';
  if (t === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  lsSet('ws.theme', t);
  for (const fn of themeListeners) fn(t);
}

export function onTheme(fn) {
  themeListeners.add(fn);
  return () => themeListeners.delete(fn);
}

export const ui = {
  h,
  append,
  icon,
  toolIcon,
  groupTools,
  GROUP_ORDER,
  grow,
  lsGet,
  lsSet,
  lsRemove,
  fmtDuration,
  fmtClock,
  fmtAbout,
  fmtLeft,
  fmtGb,
  fmtBytes,
  fmtTime,
  timeAgo,
  fmtValue,
  fit,
  toast,
  confirm,
  inlineConfirm,
  emptyState,
  errorBox,
  menu,
  closeMenu,
  media,
  downloadLink,
  lightbox,
  getTheme,
  setTheme,
  onTheme,
};

export default ui;
