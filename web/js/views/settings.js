// Settings: theme, profile, prompt help provider, the local LLM, the Jobs switches, the agent's sandbox and web search,
// the model paths in config.toml, and where things live. Every change goes to config.toml through PUT /api/settings.
// The app reads paths, the provider and [agent] only when it starts, so such a change shows a restart note. The
// profile, the Jobs switches and the thinking level of a new chat ([app]) apply at once and save on change. The chat profile leaves out prompt help, the
// job switches and the paths Chat does not use.

import {h, icon, toast, confirm, errorBox, getTheme, setTheme, onTheme} from '../ui.js';

const THEMES = [['system', 'System', 'monitor'], ['light', 'Light', 'sun'], ['dark', 'Dark', 'moon']];
const README = [
  ['start-the-app', 'Start the app'],
  ['install-the-models', 'Install the models'],
  ['how-it-works', 'How it works'],
  ['model-licences', 'Model licences'],
  ['security', 'Security'],
  ['chat-only', 'Chat only'],
];
const PROFILE_TEXT = {full: 'Full: every section', chat: 'Chat only: Chat, Models and Settings'};
// the thinking levels of app/effort.py, with the upper limit of the thinking at about 17 tokens a second
const EFFORT_TEXT = {off: 'Off: no thinking', low: 'Low: about 1 minute', medium: 'Medium: about 3 minutes',
  high: 'High: about 6 minutes', xhigh: 'XHigh: about 8 minutes', max: 'Max: no limit'};
// the same rules as app/settings_api.py: a TOML basic string cannot hold these as they are
const BAD_CHARS = /["\\\u0000-\u001f\u007f]/;
const MAX_PATH = 1024;
// Settings > Agent: a separate environment for run_python. Versions checked on PyPI (2026-11-03), wheels for Python 3.12
// on Apple Silicon.
const SANDBOX_DIR = '~/mlx-media/venv-sandbox';
const SANDBOX_CMD = 'uv venv --python 3.12 ' + SANDBOX_DIR + '\n' +
  'uv pip install --python ' + SANDBOX_DIR + '/bin/python numpy==2.5.4 pandas==3.1.0 matplotlib==3.11.2';
const AGENT_KEYS = ['python', 'searxng_url', 'wikipedia_language', 'marginalia'];
const LANGUAGE_NAMES = {en: 'English', tr: 'Turkish', de: 'German', fr: 'French', es: 'Spanish', it: 'Italian',
  ja: 'Japanese', zh: 'Chinese'};

let s = null;

function mount(root, ctx) {
  root.classList.add('v-settings');
  s = {root, ctx, data: null, draft: {}, errors: {}, rows: {}, llm: {}, offs: [], abort: new AbortController(), saving: false,
    appErrors: {}, appSaving: null, agentDraft: {}, agentErrors: {}, agentSaving: false};
  s.notice = h('div', {class: 'set-notice', role: 'status'});
  s.body = h('div', {class: 'set-body'}, h('p', {class: 'muted'}, 'Loading the settings...'));
  root.append(h('div', {class: 'settings'}, s.notice, appearance(), s.body));
  s.offs.push(onTheme(renderTheme), ctx.store.on('llm', renderLlm), ctx.store.on('profile', () => {
    if (s.data) build();
  }));
  load();
}

function chatOnly() {
  return s.ctx.store.profile() === 'chat';
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
    s.agentDraft = agentValues(data.agent);
    s.agentErrors = {};
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
  s.appCtl = {};
  s.body.replaceChildren(...[profile(), chatOnly() ? null : promptHelp(), localLlm(), jobs(), agent(), modelPaths(),
    about()].filter(Boolean));
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
    const data = await put({assist: {provider: id}});
    toast(data.assist.provider !== data.assist.active ? 'Prompt help saved. Restart the app to use it.' :
      'Prompt help saved. The app uses it now.');
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
      h('div', {class: 'set-actions'}, L.models, L.unload)),
    chatEffort()));
}

// The thinking level of a new chat ([app] chat_effort); a chat keeps the last level used in it.
function chatEffort() {
  const a = s.data.app;
  const select = h('select', {class: 'select set-select', disabled: Boolean(s.data.config.error) || s.appSaving === 'chat_effort',
    onchange: (e) => saveApp('chat_effort', e.target.value)},
  a.efforts.map((v) => h('option', {value: v, selected: v === a.chat_effort}, EFFORT_TEXT[v] || v)));
  return rowFor('chat_effort', 'Thinking in a new chat',
    'How long the model may think before it answers, at the start of a new chat. Change it per chat under the ' +
    'message box; each chat keeps its last level. Higher levels answer better and slower.', select);
}

function llmState(l) {
  const gb = Math.round(l.peak_gb);
  const uses = chatOnly() ? 'chat' : 'prompt help and chat';
  if (!l.installed) return ['Not installed', 'idle', 'Install it on the Models screen to use ' + uses + '.'];
  if (l.state === 'loading') return ['Loading', 'run', 'It is loading the weights from disk.'];
  if (l.state === 'ready' && l.busy) return ['Answering', 'run', 'It is writing an answer now.'];
  if (l.state === 'ready') return ['Loaded', 'ok', 'It uses about ' + gb + ' GB of memory while it is loaded.'];
  return ['Not loaded', 'idle', 'It loads when you ' + (chatOnly() ? '' : 'press Improve or ') +
    'send a chat message. Now it uses no memory.'];
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
  // the saved setting: the llm poll may still show the value from before a change
  const idle = s.data.app.llm_idle_minutes;
  const chat = chatOnly();
  L.idle.textContent = idle > 0
    ? 'It unloads after ' + minutes(idle * 60) + ' without use (' + (chat ? 'Memory' : 'Jobs') + ', below)' +
      (chat ? '.' : ', and before a job that needs its memory.')
    : 'It stays loaded until you press Unload' + (chat ? '.' : ', or until a job needs its memory.');
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

// ---------- Jobs ([app] in config.toml; applies without a restart) ----------

const SWITCHES = [
  ['notify', 'Notify when a job ends', 'A macOS notification with the tool name when a job is done or failed.'],
  ['keep_awake', 'Keep the Mac awake while jobs run',
    'The Mac does not go to sleep while a job is queued or running. The display may still sleep.'],
];

function idleText(m) {
  if (m === 0) return 'Never';
  if (m % 60 === 0) return m / 60 === 1 ? 'After 1 hour' : 'After ' + m / 60 + ' hours';
  return 'After ' + m + (m === 1 ? ' minute' : ' minutes');
}

// One [app] value: label, help, error line and its control (a switch or a select).
function rowFor(key, label, help, control) {
  const id = 'set-app-' + key;
  const err = h('p', {class: 'field-err', id: id + '-err', role: 'alert', hidden: true});
  s.appCtl[key] = {control, err};
  control.id = id;
  control.setAttribute('aria-labelledby', id + '-label');
  control.setAttribute('aria-describedby', id + '-help ' + id + '-err');
  renderAppErr(key);
  return h('div', {class: 'set-row'},
    h('div', null, h('span', {class: 'label', id: id + '-label'}, label), h('p', {class: 'help', id: id + '-help'}, help), err),
    control.type === 'checkbox' ? h('label', {class: 'switch', for: id}, control, h('span', {class: 'track'})) : control);
}

// The profile: what the sidebar shows. A window opened with ?profile= keeps its own, so it cannot change it here.
// The select shows this run's profile, which --chat can set. The help names the saved one, so a change is not hidden.
function profile() {
  const a = s.data.app;
  const pinned = s.ctx.store.profilePinned();
  const shown = s.ctx.store.profile();
  const select = h('select', {class: 'select set-select', disabled: pinned || Boolean(s.data.config.error) || s.appSaving === 'profile',
    onchange: (e) => saveApp('profile', e.target.value)},
  a.profiles.map((p) => h('option', {value: p, selected: p === (pinned ? shown : a.profile)}, PROFILE_TEXT[p] || p)));
  const help = pinned
    ? 'This window opened next to the running app and always shows ' + (shown === 'chat' ? 'Chat only' : 'every section') +
      '. Change the profile in the app\'s own window.'
    : ['Chat only hides Create, Jobs and Library, for a Mac with only the local LLM. A change applies at once and is ' +
      'saved for the next start. start.py --chat and Workstation Chat.app always start in Chat only. ' +
      'Saved for Workstation.app and start.py: ', h('strong', null, a.saved_profile === 'chat' ? 'Chat only' : 'Full'), '.'];
  return section('set-pf', 'Profile', null, h('div', {class: 'card set-card'}, rowFor('profile', 'Sections', help, select)));
}

function jobs() {
  const a = s.data.app;
  const locked = Boolean(s.data.config.error);
  const chat = chatOnly();
  const rows = chat ? [] : SWITCHES.map(([key, label, help]) => rowFor(key, label, help,
    h('input', {type: 'checkbox', role: 'switch', checked: a[key], disabled: locked || s.appSaving === key,
      onchange: (e) => saveApp(key, e.target.checked)})));
  const choices = a.idle_choices.includes(a.llm_idle_minutes) ? a.idle_choices : [...a.idle_choices, a.llm_idle_minutes];
  const select = h('select', {class: 'select set-select', disabled: locked || s.appSaving === 'llm_idle_minutes',
    onchange: (e) => saveApp('llm_idle_minutes', Number(e.target.value))},
  choices.map((m) => h('option', {value: String(m), selected: m === a.llm_idle_minutes}, idleText(m))));
  rows.push(rowFor('llm_idle_minutes', 'Unload the chat model when idle',
    'Frees its memory after this long without a ' + (chat ? '' : 'prompt help or ') + 'chat request. ' +
      'It loads again on the next one.', select));
  return section('set-jobs', chat ? 'Memory' : 'Jobs', chat ? 'This applies at once, without a restart.' : 'These apply at once, without a restart.',
    h('div', {class: 'card set-card'}, rows));
}

function renderAppErr(key) {
  const el = s.appCtl[key];
  const msg = s.appErrors[key];
  el.err.replaceChildren(...(msg ? [icon('alert', 'ic-sm'), msg] : []));
  el.err.hidden = !msg;
  el.control.setAttribute('aria-invalid', String(Boolean(msg)));
}

function showApp(c, value) {
  if (c.type === 'checkbox') c.checked = value;
  else c.value = String(value);
}

async function saveApp(key, value) {
  const control = s.appCtl[key].control;
  if (s.appSaving) {  // one save at a time: undo this change on screen
    showApp(control, s.data.app[key]);
    return;
  }
  const hadFocus = document.activeElement === control;
  s.appSaving = key;
  control.disabled = true;
  delete s.appErrors[key];
  renderAppErr(key);
  try {
    await put({app: {[key]: value}});  // rebuilds the screen with the saved value
    if (key === 'profile') await s.ctx.store.refresh('system');  // the sidebar changes now, not at the next poll
    if (key === 'chat_effort') s.ctx.store.refresh('llm');  // the chat's default for a new chat
    toast('Saved. The app uses it now.');
  } catch (e) {
    if (!s || e.name === 'AbortError') return;
    const fields = e.detail && e.detail.fields;
    s.appErrors[key] = (fields && fields['app.' + key]) || e.message;
    showApp(s.appCtl[key].control, s.data.app[key]);
    renderAppErr(key);
  } finally {
    if (s) {
      s.appSaving = null;
      const c = s.appCtl[key].control;  // a new element when the save rebuilt the screen
      c.disabled = Boolean(s.data.config.error);
      if (hadFocus || s.appErrors[key]) c.focus();
    }
  }
}

// ---------- Agent ([agent] in config.toml; read at start) ----------

function agentValues(a) {
  return Object.fromEntries(AGENT_KEYS.map((k) => [k, a[k]]));
}

function agentChanges() {
  const out = {};
  for (const k of AGENT_KEYS) {
    const v = typeof s.agentDraft[k] === 'string' ? s.agentDraft[k].trim() : s.agentDraft[k];
    if (v !== s.data.agent[k]) out[k] = v;
  }
  return out;
}

function agentField(key, label, control, ...below) {
  const id = 'set-ag-' + key;
  const err = h('p', {class: 'field-err', id: id + '-err', role: 'alert', hidden: true});
  s.agentEl[key] = {control, err};
  control.id = id;
  control.setAttribute('aria-describedby', [id + '-err', ...below.map((el) => el.id).filter(Boolean)].join(' '));
  return {id, err, label: h('label', {class: 'label set-path-label', for: id}, label,
    h('span', {class: 'mono faint'}, key))};
}

function agentInput(key, attrs) {
  return h('input', {class: 'input', value: s.agentDraft[key], spellcheck: 'false', autocomplete: 'off',
    autocapitalize: 'off', disabled: Boolean(s.data.config.error), ...attrs,
    oninput: (e) => setAgent(key, e.target.value),
    onkeydown: (e) => {
      if (e.key === 'Enter') saveAgent();
    }});
}

function agent() {
  const a = s.data.agent;
  const locked = Boolean(s.data.config.error);
  s.agentEl = {};

  const pyInput = agentInput('python', {placeholder: 'Empty: the app\'s Python, standard library only'});
  const pyStatus = h('p', {class: 'help', id: 'set-ag-python-st'});
  const py = agentField('python', 'Sandbox Python', pyInput, pyStatus);
  const pyDefault = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => {
    pyInput.value = '';
    setAgent('python', '');
    pyInput.focus();
  }}, 'Use default');
  Object.assign(s.agentEl.python, {status: pyStatus, def: pyDefault, mark: h('span', {class: 'set-mark'})});

  const urlInput = agentInput('searxng_url', {type: 'url', inputmode: 'url', placeholder: 'http://127.0.0.1:8888'});
  const urlHelp = h('p', {class: 'help', id: 'set-ag-searxng_url-help'}, 'Optional. A SearXNG instance you run ' +
    'yourself, with the JSON format on. Web search asks it first. Leave it empty to not use one.');
  const url = agentField('searxng_url', 'SearXNG URL', urlInput, urlHelp);

  const langs = a.languages.includes(s.agentDraft.wikipedia_language)
    ? a.languages : [...a.languages, s.agentDraft.wikipedia_language];
  const langSelect = h('select', {class: 'select set-select', disabled: locked,
    onchange: (e) => setAgent('wikipedia_language', e.target.value)},
  langs.map((c) => h('option', {value: c, selected: c === s.agentDraft.wikipedia_language},
    LANGUAGE_NAMES[c] ? LANGUAGE_NAMES[c] + ' (' + c + ')' : c)));
  const langHelp = h('p', {class: 'help', id: 'set-ag-wikipedia_language-help'}, 'The Wikipedia that web search asks.');
  const lang = agentField('wikipedia_language', 'Wikipedia language', langSelect, langHelp);

  const mgSwitch = h('input', {type: 'checkbox', role: 'switch', checked: s.agentDraft.marginalia, disabled: locked,
    onchange: (e) => setAgent('marginalia', e.target.checked)});
  const mgHelp = h('p', {class: 'help', id: 'set-ag-marginalia-help'}, 'Web search also asks Marginalia Search, a ' +
    'search engine for small independent sites. The app uses its shared key "public", which is often rate limited: ' +
    'then web search uses the other sources only.');
  const mg = agentField('marginalia', 'Marginalia Search', mgSwitch, mgHelp);

  s.agentSaveBtn = h('button', {type: 'button', class: 'btn btn-primary', onclick: saveAgent}, 'Save');
  s.agentResetBtn = h('button', {type: 'button', class: 'btn', onclick: resetAgent}, 'Undo changes');
  s.agentMsg = h('p', {class: 'help', 'aria-live': 'polite'});
  s.agentFormErr = h('p', {class: 'field-err', role: 'alert', hidden: true});

  const intro = h('p', null, 'The tools of the Chat\'s agent mode: the sandbox where run_python runs code, and the ' +
    'sources of web search. The ', h('code', null, '[agent]'), ' in ', h('code', null, s.data.config.path),
  '. The app reads it when it starts, so a change applies after a restart.');
  const sec = section('set-ag', 'Agent', null, intro, h('div', {class: 'card set-card'},
    h('div', {class: 'set-row stack'}, py.label,
      h('div', {class: 'path-row'}, pyInput, s.agentEl.python.mark, pyDefault), py.err, pyStatus,
      h('p', {class: 'help'}, 'The default is the app\'s own Python with the standard library only. For numpy, ' +
        'pandas or matplotlib, make a separate environment for the sandbox and enter the absolute path of its ' +
        'python. With uv, in Terminal:'),
      commandBlock(SANDBOX_CMD, 'Copy the commands'),
      h('p', {class: 'help'}, 'Then enter ', h('span', {class: 'mono'}, SANDBOX_DIR + '/bin/python'), ' and save.')),
    h('div', {class: 'set-row stack'}, url.label, urlInput, url.err, urlHelp),
    h('div', {class: 'set-row'}, h('div', null, lang.label, langHelp, lang.err), langSelect),
    h('div', {class: 'set-row'}, h('div', null, mg.label, mgHelp, mg.err),
      h('label', {class: 'switch', for: mg.id}, mgSwitch, h('span', {class: 'track'})))),
  h('div', {class: 'set-save'}, s.agentMsg, h('div', {class: 'set-actions'}, s.agentResetBtn, s.agentSaveBtn)),
  s.agentFormErr);
  renderAgent();
  return sec;
}

function setAgent(key, value) {
  s.agentDraft[key] = value;
  delete s.agentErrors[key];
  s.agentFormErr.hidden = true;
  renderAgent();
}

function renderAgent() {
  const a = s.data.agent;
  for (const [key, el] of Object.entries(s.agentEl)) {
    const err = s.agentErrors[key];
    el.err.replaceChildren(...(err ? [icon('alert', 'ic-sm'), err] : []));
    el.err.hidden = !err;
    el.control.setAttribute('aria-invalid', String(Boolean(err)));
  }
  const el = s.agentEl.python;
  const draft = s.agentDraft.python.trim();
  el.def.hidden = draft === '';
  const parts = [];
  if (draft !== a.python) {
    setMark(el, 'info', 'is-edit', 'Not saved');
    parts.push(draft ? 'Not saved yet. Save to check this path.' : 'Not saved yet. Save to use the default.');
  } else if (!a.python) {
    el.mark.replaceChildren();
    el.mark.className = 'set-mark';
    el.mark.title = '';
    parts.push('Default: ', h('span', {class: 'mono'}, a.default_python + ' -I -S'), ', the standard library only.');
  } else if (a.python_found && a.python_problem) {
    setMark(el, 'x', 'is-miss', 'Refused');
    parts.push(h('span', {class: 'set-missing'}, 'Refused: '), a.python_problem,
      ' The agent has no Python tool until you fix it or press Use default.');
  } else if (a.python_found) {
    setMark(el, 'check', 'is-ok', 'Found');
    parts.push(h('span', {class: 'set-found'}, 'Found: '), h('span', {class: 'mono'}, a.python_path));
  } else {
    setMark(el, 'x', 'is-miss', 'Not found');
    parts.push(h('span', {class: 'set-missing'}, 'Not found: '), h('span', {class: 'mono'}, a.python_path),
      '. Make the environment again, or press Use default.');
  }
  el.status.replaceChildren(...parts);

  const n = Object.keys(agentChanges()).length;
  s.agentSaveBtn.disabled = s.agentSaving || !n || Boolean(s.data.config.error);
  s.agentResetBtn.hidden = !n;
  if (s.agentSaving) s.agentMsg.textContent = 'Saving...';
  else if (n) s.agentMsg.textContent = n === 1 ? '1 value changed.' : n + ' values changed.';
  else if (a.restart_required) s.agentMsg.textContent = 'Saved. The agent uses the new values after a restart.';
  else s.agentMsg.textContent = 'No changes.';
}

function resetAgent() {
  s.agentDraft = agentValues(s.data.agent);
  s.agentErrors = {};
  s.agentFormErr.hidden = true;
  const focus = document.activeElement && document.activeElement.id;
  build();
  const again = focus && document.getElementById(focus);
  if (again && s.root.contains(again)) again.focus();
}

function checkAgent(key, v) {
  if (typeof v !== 'string' || !v) return null;
  if (v.length > MAX_PATH) return 'The value is longer than ' + MAX_PATH + ' characters.';
  if (BAD_CHARS.test(v)) return 'The value cannot contain a double quote, a backslash or a line break.';
  if (key === 'python' && !(v.startsWith('/') || v === '~' || v.startsWith('~/'))) {
    return 'Use an absolute path, for example ~/env/bin/python.';
  }
  if (key === 'searxng_url' && !/^https?:\/\/[^\s/?#]/i.test(v)) return 'Use an http:// or https:// URL.';
  return null;
}

function showAgentErrors(errors) {
  s.agentErrors = errors;
  renderAgent();
  const first = AGENT_KEYS.find((k) => errors[k]);
  if (first) s.agentEl[first].control.focus();
}

async function saveAgent() {
  const todo = agentChanges();
  if (!Object.keys(todo).length || s.agentSaving) return;
  s.agentFormErr.hidden = true;
  const errors = {};
  for (const [k, v] of Object.entries(todo)) {
    const msg = checkAgent(k, v);
    if (msg) errors[k] = msg;
  }
  if (Object.keys(errors).length) return showAgentErrors(errors);
  s.agentSaving = true;
  renderAgent();
  try {
    const data = await put({agent: todo});
    toast(data.agent.restart_required ? 'Agent settings saved to config.toml. Restart the app to use them.' :
      'Agent settings saved to config.toml. The app uses them now.');
  } catch (e) {
    if (!s || e.name === 'AbortError') return;
    const fields = (e.detail && e.detail.fields) || {};
    const byKey = {};
    for (const [k, msg] of Object.entries(fields)) if (k.startsWith('agent.')) byKey[k.slice(6)] = msg;
    if (Object.keys(byKey).length) showAgentErrors(byKey);
    else {
      s.agentFormErr.replaceChildren(icon('alert', 'ic-sm'), e.message);
      s.agentFormErr.hidden = false;
    }
  } finally {
    if (s) {
      s.agentSaving = false;
      renderAgent();
    }
  }
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

// ---------- Model paths ----------

function modelPaths() {
  const d = s.data;
  s.rows = {};
  // the chat profile shows only the paths Chat uses; the other drafts stay as they are
  const rows = d.paths.filter((r) => !chatOnly() || r.used_by.features.includes('Chat')).map(pathRow);
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
    if (s.rows[r.key]) s.rows[r.key].input.value = r.value;
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
    const data = await put({paths: todo});
    toast(data.paths.some((r) => r.path !== r.active) ? 'Paths saved to config.toml. Restart the app to use them.' :
      'Paths saved to config.toml. The app uses them now.');
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
  if (body.agent) {  // every changed agent value went in this request
    s.agentDraft = agentValues(data.agent);
    s.agentErrors = {};
  }
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
      row('Outputs', mono(a.outputs), h('br'),
        faint((chatOnly() ? '' : 'Each job writes to outputs/<job id>/. ') + 'Chats are in outputs/chats/.')),
      row('Logs', mono('outputs/llm.log'), faint(' (local LLM)'), h('br'), mono('outputs/app.log'),
        faint(' (when started from Workstation.app or Workstation Chat.app)')),
      row('Server', mono(location.host), faint(' on this Mac only. The app sends no data off this Mac, except the agent\'s web tools after you approve a call.')),
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
  subtitle: (ctx) => (ctx.store.profile() === 'chat' ? 'Profile, the local LLM, agent, paths and appearance' :
    'Profile, jobs, agent, paths, prompt help and appearance'),
  mount,
  unmount,
};
