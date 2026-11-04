// Settings > MCP servers (app/mcp_api.py): add, edit and remove MCP servers, approve a server's command line and its
// tools, turn a server on for chats, pick how the agent sees its tools, test the connection and read the last error.
// Secrets (environment values, bearer tokens) go to the Keychain through the API; the page never gets them back.

import {h, icon, toast, confirm} from '../../ui.js';

const EXPOSURE_TEXT = {
  deferred: 'Deferred: the agent finds them with tool search',
  direct: 'Direct: always in the agent\'s tool list',
  hidden: 'Hidden: not offered to the agent',
};
const STATE = {stopped: ['Not running', 'idle'], starting: ['Starting', 'run'], ready: ['Running', 'ok'],
  failed: ['Failed', 'err']};
const TOOL_STATE = {approved: ['Approved', 'ok'], new: ['Not approved', 'idle'], changed: ['Changed', 'warn'],
  gone: ['Not offered now', 'idle']};
const HINT_TEXT = {readOnlyHint: ['read-only', 'not read-only'], destructiveHint: ['destructive', 'not destructive'],
  idempotentHint: ['idempotent', 'not idempotent'], openWorldHint: ['reaches outside', 'stays local']};

// kept across rebuilds of the Settings screen: the last list, the open form and its draft
const st = {data: null, form: null, errors: {}, busy: {}, formErr: null, root: null, ctx: null, loading: null};

export function mcpServers(ctx) {
  st.ctx = ctx;
  st.root = h('section', {class: 'set-sec set-mcp', 'aria-labelledby': 'set-mcp'});
  render();
  load();
  return st.root;
}

function errText(e) {
  return (e.detail && e.detail.message) || e.message;
}

async function call(path, method, body) {
  return st.ctx.api.request(path, {
    method, headers: body ? {'Content-Type': 'application/json'} : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function load() {
  if (st.loading) return st.loading;
  st.loading = (async () => {
    try {
      st.data = await st.ctx.api.request('/api/mcp/servers');
      st.loadErr = null;
    } catch (e) {
      st.loadErr = errText(e);
    } finally {
      st.loading = null;
    }
    render();
  })();
  return st.loading;
}

function replace(view) {
  const list = st.data.servers;
  const i = list.findIndex((s) => s.name === view.name);
  if (i >= 0) list[i] = view;
  else list.push(view);
}

// one action of one server at a time; the buttons of that server show it
async function act(name, label, fn) {
  if (st.busy[name]) return;
  st.busy[name] = label;
  render();
  try {
    const view = await fn();
    if (view && view.name) replace(view);
    return view;
  } catch (e) {
    toast(errText(e), {kind: 'err'});
    await load();
    return null;
  } finally {
    delete st.busy[name];
    render();
  }
}

// ---------- layout ----------

function render() {
  if (!st.root) return;
  const intro = h('p', null, 'MCP servers give the agent more tools. A local server is a program that runs on this ' +
    'Mac with your user rights: read its command and approve it before it runs. A remote server sends the agent\'s ' +
    'calls and their arguments off this Mac. The agent asks you before every call of an MCP tool, and treats the ' +
    'results as untrusted.');
  const parts = [h('h2', {id: 'set-mcp'}, 'MCP servers'), intro];
  if (st.loadErr) {
    parts.push(h('p', {class: 'field-err', role: 'alert'}, icon('alert', 'ic-sm'), 'The list could not be read: ' +
      st.loadErr), h('div', null, h('button', {type: 'button', class: 'btn btn-sm', onclick: load},
      icon('refresh', 'ic-sm'), 'Try again')));
  } else if (!st.data) {
    parts.push(h('p', {class: 'muted'}, 'Loading the MCP servers...'));
  } else {
    if (!st.data.servers.length && !st.form) parts.push(h('p', {class: 'help'}, 'No MCP servers yet.'));
    for (const s of st.data.servers) {
      parts.push(st.form && st.form.mode === 'edit' && st.form.name === s.name ? form(s) : card(s));
    }
    if (st.form && st.form.mode === 'add') parts.push(form(null));
    else if (!st.form && st.data.servers.length < st.data.max_servers) {
      parts.push(h('div', null, h('button', {type: 'button', class: 'btn btn-sm', onclick: () => openForm('add')},
        icon('plus', 'ic-sm'), 'Add a server')));
    }
  }
  const focus = document.activeElement && st.root.contains(document.activeElement) && document.activeElement.id;
  st.root.replaceChildren(...parts);
  const again = focus && document.getElementById(focus);
  if (again) again.focus();
}

function pill(text, kind) {
  return h('span', {class: 'pill pill-' + kind}, text);
}

function card(s) {
  const busy = st.busy[s.name];
  const id = 'set-mcp-' + s.name;
  const remote = s.kind === 'remote';
  const [stateText, stateKind] = STATE[s.state] || [s.state, 'idle'];
  const enable = h('input', {type: 'checkbox', role: 'switch', id: id + '-on', checked: s.enabled,
    'aria-label': 'Use ' + s.name + ' in agent chats',
    disabled: Boolean(busy), onchange: (e) => setEnabled(s, e.target.checked)});
  const head = h('div', {class: 'set-row'},
    h('div', null,
      h('div', {class: 'set-mcp-head'}, h('span', {class: 'label mono'}, s.name),
        remote ? pill('Remote: sends data off this Mac', 'warn') : pill('Local program', 'idle'),
        pill(busy || stateText, busy ? 'run' : stateKind)),
      h('p', {class: 'help'}, s.enabled ? 'Its approved tools are in new agent chats.' :
        'Off: no chat uses it. Turn it on after you approve it and its tools.')),
    h('label', {class: 'switch', for: id + '-on'}, enable, h('span', {class: 'track'})));

  const spec = remote
    ? [h('span', {class: 'label'}, 'URL'), commandText(s.url),
      h('p', {class: 'help'}, 'Bearer token: ' + (s.token_set ? 'stored in the Keychain.' : 'none.'))]
    : [h('span', {class: 'label'}, 'Command'), commandText(s.command_line),
      h('p', {class: 'help'}, 'Working folder: ', s.cwd ? h('span', {class: 'mono'}, s.cwd) : 'the app\'s folder.'),
      s.env.length ? h('p', {class: 'help'}, 'Environment: ', s.env.map((e, i) => [i ? ', ' : '',
        h('span', {class: 'mono'}, e.name), e.set ? ' (stored in the Keychain)' : ' (no value)'])) : null];
  const warn = s.warnings.length ? h('div', {class: 'banner banner-warn'}, icon('alert'),
    h('div', {class: 'banner-body'}, s.warnings.map((w) => h('span', null, w)))) : null;
  const approve = s.approved ? null : h('div', {class: 'banner banner-info'}, icon('lock'),
    h('div', {class: 'banner-body'},
      h('strong', null, remote ? 'Approve this URL before the app connects to it' :
        'Approve this command before the app runs it'),
      h('span', null, remote ? 'The app sends the agent\'s calls to this address.' :
        'It runs a program on this Mac with your user rights. Read the whole command line first.'),
      h('div', {class: 'banner-actions'}, h('button', {type: 'button', class: 'btn btn-sm btn-primary',
        disabled: Boolean(busy), onclick: () => approveCommand(s)}, icon('check', 'ic-sm'),
      remote ? 'Approve this URL' : 'Approve this command'))));

  const select = h('select', {class: 'select set-select', id: id + '-exp', disabled: Boolean(busy),
    onchange: (e) => setExposure(s, e.target.value)},
  Object.entries(EXPOSURE_TEXT).map(([k, label]) => h('option', {value: k, selected: k === s.exposure}, label)));
  const exposure = h('div', {class: 'set-row'},
    h('div', null, h('label', {class: 'label', for: id + '-exp'}, 'How the agent sees its tools'),
      h('p', {class: 'help'}, 'Deferred keeps the prompt small. Direct puts the tools in every agent chat\'s tool ' +
        'list, at most ' + st.data.max_direct + ' MCP tools of all servers.')),
    select);

  const error = s.error ? h('div', {class: 'set-row stack'}, h('span', {class: 'label'}, 'Last error'),
    h('pre', {class: 'cmd-text set-mcp-err', role: 'alert'}, s.error),
    s.log ? h('p', {class: 'help'}, 'The server\'s full output is in ', h('span', {class: 'mono'}, s.log), '.') : null)
    : null;

  const actions = h('div', {class: 'set-row'}, h('div'), h('div', {class: 'set-actions'},
    h('button', {type: 'button', class: 'btn btn-sm', disabled: Boolean(busy) || !s.approved,
      onclick: () => test(s)}, icon('refresh', 'ic-sm'), 'Test connection'),
    h('button', {type: 'button', class: 'btn btn-sm', disabled: Boolean(busy) || Boolean(st.form),
      onclick: () => openForm('edit', s)}, 'Edit'),
    h('button', {type: 'button', class: 'btn btn-sm btn-danger', disabled: Boolean(busy),
      onclick: () => remove(s)}, icon('trash', 'ic-sm'), 'Remove')));

  return h('div', {class: 'card set-card', id},
    head,
    h('div', {class: 'set-row stack'}, ...spec, warn, approve),
    exposure,
    toolsRow(s),
    error,
    actions);
}

function commandText(text) {
  return h('pre', {class: 'cmd-text', tabindex: '0'}, h('code', null, text));
}

function toolsRow(s) {
  const busy = Boolean(st.busy[s.name]);
  const todo = s.tools.filter((t) => (t.state === 'new' || t.state === 'changed') && t.hash);
  const head = h('div', {class: 'set-mcp-head'}, h('span', {class: 'label'}, 'Tools (' + s.tools.length + ')'),
    todo.length ? h('button', {type: 'button', class: 'btn btn-sm', disabled: busy,
      onclick: () => setTools(s, Object.fromEntries(todo.map((t) => [t.name, t.hash])), [])},
    icon('check', 'ic-sm'), 'Approve ' + (todo.length === 1 ? 'it' : 'all ' + todo.length)) : null);
  const note = s.listed ? null : h('p', {class: 'help'}, s.tools.length
    ? 'These are the approved tools. Press Test connection to compare them with what the server offers now.'
    : 'Press Test connection to list the tools that the server offers.');
  const items = s.tools.map((t) => {
    const [text, kind] = TOOL_STATE[t.state] || [t.state, 'idle'];
    const hints = Object.entries(t.hints).map(([k, v]) => (HINT_TEXT[k] || [k, 'not ' + k])[v ? 0 : 1]);
    const button = t.state === 'approved' || t.state === 'gone'
      ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost', disabled: busy,
        onclick: () => setTools(s, {}, [t.name])}, 'Revoke')
      : t.hash ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost', disabled: busy,
        onclick: () => setTools(s, {[t.name]: t.hash}, [])}, 'Approve') : null;
    return h('li', {class: 'set-mcp-tool'},
      h('div', null,
        h('div', {class: 'set-mcp-head'}, h('span', {class: 'mono'}, t.full_name), pill(text, kind)),
        t.description ? h('p', {class: 'help'}, t.description) : null,
        t.state === 'changed' ? h('p', {class: 'help'}, 'The server changed this tool after you approved it. It is ' +
          'off until you approve the new definition.') : null,
        t.approved_description !== null && t.approved_description !== t.description
          ? h('p', {class: 'help faint'}, 'The description you approved: ' + (t.approved_description || '(none)'))
          : null,
        // the parameters as the model reads them: their texts reach the model too, so the user sees them before
        // an approval; a changed tool also shows the parameters that were approved
        t.schema ? h('details', null, h('summary', {class: 'help'}, 'Parameters the agent sees'),
          h('pre', {class: 'cmd-text', tabindex: '0'}, h('code', null, t.schema))) : null,
        t.approved_schema && t.approved_schema !== t.schema
          ? h('details', null, h('summary', {class: 'help faint'}, 'Parameters you approved before'),
            h('pre', {class: 'cmd-text', tabindex: '0'}, h('code', null, t.approved_schema)))
          : null,
        hints.length ? h('p', {class: 'help faint'}, 'The server says: ' + hints.join(', ') +
          '. These are hints only.') : null),
      button);
  });
  return h('div', {class: 'set-row stack'}, head, note, items.length ? h('ul', {class: 'set-mcp-tools'}, items) : null);
}

// ---------- actions ----------

function setEnabled(s, on) {
  act(s.name, on ? 'Turning on' : 'Turning off', () => call('/api/mcp/servers/' + s.name, 'PUT', {enabled: on}))
    .then((v) => v && toast(on ? s.name + ' is on for new agent chats.' : s.name + ' is off.'));
}

function setExposure(s, exposure) {
  act(s.name, 'Saving', () => call('/api/mcp/servers/' + s.name, 'PUT', {exposure}))
    .then((v) => v && toast('Saved. New agent chats use it.'));
}

function approveCommand(s) {
  act(s.name, 'Approving', () => call('/api/mcp/servers/' + s.name + '/approve', 'POST', {approval: s.approval}))
    .then((v) => v && toast('Approved. Press Test connection to list its tools.'));
}

function test(s) {
  act(s.name, 'Testing', () => call('/api/mcp/servers/' + s.name + '/test', 'POST'))
    .then((v) => v && toast(v.state === 'ready' ? s.name + ' answered with ' + v.tools.length + ' tools.' :
      s.name + ' did not start. Read the last error.', {kind: v.state === 'ready' ? null : 'err'}));
}

function setTools(s, approve, revoke) {
  act(s.name, 'Saving', () => call('/api/mcp/servers/' + s.name + '/tools', 'POST', {approve, revoke}));
}

async function remove(s) {
  const ok = await confirm({title: 'Remove ' + s.name + '?', text: 'The app stops the server, forgets its tool ' +
    'approvals and deletes its secrets from the Keychain.', confirmLabel: 'Remove', danger: true});
  if (!ok) return;
  const done = await act(s.name, 'Removing', () => call('/api/mcp/servers/' + s.name, 'DELETE'));
  if (done) {
    st.data.servers = st.data.servers.filter((x) => x.name !== s.name);
    render();
    toast(s.name + ' removed.');
  }
}

// ---------- the add and edit form ----------

function openForm(mode, s) {
  const f = {mode, name: s ? s.name : '', kind: s ? s.kind : 'stdio', command: s && s.command || '',
    args: s && s.args ? s.args.join('\n') : '', cwd: s && s.cwd || '', url: s && s.url || '',
    exposure: s ? s.exposure : 'deferred', token: '', tokenSet: Boolean(s && s.token_set), clearToken: false,
    env: s && s.env ? s.env.map((e) => ({name: e.name, value: '', set: e.set})) : []};
  st.form = f;
  st.errors = {};
  st.formErr = null;
  render();
  const first = document.getElementById(mode === 'add' ? 'set-mcp-f-name' : 'set-mcp-f-command') ||
    document.getElementById('set-mcp-f-url');
  if (first) first.focus();
}

function closeForm() {
  st.form = null;
  st.errors = {};
  st.formErr = null;
  render();
}

function field(key, label, control, help) {
  const id = 'set-mcp-f-' + key;
  control.id = id;
  const err = st.errors[key];
  control.setAttribute('aria-invalid', String(Boolean(err)));
  return h('div', {class: 'set-row stack'},
    h('label', {class: 'label', for: id}, label),
    control,
    err ? h('p', {class: 'field-err', role: 'alert'}, icon('alert', 'ic-sm'), err) : null,
    help ? h('p', {class: 'help'}, help) : null);
}

function input(key, attrs) {
  const f = st.form;
  return h('input', {class: 'input', value: f[key], spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off',
    ...attrs, oninput: (e) => {
      f[key] = e.target.value;
    }});
}

function form(s) {
  const f = st.form;
  const adding = f.mode === 'add';
  const rows = [];
  if (adding) {
    rows.push(field('name', 'Name', input('name', {placeholder: 'git', maxlength: '24'}),
      'Lowercase letters and digits, with - or _ between them. The agent\'s tools start with it: name__tool.'));
    const kinds = h('div', {class: 'seg', role: 'group', 'aria-label': 'Kind'},
      [['stdio', 'Local program'], ['remote', 'Remote server']].map(([k, label]) => h('button', {type: 'button',
        'aria-pressed': String(f.kind === k), onclick: () => {
          f.kind = k;
          render();
        }}, label)));
    rows.push(h('div', {class: 'set-row'}, h('div', null, h('span', {class: 'label'}, 'Kind'),
      h('p', {class: 'help'}, f.kind === 'remote' ? 'A service at an https URL. It sends data off this Mac.' :
        'A program on this Mac that talks on stdin and stdout.')), kinds));
  }
  if (f.kind === 'stdio') {
    rows.push(field('command', 'Program', input('command', {placeholder: '/opt/homebrew/bin/uvx', class: 'input mono'}),
      'Use the absolute path. A bare name is looked up in the app\'s PATH.'));
    const args = h('textarea', {class: 'textarea mono', rows: '3', spellcheck: 'false',
      placeholder: 'mcp-server-git\n--repository\n/Users/you/project', oninput: (e) => {
        f.args = e.target.value;
      }});
    args.value = f.args;
    rows.push(field('args', 'Arguments', args, 'One argument a line, without quotes.'));
    rows.push(field('cwd', 'Working folder', input('cwd', {placeholder: 'Empty: the app\'s folder'})));
    rows.push(envRows());
  } else {
    rows.push(field('url', 'URL', input('url', {type: 'url', inputmode: 'url', placeholder: 'https://example.com/mcp'}),
      'https:// only. Plain http:// works only for a server on this Mac (127.0.0.1).'));
    const token = h('input', {class: 'input', type: 'password', autocomplete: 'off', value: f.token,
      placeholder: f.tokenSet && !f.clearToken ? 'Stored in the Keychain. Type to replace it.' : 'Optional',
      oninput: (e) => {
        f.token = e.target.value;
      }});
    const clear = f.tokenSet ? h('label', {class: 'help'}, h('input', {type: 'checkbox', checked: f.clearToken,
      onchange: (e) => {
        f.clearToken = e.target.checked;
      }}), ' Remove the stored token') : null;
    rows.push(field('secrets.token', 'Bearer token', token, 'Sent in the Authorization header. Stored in the ' +
      'macOS Keychain, never in a file.'), clear);
  }
  if (adding) {
    const select = h('select', {class: 'select', onchange: (e) => {
      f.exposure = e.target.value;
    }}, Object.entries(EXPOSURE_TEXT).map(([k, label]) => h('option', {value: k, selected: k === f.exposure}, label)));
    rows.push(field('exposure', 'How the agent sees its tools', select));
  }
  const busy = Boolean(st.busy[f.name || '+']);
  return h('div', {class: 'card set-card'},
    h('div', {class: 'set-row'}, h('span', {class: 'label'}, adding ? 'Add an MCP server' : 'Edit ' + s.name)),
    ...rows,
    st.formErr ? h('p', {class: 'field-err', role: 'alert'}, icon('alert', 'ic-sm'), st.formErr) : null,
    h('div', {class: 'set-row'}, h('p', {class: 'help'}, adding ? 'The server stays off until you approve it and ' +
      'turn it on.' : 'A changed command or URL needs a new approval.'),
    h('div', {class: 'set-actions'},
      h('button', {type: 'button', class: 'btn', onclick: closeForm}, 'Cancel'),
      h('button', {type: 'button', class: 'btn btn-primary', disabled: busy, onclick: submit},
        adding ? 'Add' : 'Save'))));
}

function envRows() {
  const f = st.form;
  const rows = f.env.map((e, i) => {
    const name = h('input', {class: 'input mono', value: e.name, placeholder: 'API_KEY', spellcheck: 'false',
      autocomplete: 'off', 'aria-label': 'Variable name', disabled: e.set && !e.fresh, oninput: (ev) => {
        e.name = ev.target.value;
      }});
    const value = h('input', {class: 'input', type: 'password', value: e.value, autocomplete: 'off',
      'aria-label': 'Value of ' + (e.name || 'the variable'),
      placeholder: e.set ? 'Stored in the Keychain. Type to replace it.' : 'Value',
      oninput: (ev) => {
        e.value = ev.target.value;
      }});
    return h('div', {class: 'path-row'}, name, value,
      h('button', {type: 'button', class: 'btn btn-sm btn-ghost btn-icon', title: 'Remove this variable',
        'aria-label': 'Remove this variable', onclick: () => {
          f.env.splice(i, 1);
          render();
        }}, icon('x', 'ic-sm')));
  });
  const err = st.errors.env || Object.entries(st.errors).filter(([k]) => k.startsWith('secrets.') &&
    k !== 'secrets.token').map(([, v]) => v)[0];
  return h('div', {class: 'set-row stack'}, h('span', {class: 'label'}, 'Environment variables'),
    ...rows,
    err ? h('p', {class: 'field-err', role: 'alert'}, icon('alert', 'ic-sm'), err) : null,
    h('div', null, h('button', {type: 'button', class: 'btn btn-sm', onclick: () => {
      f.env.push({name: '', value: '', set: false, fresh: true});
      render();
    }}, icon('plus', 'ic-sm'), 'Add a variable')),
    h('p', {class: 'help'}, 'For API keys and similar values. The values go to the macOS Keychain, never to a file. ' +
      'The server gets only these variables and HOME, LOGNAME, PATH, SHELL, TERM and USER.'));
}

async function submit() {
  const f = st.form;
  const adding = f.mode === 'add';
  const body = {exposure: adding ? f.exposure : undefined};
  const secrets = {};
  if (adding) Object.assign(body, {name: f.name.trim(), kind: f.kind});
  if (f.kind === 'stdio') {
    body.command = f.command.trim();
    body.args = f.args.split('\n').map((a) => a.trim()).filter(Boolean);
    body.cwd = f.cwd.trim();
    body.env = f.env.map((e) => e.name.trim()).filter(Boolean);
    for (const e of f.env) if (e.name.trim() && e.value) secrets[e.name.trim()] = e.value;
  } else {
    body.url = f.url.trim();
    if (f.token) secrets.token = f.token;
    else if (f.clearToken) secrets.token = null;
  }
  body.secrets = secrets;
  const key = adding ? '+' : f.name;
  st.busy[key] = 'Saving';
  render();
  try {
    const view = adding ? await call('/api/mcp/servers', 'POST', body)
      : await call('/api/mcp/servers/' + f.name, 'PUT', body);
    replace(view);
    delete st.busy[key];
    closeForm();
    toast(adding ? view.name + ' added. Read the command and approve it.' : 'Saved.');
  } catch (e) {
    delete st.busy[key];
    st.errors = (e.detail && e.detail.fields) || {};
    st.formErr = Object.keys(st.errors).length ? null : errText(e);
    render();
  }
}
