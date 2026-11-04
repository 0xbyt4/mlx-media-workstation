// Settings > Skills (app/skills_api.py): the agent's skills with an on/off switch each, the error of a broken one,
// its SKILL.md, the user's skills folder, the import of a skill folder or .zip from this Mac (check first: the files,
// the scripts and the scan's findings; then the user confirms) and the delete of an imported skill. A change applies
// to new chats: a running chat keeps the skills it started with. There is no install from the network.

import {h, icon, toast, confirm} from '../../ui.js';

const SOURCE_TEXT = {app: 'App', user: 'Imported', folder: 'Your folder'};

export function skillsSection(ctx, signal) {
  const st = {data: null, open: {}, texts: {}, check: null, busy: false, path: ''};
  const list = h('div', {class: 'card set-card'}, h('div', {class: 'set-row stack'},
    h('p', {class: 'help'}, 'Loading the skills...')));
  const foot = h('p', {class: 'help', 'aria-live': 'polite'});
  const folderBox = h('div', {class: 'card set-card'});
  const importBox = h('div', {class: 'card set-card'});
  const intro = h('p', null, 'Skills are guides that teach the agent a kind of task (the Agent Skills format, ',
    h('code', null, 'SKILL.md'), '). A new chat lists the skills that are on, and the agent loads one when a task ' +
    'matches it. Type ', h('code', null, '/skill <name>'), ' at the start of a message to use one. A change applies ' +
    'to new chats. Skill scripts never run by themselves: Python and Shell ask you before each run.');
  const sec = h('section', {class: 'set-sec', 'aria-labelledby': 'set-sk'},
    h('h2', {id: 'set-sk'}, 'Skills'), intro, list, foot, folderBox, importBox);

  async function call(path, opts) {
    return ctx.api.request('/api/skills' + path, Object.assign({signal}, opts));
  }

  function json(method, body) {
    return {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  }

  async function load() {
    try {
      show(await call(''));
    } catch (e) {
      if (e.name === 'AbortError') return;
      list.replaceChildren(h('div', {class: 'set-row stack'}, h('p', {class: 'field-err', role: 'alert'},
        icon('alert', 'ic-sm'), 'The skills could not be read: ' + e.message)));
    }
  }

  function show(data) {
    st.data = data;
    renderList();
    renderFolder();
    renderImport();
  }

  // ---------- the list ----------

  function renderList() {
    const d = st.data;
    list.replaceChildren(...(d.skills.length ? d.skills.map(skillRow)
      : [h('div', {class: 'set-row stack'}, h('p', {class: 'help'}, 'No skills yet.'))]));
    foot.textContent = 'The list of skills takes about ' + d.catalog_tokens + ' of ' + d.budget +
      ' tokens in a new chat\'s prompt. Skills that do not fit are left out of the list but stay usable.';
  }

  function skillRow(s) {
    const key = s.source + '/' + s.name;
    const id = 'set-sk-' + s.source + '-' + s.name;
    const sw = h('input', {type: 'checkbox', role: 'switch', id, checked: s.enabled, disabled: Boolean(s.error) || st.busy,
      'aria-labelledby': id + '-label', 'aria-describedby': id + '-help',
      onchange: (e) => toggle(s, e.target.checked)});
    const facts = [];
    if (s.needs.length) facts.push('Needs: ' + s.needs.join(', ') + '. A chat without them does not list it.');
    if (s.scripts.length) facts.push('Scripts: ' + s.scripts.join(', ') + '.');
    if (s.manual) facts.push('Only with /skill ' + s.name + ': the agent does not load it by itself.');
    if (s.license) facts.push('Licence: ' + s.license + '.');
    const view = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', 'aria-expanded': String(Boolean(st.open[key])),
      'aria-controls': id + '-md', onclick: () => toggleView(s)}, icon('file', 'ic-sm'),
    st.open[key] ? 'Hide SKILL.md' : 'View SKILL.md');
    const del = s.source === 'user' ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost btn-danger',
      disabled: st.busy, onclick: () => remove(s)}, icon('trash', 'ic-sm'), 'Delete') : null;
    const text = st.texts[key];
    return h('div', {class: 'set-row'},
      h('div', {style: {minWidth: '0', display: 'grid', gap: '4px'}},
        h('div', {class: 'chips', style: {alignItems: 'center', gap: '6px 8px'}},
          h('span', {class: 'label mono', id: id + '-label'}, s.name),
          h('span', {class: 'chip'}, SOURCE_TEXT[s.source] || s.source),
          s.error ? h('span', {class: 'pill pill-err'}, 'Broken') : null),
        h('p', {class: 'help', id: id + '-help'}, s.error ? s.path : s.description),
        s.error ? h('p', {class: 'field-err', role: 'alert'}, icon('alert', 'ic-sm'), s.error) : null,
        ...s.warnings.map((w) => h('p', {class: 'help'}, icon('info', 'ic-sm'), ' ', w)),
        facts.length ? h('p', {class: 'help'}, facts.join(' ')) : null,
        h('div', {class: 'set-actions', style: {justifyContent: 'flex-start'}}, view, del),
        st.open[key] ? h('pre', {class: 'cmd-text', id: id + '-md', tabindex: '0', style: {maxHeight: '320px', overflow: 'auto'},
          'aria-label': 'SKILL.md of ' + s.name}, text === undefined ? 'Loading...' : text) : null),
      h('label', {class: 'switch', for: id, title: s.error ? 'A broken skill cannot be turned on.' : null}, sw,
        h('span', {class: 'track'})));
  }

  async function toggleView(s) {
    const key = s.source + '/' + s.name;
    st.open[key] = !st.open[key];
    renderList();
    if (!st.open[key] || st.texts[key] !== undefined) return;
    try {
      const got = await call('/' + encodeURIComponent(s.source) + '/' + encodeURIComponent(s.name));
      st.texts[key] = got.text;
    } catch (e) {
      if (e.name === 'AbortError') return;
      st.texts[key] = 'Cannot show it: ' + e.message;
    }
    renderList();
  }

  async function toggle(s, on) {
    st.busy = true;
    try {
      show(await call('/' + encodeURIComponent(s.source) + '/' + encodeURIComponent(s.name), json('PUT', {enabled: on})));
      toast(s.name + (on ? ' is on for new chats.' : ' is off for new chats.'));
    } catch (e) {
      if (e.name !== 'AbortError') toast(e.message, {kind: 'err'});
    } finally {
      st.busy = false;
      if (st.data) renderList();
    }
  }

  async function remove(s) {
    const ok = await confirm({title: 'Delete the skill ' + s.name + '?', text: 'Its folder in outputs/skills/ is ' +
      'deleted. A chat that uses it keeps what it already read.', confirmLabel: 'Delete', danger: true});
    if (!ok) return;
    st.busy = true;
    try {
      show(await call('/user/' + encodeURIComponent(s.name), {method: 'DELETE'}));
      toast('Deleted ' + s.name + '.');
    } catch (e) {
      if (e.name !== 'AbortError') toast(e.message, {kind: 'err'});
    } finally {
      st.busy = false;
      if (st.data) renderList();
    }
  }

  // ---------- the user's folder ----------

  function renderFolder() {
    const d = st.data;
    const input = h('input', {class: 'input', id: 'set-sk-folder', type: 'text', value: d.folder || '',
      placeholder: '/Users/you/.agents/skills', spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off',
      'aria-describedby': 'set-sk-folder-help set-sk-folder-err'});
    const err = h('p', {class: 'field-err', id: 'set-sk-folder-err', role: 'alert', hidden: true});
    const save = async (value) => {
      err.hidden = true;
      try {
        show(await call('/folder', json('PUT', {folder: value})));
        toast(value ? 'The skills of this folder are listed. Turn on the ones you want.' : 'The folder is no longer read.');
      } catch (e) {
        if (e.name === 'AbortError') return;
        err.replaceChildren(icon('alert', 'ic-sm'), e.message);
        err.hidden = false;
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') save(input.value.trim());
    });
    folderBox.replaceChildren(h('div', {class: 'set-row stack'},
      h('label', {class: 'label', for: 'set-sk-folder'}, 'Your skills folder'),
      h('div', {class: 'path-row'}, input,
        h('button', {type: 'button', class: 'btn btn-sm', onclick: () => save(input.value.trim())}, 'Save'),
        d.folder ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => save(null)}, 'Clear') : null),
      err,
      h('p', {class: 'help', id: 'set-sk-folder-help'}, 'Optional. A folder of skills that you keep yourself, such ' +
        'as the one other agents read (~/.agents/skills). The app only reads it. Its skills start off: read each ' +
        'one, then turn it on. Imported skills go to ', h('span', {class: 'mono'}, d.user_dir), '.')));
  }

  // ---------- import ----------

  function renderImport() {
    const input = h('input', {class: 'input', id: 'set-sk-import', type: 'text', placeholder: '/Users/you/Downloads/my-skill.zip',
      value: st.path, spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off',
      'aria-describedby': 'set-sk-import-help set-sk-import-err', oninput: () => (st.path = input.value)});
    const err = h('p', {class: 'field-err', id: 'set-sk-import-err', role: 'alert', hidden: true});
    const checkBtn = h('button', {type: 'button', class: 'btn btn-sm', onclick: () => check(input.value.trim())}, 'Check');
    const check = async (path) => {
      err.hidden = true;
      if (!path) {
        err.replaceChildren(icon('alert', 'ic-sm'), 'Enter the full path of a skill folder or a .zip file.');
        err.hidden = false;
        return;
      }
      checkBtn.disabled = true;
      try {
        st.check = await call('/import/check', json('POST', {path}));
        renderImport();
      } catch (e) {
        if (e.name === 'AbortError') return;
        err.replaceChildren(icon('alert', 'ic-sm'), e.message);
        err.hidden = false;
        checkBtn.disabled = false;
      }
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') check(input.value.trim());
    });
    importBox.replaceChildren(h('div', {class: 'set-row stack'},
      h('label', {class: 'label', for: 'set-sk-import'}, 'Import a skill'),
      h('div', {class: 'path-row'}, input, checkBtn),
      err,
      h('p', {class: 'help', id: 'set-sk-import-help'}, 'The full path of a skill folder (with a SKILL.md) or a .zip ' +
        'file on this Mac. Check shows its files, its scripts and anything that looks dangerous before you import it. ' +
        'A skill is text that the agent follows: import only skills from people you trust.')),
    ...(st.check ? [review(st.check)] : []));
  }

  function review(c) {
    const findings = c.findings.length
      ? h('div', {class: 'banner banner-warn', role: 'alert'}, icon('alert'), h('div', {class: 'banner-body'},
        h('strong', null, c.findings.length === 1 ? '1 thing to check' : c.findings.length + ' things to check'),
        h('ul', {style: {margin: '4px 0 0', paddingLeft: '18px'}}, c.findings.map((f) => h('li', null,
          h('span', {class: 'mono'}, f.file + (f.line ? ':' + f.line : '')), ' ', f.why,
          f.text ? [h('br'), h('code', null, f.text)] : null)))))
      : h('p', {class: 'help'}, icon('check', 'ic-sm'), ' The scan found no dangerous pattern. Read the skill anyway.');
    const files = c.files.length
      ? h('ul', {class: 'help', style: {margin: '0', paddingLeft: '18px'}}, c.files.map((f) => h('li', null,
        h('span', {class: 'mono'}, f.path), f.script ? [' ', h('span', {class: 'chip'}, 'script')] : null)))
      : h('p', {class: 'help'}, 'Only SKILL.md.');
    const go = h('button', {type: 'button', class: 'btn btn-primary', disabled: c.blocked, onclick: () => doImport(c)},
      'Import');
    const cancel = h('button', {type: 'button', class: 'btn', onclick: () => {
      st.check = null;
      renderImport();
    }}, 'Cancel');
    return h('div', {class: 'set-row stack', 'aria-label': 'Check of ' + c.name},
      h('div', {class: 'chips', style: {alignItems: 'center', gap: '6px 8px'}},
        h('span', {class: 'label mono'}, c.name), c.scripts.length ? h('span', {class: 'pill pill-warn'},
          c.scripts.length === 1 ? '1 script' : c.scripts.length + ' scripts') : null),
      h('p', {class: 'help'}, c.description),
      ...c.warnings.map((w) => h('p', {class: 'help'}, icon('info', 'ic-sm'), ' ', w)),
      c.skipped.length ? h('p', {class: 'help'}, 'Left out (hidden or secret files): ' + c.skipped.join(', ') + '.') : null,
      findings,
      h('span', {class: 'label'}, 'Files'), files,
      h('details', null, h('summary', {class: 'help'}, 'SKILL.md'),
        h('pre', {class: 'cmd-text', tabindex: '0', style: {maxHeight: '320px', overflow: 'auto'}}, c.text)),
      h('div', {class: 'set-actions'}, cancel, go));
  }

  async function doImport(c) {
    if (c.findings.length || c.scripts.length) {
      const ok = await confirm({title: 'Import ' + c.name + '?', text: (c.findings.length ? 'The scan found ' +
        c.findings.length + ' thing(s) to check. ' : '') + (c.scripts.length ? 'It has scripts; they run only ' +
        'when you approve a Python or Shell call. ' : '') + 'Import it only when you trust where it comes from.',
      confirmLabel: 'Import', danger: Boolean(c.findings.length)});
      if (!ok) return;
    }
    try {
      const data = await call('/import', json('POST', {token: c.token}));
      st.check = null;
      st.path = '';
      show(data);
      toast(data.name + ' is imported and on for new chats.');
    } catch (e) {
      if (e.name !== 'AbortError') toast(e.message, {kind: 'err'});
    }
  }

  load();
  return sec;
}
