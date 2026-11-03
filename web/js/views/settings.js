// Settings: theme, prompt help provider, the local LLM, the model paths in config.toml, and where things live.
// Path and provider changes go to config.toml through PUT /api/settings; the app reads that file only when it starts,
// so a saved change shows a restart note instead of taking effect at once.

import {h, icon, toast, confirm, errorBox, getTheme, setTheme, onTheme} from '../ui.js';

const THEMES = [['system', 'System', 'monitor'], ['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']];
const README = [
  ['start-the-app', 'Start the app'],
  ['install-the-models', 'Install the models'],
  ['how-it-works', 'How it works'],
  ['model-licences', 'Model licences'],
  ['security', 'Security'],
];
// the same rules as app/settings_api.py: a TOML basic string cannot hold these as they are
const BAD_CHARS = /["\\\u0000-\u001f\u007f]/;
const MAX_PATH = 1024;

let s = null;

function mount(root, ctx) {
  root.classList.add('v-settings');
  s = {root, ctx, data: null, draft: {}, errors: {}, rows: {}, llm: {}, offs: [], abort: new AbortController(), saving: false};
  s.notice = h('div', {class: 'set-notice', role: 'status'});
  s.body = h('div', {class: 'set-body'}, h('p', {class: 'muted'}, 'Loading the settings...'));
  root.append(h('div', {class: 'settings'}, s.notice, appearance(), s.body));
  s.offs.push(onTheme(renderTheme), ctx.store.on('llm', renderLlm));
  load();
}

function unmount() {
  if (!s) return;
  for (const off of s.offs) off();
  s.abort.abort();
  s = null;
}

async function load() {
  try {
    const data = await s.ctx.api.request('/api/settings', {signal: s.abort.signal});
    if (!s) return;
    s.data = data;
    s.draft = Object.fromEntries(data.paths.map((r) => [r.key, r.value]));
    s.errors = {};
    build();
  } catch (e) {
    if (!s || e.name === 'AbortError') return;
    s.body.replaceChildren(errorBox({
      title: 'The settings could not be read',
      text: e.message,
      fixes: e.offline ? ['Start the app again.', 'Press Try again.'] : null,
      actions: [h('button', {type: 'button', class: 'btn btn-sm', onclick: load}, icon('refresh', 'ic-sm'), 'Try again')],
    }));
  }
}

// ---------- layout ----------

function section(id, title, intro, ...body) {
  return h('section', {class: 'set-sec', 'aria-labelledby': id},
    h('h2', {id}, title), intro ? h('p', null, intro) : null, ...body);
}

function build() {
  s.body.replaceChildren(promptHelp(), localLlm(), modelPaths(), about());
  renderNotice();
  renderLlm();
}

function renderNotice() {
  const d = s.data;
  const parts = [];
  if (d.config.error) {
    parts.push(h('div', {class: 'banner banner-err'}, icon('alert'), h('div', {class: 'banner-body'},
      h('strong', null, 'config.toml cannot be read'),
      h('span', null, d.config.error + '. The app uses the values it read at start. ' +
        'Fix the file by hand, then open this screen again.'))));
  }
  if (d.restart_required) {
    parts.push(h('div', {class: 'banner banner-warn'}, icon('info'), h('div', {class: 'banner-body'},
      h('strong', null, 'Restart the app to use the new settings'),
      h('span', null, 'config.toml changed after the app started. The app reads it only when it starts. ' +
        'Quit Workstation (or stop start.py) and open it again.'))));
  }
  s.notice.replaceChildren(...parts);
}

// ---------- Appearance ----------

function appearance() {
  s.themeSeg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Theme'},
    THEMES.map(([k, label, ic]) => h('button', {type: 'button', 'data-theme-set': k, onclick: () => setTheme(k)},
      icon(ic, 'ic-sm'), label)));
  renderTheme(getTheme());
  return section('set-ap', 'Appearance', null,
    h('div', {class: 'card set-card'}, h('div', {class: 'set-row'},
      h('div', null, h('span', {class: 'label'}, 'Theme'),
        h('p', {class: 'help'}, 'System follows the macOS setting. Saved in this browser.')),
      s.themeSeg)));
}

function renderTheme(mode) {
  if (!s || !s.themeSeg) return;
  for (const b of s.themeSeg.children) b.setAttribute('aria-pressed', String(b.dataset.themeSet === mode));
}

// ---------- Prompt help ----------

function promptHelp() {
  const a = s.data.assist;
  s.provErr = h('p', {class: 'field-err', role: 'alert', hidden: true});
  const cards = a.providers.map((p) => {
    const input = h('input', {type: 'radio', name: 'set-provider', id: 'set-pv-' + p.id, value: p.id,
      checked: p.id === a.provider, disabled: Boolean(s.data.config.error), onchange: () => saveProvider(p.id)});
    const state = p.available
      ? h('span', {class: 'pill pill-ok'}, 'Installed')
      : h('span', {class: 'pill pill-idle'}, 'Not installed');
    return h('label', {class: 'choice', for: 'set-pv-' + p.id}, input,
      h('span', {class: 'choice-title'}, h('b', null, p.label + (p.model ? ': ' + p.model : '')), state),
      h('span', {class: 'help'}, p.description,
        p.available ? null : ' Install the model on the Models screen to use it.'));
  });
  return section('set-ph', 'Prompt help',
    'The Improve button under a prompt rewrites a short idea into a full prompt for that tool. Chat uses the same model.',
    h('div', {class: 'choice-list', role: 'radiogroup', 'aria-labelledby': 'set-ph'}, cards), s.provErr);
}

async function saveProvider(id) {
  s.provErr.hidden = true;
  try {
    await put({assist: {provider: id}});
    toast('Prompt help saved. Restart the app to use it.');
  } catch (e) {
    if (!s || e.name === 'AbortError') return;
    const fields = e.detail && e.detail.fields;
    s.provErr.replaceChildren(icon('alert', 'ic-sm'), (fields && fields['assist.provider']) || e.message);
    s.provErr.hidden = false;
    for (const r of s.root.querySelectorAll('input[name="set-provider"]')) r.checked = r.value === s.data.assist.provider;
  }
}

// ---------- Local LLM ----------

function localLlm() {
  const L = s.llm;
  L.pill = h('span', {class: 'pill'});
  L.name = h('span', {class: 'label'});
  L.text = h('p', {class: 'help'});
  L.idle = h('p', {class: 'help'});
  L.err = h('p', {class: 'field-err', role: 'alert', hidden: true});
  L.unload = h('button', {type: 'button', class: 'btn btn-sm', onclick: unload}, icon('stop', 'ic-sm'), 'Unload');
  L.models = h('button', {type: 'button', class: 'btn btn-sm', hidden: true, onclick: () => s.ctx.router.go('models')},
    icon('models', 'ic-sm'), 'Open Models');
  return section('set-llm', 'Local LLM', null, h('div', {class: 'card set-card'},
    h('div', {class: 'set-row'},
      h('div', {class: 'set-llm-info'}, h('div', {class: 'set-llm-head'}, L.name, L.pill), L.text, L.idle, L.err),
      h('div', {class: 'set-actions'}, L.models, L.unload))));
}

function llmState(l) {
  const gb = Math.round(l.peak_gb);
  if (!l.installed) return ['Not installed', 'idle', 'Install it on the Models screen to use prompt help and chat.'];
  if (l.state === 'loading') return ['Loading', 'run', 'It is loading the weights from disk.'];
  if (l.state === 'ready' && l.busy) return ['Answering', 'run', 'It is writing an answer now.'];
  if (l.state === 'ready') return ['Loaded', 'ok', 'It uses about ' + gb + ' GB of memory while it is loaded.'];
  return ['Not loaded', 'idle', 'It loads when you press Improve or send a chat message. Now it uses no memory.'];
}

function renderLlm() {
  if (!s || !s.llm.pill) return;
  const L = s.llm;
  const l = s.ctx.store.get('llm');
  if (!l) {
    L.name.textContent = 'Local LLM';
    L.pill.className = 'pill pill-idle';
    L.pill.textContent = 'Checking';
    L.unload.disabled = true;
    return;
  }
  const [label, kind, text] = llmState(l);
  L.name.textContent = l.model;
  L.pill.className = 'pill pill-' + kind;
  L.pill.textContent = label;
  const waits = l.waiting ? ' ' + l.waiting + (l.waiting === 1 ? ' request waits' : ' requests wait') +
    ' for a media job to end.' : '';
  L.text.textContent = text + waits;
  const idle = l.idle_stop_s || s.data.llm.idle_stop_s;
  L.idle.textContent = 'It unloads after ' + minutes(idle) + ' without use, and before a job that needs its memory.';
  L.unload.disabled = l.state === 'off' || !l.installed;
  L.models.hidden = Boolean(l.installed);
}

function minutes(sec) {
  const m = Math.round(sec / 60);
  return m >= 1 ? m + (m === 1 ? ' minute' : ' minutes') : Math.round(sec) + ' seconds';
}

async function unload() {
  const L = s.llm;
  const l = s.ctx.store.get('llm') || {};
  if (l.busy || l.waiting) {
    const ok = await confirm({title: 'Unload the model now?', text: 'It is answering. Unloading stops the answer.',
      confirmLabel: 'Unload', danger: true});
    if (!ok || !s) return;
  }
  L.err.hidden = true;
  L.unload.disabled = true;
  try {
    await s.ctx.api.unloadLlm();
    toast('The local LLM was unloaded.');
  } catch (e) {
    if (!s) return;
    L.err.replaceChildren(icon('alert', 'ic-sm'), e.message);
    L.err.hidden = false;
  }
  if (s) s.ctx.store.refresh('llm');
}

// ---------- Model paths ----------

function modelPaths() {
  const d = s.data;
  s.rows = {};
  const rows = d.paths.map(pathRow);
  s.saveBtn = h('button', {type: 'button', class: 'btn btn-primary', onclick: savePaths}, 'Save');
  s.resetBtn = h('button', {type: 'button', class: 'btn', onclick: resetDraft}, 'Undo changes');
  s.saveMsg = h('p', {class: 'help', 'aria-live': 'polite'});
  s.formErr = h('p', {class: 'field-err', role: 'alert', hidden: true});
  const intro = h('p', null, 'Where each model is installed: the ', h('code', null, '[paths]'), ' in ',
    h('code', null, d.config.path), '. Save writes only the paths you changed. ',
    '"~" means your home folder. A relative path counts from the app folder.');
  const sec = section('set-mp', 'Model paths', null, intro,
    h('div', {class: 'card set-card'}, rows),
    h('div', {class: 'set-save'}, s.saveMsg, h('div', {class: 'set-actions'}, s.resetBtn, s.saveBtn)), s.formErr);
  renderSave();
  return sec;
}

function usedBy(u) {
  const names = [...u.tools, ...u.features];
  return names.length ? 'Used by ' + names.join(', ') + '.' : '';
}

function pathRow(r) {
  const id = 'set-p-' + r.key;
  const input = h('input', {class: 'input', id, value: s.draft[r.key], spellcheck: 'false', autocomplete: 'off',
    autocapitalize: 'off', disabled: Boolean(s.data.config.error), 'aria-describedby': id + '-err ' + id + '-st',
    oninput: () => {
      s.draft[r.key] = input.value;
      delete s.errors[r.key];
      renderRow(r.key);
      renderSave();
    },
    onkeydown: (e) => {
      if (e.key === 'Enter') savePaths();
    }});
  const def = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => {
    input.value = s.draft[r.key] = r.default;
    delete s.errors[r.key];
    renderRow(r.key);
    renderSave();
    input.focus();
  }}, 'Use default');
  const el = {
    input, def,
    mark: h('span', {class: 'set-mark'}),
    status: h('p', {class: 'help', id: id + '-st'}),
    err: h('p', {class: 'field-err', id: id + '-err', role: 'alert', hidden: true}),
  };
  s.rows[r.key] = el;
  renderRow(r.key);
  return h('div', {class: 'set-row stack'},
    h('label', {class: 'label set-path-label', for: id}, r.label, h('span', {class: 'mono faint'}, r.key)),
    h('div', {class: 'path-row'}, input, el.mark, def),
    el.err, el.status,
    h('p', {class: 'help'}, usedBy(r.used_by)));
}

function setMark(el, name, cls, text) {
  el.mark.replaceChildren(icon(name), h('span', {class: 'sr-only'}, text));
  el.mark.className = 'set-mark ' + cls;
  el.mark.title = text;
}

function renderRow(key) {
  const r = s.data.paths.find((x) => x.key === key);
  const el = s.rows[key];
  const draft = s.draft[key].trim();
  const edited = draft !== r.value;
  const err = s.errors[key];
  el.def.hidden = draft === r.default;
  el.input.setAttribute('aria-invalid', String(Boolean(err)));
  el.err.replaceChildren(...(err ? [icon('alert', 'ic-sm'), err] : []));
  el.err.hidden = !err;
  const parts = [];
  if (edited) {
    setMark(el, 'info', 'is-edit', 'Not saved');
    parts.push('Not saved yet. Save to check this path.');
  } else if (r.exists) {
    setMark(el, 'check', 'is-ok', 'Found');
    parts.push(h('span', {class: 'set-found'}, 'Found: '), h('span', {class: 'mono'}, r.path));
  } else {
    setMark(el, 'x', 'is-miss', 'Not found');
    parts.push(h('span', {class: 'set-missing'}, 'Not found: '), h('span', {class: 'mono'}, r.path),
      '. Install it on the Models screen, or enter the folder where it is.');
  }
  if (!edited && r.path !== r.active) {
    parts.push(h('br'), 'Saved. Until the restart the app uses ', h('span', {class: 'mono'}, r.active), '.');
  }
  el.status.replaceChildren(...parts);
}

function changes() {
  const out = {};
  for (const r of s.data.paths) {
    const v = s.draft[r.key].trim();
    if (v !== r.value) out[r.key] = v;
  }
  return out;
}

function renderSave() {
  const n = Object.keys(changes()).length;
  s.saveBtn.disabled = s.saving || !n || Boolean(s.data.config.error);
  s.resetBtn.hidden = !n;
  if (s.saving) s.saveMsg.textContent = 'Saving...';
  else if (n) s.saveMsg.textContent = n === 1 ? '1 path changed.' : n + ' paths changed.';
  else s.saveMsg.textContent = 'No changes.';
}

function resetDraft() {
  for (const r of s.data.paths) {
    s.draft[r.key] = r.value;
    s.rows[r.key].input.value = r.value;
  }
  s.errors = {};
  s.formErr.hidden = true;
  for (const key of Object.keys(s.rows)) renderRow(key);
  renderSave();
}

function checkPath(v) {
  if (!v) return 'Enter a path, or press Use default.';
  if (v.length > MAX_PATH) return 'The path is longer than ' + MAX_PATH + ' characters.';
  if (BAD_CHARS.test(v)) return 'The path cannot contain a double quote, a backslash or a line break.';
  return null;
}

function showErrors(errors) {
  s.errors = errors;
  for (const key of Object.keys(s.rows)) renderRow(key);
  const first = Object.keys(errors)[0];
  if (first && s.rows[first]) s.rows[first].input.focus();
}

async function savePaths() {
  const todo = changes();
  if (!Object.keys(todo).length || s.saving) return;
  s.formErr.hidden = true;
  const errors = {};
  for (const [k, v] of Object.entries(todo)) {
    const msg = checkPath(v);
    if (msg) errors[k] = msg;
  }
  if (Object.keys(errors).length) return showErrors(errors);
  s.saving = true;
  renderSave();
  try {
    await put({paths: todo});
    toast('Paths saved to config.toml. Restart the app to use them.');
  } catch (e) {
    if (!s || e.name === 'AbortError') return;
    const fields = (e.detail && e.detail.fields) || {};
    const byKey = {};
    for (const [k, msg] of Object.entries(fields)) if (k.startsWith('paths.')) byKey[k.slice(6)] = msg;
    if (Object.keys(byKey).length) showErrors(byKey);
    else {
      s.formErr.replaceChildren(icon('alert', 'ic-sm'), e.message);
      s.formErr.hidden = false;
    }
  } finally {
    if (s) {
      s.saving = false;
      renderSave();
    }
  }
}

// PUT /api/settings, then show the new state. Unsaved edits of the other paths stay in their fields.
async function put(body) {
  const data = await s.ctx.api.request('/api/settings', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: s.abort.signal,
  });
  if (!s) return data;
  const sent = body.paths || {};
  const keep = Object.fromEntries(s.data.paths.filter((r) => !(r.key in sent)).map((r) => [r.key, s.draft[r.key]]));
  s.data = data;
  s.draft = Object.fromEntries(data.paths.map((r) => [r.key, r.key in keep ? keep[r.key] : r.value]));
  s.errors = {};
  const focus = document.activeElement && document.activeElement.id;
  build();
  const again = focus && document.getElementById(focus);
  if (again && s.root.contains(again)) again.focus();
  return data;
}

// ---------- About ----------

function about() {
  const a = s.data.about;
  const c = s.data.config;
  const row = (k, ...v) => [h('dt', null, k), h('dd', null, ...v)];
  const mono = (t) => h('span', {class: 'mono'}, t);
  const faint = (t) => h('span', {class: 'faint'}, t);
  const version = [a.version, a.commit && '(' + a.commit + ')'].filter(Boolean).join(' ');
  return section('set-ab', 'About', null, h('div', {class: 'card set-card'},
    h('div', {class: 'set-row stack'}, h('dl', {class: 'kv'},
      version ? row('Version', version) : null,
      row('Settings file', mono(c.path), c.exists ? null : faint(' (not made yet: every path uses its default)')),
      row('Outputs', mono(a.outputs), h('br'), faint('Each job writes to outputs/<job id>/. Chats are in outputs/chats/.')),
      row('Logs', mono('outputs/llm.log'), faint(' (local LLM)'), h('br'), mono('outputs/app.log'),
        faint(' (when started from Workstation.app)')),
      row('Server', mono(location.host), faint(' on this Mac only. The app sends no data anywhere.')),
    )),
    h('div', {class: 'set-row stack'},
      h('span', {class: 'label'}, 'README'),
      h('ul', {class: 'set-links'}, README.map(([anchor, label]) =>
        h('li', null, h('a', {href: a.repo_url + '#' + anchor, target: '_blank', rel: 'noopener noreferrer'}, label)))),
      h('p', {class: 'help'}, 'The same file is on this Mac: ', mono(a.readme), '.'))));
}

export default {
  id: 'settings',
  title: 'Settings',
  subtitle: 'Paths, prompt help and appearance',
  mount,
  unmount,
};
