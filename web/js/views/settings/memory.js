// Settings > Chat memory (app/memory.py, app/memory_api.py): the memory switches, the two stores with their entries
// (add, edit, delete, clear all) and the chat search index. Every change saves at once through /api/memory and applies
// from the next chat: a chat keeps the memory it started with. A store write sends the version this section read, so
// it never overwrites an entry the agent saved meanwhile (409: the section reads the memory again).

import {h, icon, toast, confirm} from '../../ui.js';

const SWITCHES = [
  ['enabled', 'Memory', 'Agent and Workstation chats see the saved entries and can save new ones with the memory ' +
    'tool. Off: no chat sees or changes the memory, and chat search is off.'],
  ['ask', 'Ask before saving', 'Each change the model wants to make shows on an approval card first. Off: the model ' +
    'saves without asking, except in a chat that has web pages or files in it.'],
  ['general', 'Memory in General chats', 'General chats see the memory too. Off by default.'],
];
const STORE_HELP = {
  user: 'Who you are and what you prefer.',
  notes: 'Facts about your setup and lessons from earlier chats.',
};

function json(method, body) {
  return {method, headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)};
}

export function memorySection(ctx) {
  // editing: [store, index] of the entry in the edit field, with its text; drafts: the add fields' text; errors: a
  // store's last refused change, shown under it
  const m = {data: null, busy: false, editing: null, editText: '', drafts: {}, errors: {}};
  const body = h('div', {class: 'card set-card'}, h('div', {class: 'set-row'}, h('p', {class: 'muted'}, 'Loading the memory...')));
  const err = h('p', {class: 'field-err', role: 'alert', hidden: true});
  const sec = h('section', {class: 'set-sec', 'aria-labelledby': 'set-mem'},
    h('h2', {id: 'set-mem'}, 'Chat memory'),
    h('p', null, 'Short facts that the chat agent keeps across chats, and the search over your earlier chats. ' +
      'Everything stays on this Mac in outputs/memory. A change applies from the next chat.'),
    body, err);

  function fail(e) {
    err.replaceChildren(icon('alert', 'ic-sm'), e.message);
    err.hidden = false;
  }

  async function call(path, opts, store) {
    if (m.busy) return false;
    m.busy = true;
    err.hidden = true;
    m.errors = {};
    try {
      m.data = await ctx.api.request(path, opts);
      return true;
    } catch (e) {
      if (e.status === 409) {  // the agent saved meanwhile: show the memory as it is now
        await load();
        toast('The memory changed meanwhile. Check the entries and try again.');
      } else if (store) m.errors[store] = e.message;
      else fail(e);
      return false;
    } finally {
      m.busy = false;
      if (sec.isConnected || m.data) render();
    }
  }

  async function load() {
    try {
      m.data = await ctx.api.request('/api/memory');
      err.hidden = true;
    } catch (e) {
      fail(e);
    }
    render();
  }

  function setStore(store, entries) {
    return call('/api/memory/stores/' + store, json('PUT', {entries, version: m.data.version}), store);
  }

  function switchRow([key, label, help]) {
    const id = 'set-mem-' + key;
    const off = key !== 'enabled' && !m.data.settings.enabled;
    const input = h('input', {type: 'checkbox', role: 'switch', id, checked: m.data.settings[key], disabled: m.busy || off,
      'aria-labelledby': id + '-label', 'aria-describedby': id + '-help',
      onchange: async (e) => {
        if (await call('/api/memory/settings', json('PUT', {[key]: e.target.checked}))) toast('Saved. New chats use it.');
        document.getElementById(id)?.focus();
      }});
    return h('div', {class: 'set-row'},
      h('div', null, h('span', {class: 'label', id: id + '-label'}, label), h('p', {class: 'help', id: id + '-help'}, help)),
      h('label', {class: 'switch', for: id}, input, h('span', {class: 'track'})));
  }

  function entryRow(st, i) {
    const text = st.entries[i];
    const what = 'entry ' + (i + 1) + ' of ' + st.label;
    if (m.editing && m.editing[0] === st.id && m.editing[1] === i) {
      const input = h('input', {class: 'input', id: 'set-mem-edit', value: m.editText, maxlength: m.data.max_entry,
        'aria-label': 'Edit ' + what, oninput: (e) => {
          m.editText = e.target.value;
        }, onkeydown: (e) => {
          if (e.key === 'Enter') save();
          if (e.key === 'Escape') cancel();
        }});
      const save = async () => {
        const entries = st.entries.slice();
        entries[i] = input.value;
        if (await setStore(st.id, entries)) {
          m.editing = null;
          render();
          toast('Entry saved.');
        } else document.getElementById('set-mem-edit')?.focus();
      };
      const cancel = () => {
        m.editing = null;
        render();
      };
      queueMicrotask(() => {
        if (document.activeElement === input) return;
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
      return h('li', {class: 'mem-entry is-edit'}, input,
        h('div', {class: 'set-actions'},
          h('button', {type: 'button', class: 'btn btn-sm', onclick: cancel}, 'Cancel'),
          h('button', {type: 'button', class: 'btn btn-sm btn-primary', onclick: save}, 'Save')));
    }
    return h('li', {class: 'mem-entry'}, h('span', {class: 'mem-text'}, text),
      h('div', {class: 'mem-btns'},
        h('button', {type: 'button', class: 'btn btn-sm btn-ghost', 'aria-label': 'Edit ' + what,
          disabled: m.busy, onclick: () => {
            m.editing = [st.id, i];
            m.editText = text;
            m.errors = {};
            render();
          }}, 'Edit'),
        h('button', {type: 'button', class: 'btn btn-sm btn-ghost btn-icon', title: 'Delete', 'aria-label': 'Delete ' + what,
          disabled: m.busy, onclick: async () => {
            if (await setStore(st.id, st.entries.filter((_, j) => j !== i))) toast('Entry deleted.');
          }}, icon('trash', 'ic-sm'))));
  }

  function storeRow(st) {
    const pct = Math.min(100, Math.round(100 * st.used / st.cap));
    const color = pct >= 90 ? 'var(--warn)' : 'var(--accent)';
    const id = 'set-mem-add-' + st.id;
    const errId = id + '-err';
    const error = m.errors[st.id];
    const input = h('input', {class: 'input', id, value: m.drafts[st.id] || '', maxlength: m.data.max_entry,
      placeholder: 'A new entry, one short line', 'aria-invalid': String(Boolean(error)), 'aria-describedby': errId,
      oninput: (e) => {
        m.drafts[st.id] = e.target.value;
      }, onkeydown: (e) => {
        if (e.key === 'Enter') add();
      }});
    const add = async () => {
      if (!input.value.trim()) return;
      const ok = await setStore(st.id, [...st.entries, input.value]);
      if (ok) {
        m.drafts[st.id] = '';
        render();
        toast('Entry added.');
      }
      document.getElementById(id)?.focus();
    };
    return h('div', {class: 'set-row stack'},
      h('div', {class: 'mem-head'}, h('span', {class: 'label'}, st.label),
        h('span', {class: 'help num'}, st.used.toLocaleString() + ' of ' + st.cap.toLocaleString() + ' characters (' + pct + '%)')),
      h('p', {class: 'help'}, STORE_HELP[st.id] || ''),
      h('div', {class: 'meter', role: 'img', 'aria-label': st.label + ': ' + pct + '% full'},
        h('span', {style: {width: pct + '%', background: color}})),
      st.entries.length
        ? h('ul', {class: 'mem-list', 'aria-label': st.label}, st.entries.map((_, i) => entryRow(st, i)))
        : h('p', {class: 'help'}, 'No entries.'),
      h('div', {class: 'path-row'}, h('label', {class: 'sr-only', for: id}, 'Add an entry to ' + st.label), input,
        h('button', {type: 'button', class: 'btn btn-sm', disabled: m.busy, onclick: add}, icon('plus', 'ic-sm'), 'Add')),
      h('p', {class: 'field-err', id: errId, role: 'alert', hidden: !error}, error ? [icon('alert', 'ic-sm'), error] : null));
  }

  function searchRow() {
    const st = m.data.search;
    const text = st.available
      ? 'The search_chats tool finds words in your earlier chats. Case and accents do not matter. Temporary chats are ' +
        'never indexed. ' + st.chats + (st.chats === 1 ? ' chat, ' : ' chats, ') + st.messages + ' messages in the index.'
      : 'Not available: the SQLite of this Python has no FTS5.';
    return h('div', {class: 'set-row'},
      h('div', null, h('span', {class: 'label'}, 'Chat search'), h('p', {class: 'help'}, text)),
      st.available ? h('button', {type: 'button', class: 'btn btn-sm', disabled: m.busy, onclick: async () => {
        if (await call('/api/memory/index', json('POST'))) toast('The chat search index was built again.');
      }}, icon('refresh', 'ic-sm'), 'Rebuild index') : null);
  }

  function clearRow() {
    const n = m.data.stores.reduce((a, st) => a + st.entries.length, 0);
    return h('div', {class: 'set-row'},
      h('div', null, h('span', {class: 'label'}, 'Clear the memory'),
        h('p', {class: 'help'}, 'Deletes every entry of both stores. Earlier chats stay as they are.')),
      h('button', {type: 'button', class: 'btn btn-sm btn-danger', disabled: m.busy || !n, onclick: async () => {
        const ok = await confirm({title: 'Clear the memory?', text: 'This deletes ' + n + (n === 1 ? ' entry' : ' entries') +
          '. You cannot undo it.', confirmLabel: 'Clear', danger: true});
        if (ok && await call('/api/memory/clear', json('POST'))) toast('The memory was cleared.');
      }}, 'Clear all'));
  }

  function render() {
    if (!m.data) return;
    const focus = document.activeElement && sec.contains(document.activeElement) ? document.activeElement.id : null;
    body.replaceChildren(...SWITCHES.map(switchRow), ...m.data.stores.map(storeRow), searchRow(), clearRow());
    if (focus) document.getElementById(focus)?.focus();
  }

  load();
  return sec;
}
