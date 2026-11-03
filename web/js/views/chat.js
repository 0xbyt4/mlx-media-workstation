// Chat: conversations with the local LLM (app/chat.py). An answer streams as server-sent events, keeps running on
// the server when the page leaves, and comes back with GET /api/chats/{id}/stream. Model text is rendered as
// Markdown by building DOM nodes; it never goes into the page as HTML.
//
// An agent chat's answer is a series of steps: the model's text, then tool calls shown as compact cards. A call that
// needs the user's approval shows an approval card in the thread (POST .../approve); a page that comes back replays it
// from the stream. The model's plan (update_plan) is one checklist at the top of the answer, updated in place. The
// Tools menu turns tools on or off, grants a folder and revokes chat-wide approvals.

import {store} from '../store.js';
import {h, icon, toolIcon, grow, fmtBytes, fmtLeft, fmtTime, timeAgo, toast, inlineConfirm, lightbox, lsGet, lsSet, menu, closeMenu, media, downloadLink} from '../ui.js';

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const MAX_IMAGES = 4; // per message (app/chat.py MAX_IMAGES)
const MAX_IMAGE = 20 * 1024 * 1024;
// A chat's mode picks its system prompt (app/chat.py MODES) and is fixed after the first message. Only workstation
// answers get "Use in" cards.
const MODE_INFO = {
  workstation: {
    label: 'Workstation', icon: 'wand', help: 'Knows the app\'s tools. A text written for a tool gets a "Use in" button.',
    title: 'Ask about a prompt, a shot list or lyrics',
    text: 'The model runs on this Mac and nothing is uploaded. Attach an image to ask about it.',
    examples: [
      {title: 'Write a video prompt', text: 'Write a Text to Video prompt for a 5 second shot: a lighthouse keeper climbs a spiral staircase during a storm. Add the sounds.'},
      {title: 'Lyrics for a song', text: 'Write a style caption and lyrics (two short verses and a chorus) for a calm synth-pop song about a city in the rain.'},
      {title: 'Plan a short scene', text: 'Plan a 20 second museum scene at night as 4 shots. For each shot, say which tool to use and why.'},
      {title: 'Ask about an image', text: 'What should move in this image in a 5 second Image to Video clip? Write the prompt.', attach: true},
    ],
  },
  general: {
    label: 'General', icon: 'chat', help: 'A plain assistant for any question. It does not know the app\'s tools.',
    chatHelp: 'A plain assistant for any question.', // the chat profile has no media tools to compare with
    title: 'Ask anything',
    text: 'The model runs on this Mac and nothing is uploaded. It has no internet access. Attach an image to ask about it.',
    examples: [
      {title: 'Explain something', text: 'Explain how a heat pump works, in plain words and in under 150 words.'},
      {title: 'Make a text clearer', text: 'Make this text shorter and clearer. Keep the meaning and the tone:\n\n'},
      {title: 'Make a plan', text: 'Make a plan to learn the basics of Python in two weeks, one hour a day.'},
      {title: 'Ask about an image', text: 'What is in this image? Describe it in a few sentences.', attach: true},
    ],
  },
  agent: {
    label: 'Agent', icon: 'chip', help: 'A general assistant that can use tools: web search, Python in a sandbox, files in a folder you grant.',
    title: 'Ask, and let it use tools',
    text: 'The model runs on this Mac. It asks you before it sends anything off this Mac or runs code. Choose its tools in the Tools menu below.',
    examples: [
      {title: 'Look something up', text: 'Search the web: when was the Antikythera mechanism found, and by whom?'},
      {title: 'Compute with Python', text: 'Use Python to find the sum of the first 1000 prime numbers.'},
      {title: 'Read my notes', text: 'List the files in my folder and summarize the newest note.'},
      {title: 'Check the Library', text: 'Search my Library for images of a fox and list them.', full: true},
    ],
  },
};
const MODES = ['workstation', 'general', 'agent']; // the picker and the New chat menu offer these
const MODE_KEY = 'ws.chatMode'; // the mode of the last new chat, per browser
// Thinking levels (app/effort.py): id, label, hint. The times are the upper limits at about 17 tokens a second; in an
// agent answer they apply to each step, and steps after the first get less.
const EFFORTS = [
  ['off', 'Off', 'No thinking: fastest'],
  ['low', 'Low', 'Thinks up to about 1 minute'],
  ['medium', 'Medium', 'Thinks up to about 3 minutes'],
  ['high', 'High', 'Thinks up to about 6 minutes'],
  ['xhigh', 'XHigh', 'Thinks up to about 8 minutes'],
  ['max', 'Max', 'Slowest: no thinking limit'],
];
// Agent tools: the risk levels of app/agent/policy.py in plain words, the states of a call, and the user's answers.
const RISK = [
  {label: 'App only', pill: 'idle'},
  {label: 'Your folder', pill: 'idle'},
  {label: 'Leaves this Mac', pill: 'warn'},
  {label: 'Runs code', pill: 'err'},
];
// the tools that write in the granted folder: their risk pill names that, not the network or code
const WRITERS = {write_file: 'Writes files', export_from_workspace: 'Writes files', edit_file: 'Changes files'};

// network: false names an R2 tool that uses this Mac (create_job); undefined (a saved tool card) keeps the level's label
function riskLabel(name, risk, network) {
  if (WRITERS[name]) return WRITERS[name];
  if (risk === 2 && network === false) return 'Uses this Mac';
  return (RISK[risk] || RISK[3]).label;
}

const STATUS = {
  ok: ['ok', 'Done'], error: ['err', 'Error'], denied: ['warn', 'Denied'], refused: ['warn', 'Not allowed'],
  cancelled: ['idle', 'Stopped'], skipped: ['idle', 'Not run'], repeat: ['idle', 'Already done'],
};
const PLAN_TOOL = 'update_plan'; // its calls show as the answer's checklist, not as cards (app/agent/plan_tool.py)
const DECISION = {
  once: 'You allowed it once.', chat: 'You allowed it for this chat.', deny: 'You denied it.',
  timeout: 'Denied: no answer in time.', stopped: 'Stopped while it waited for your answer.',
};

// The modes a new chat can take here: the chat profile has no media tools, so it has no Workstation mode.
function offered() {
  return s.ctx.store.profile() === 'chat' ? MODES.filter((m) => m !== 'workstation') : MODES;
}

function modeHelp(mode) {
  const info = MODE_INFO[mode] || MODE_INFO.workstation;
  return s.ctx.store.profile() === 'chat' && info.chatHelp ? info.chatHelp : info.help;
}

// "Use in" cards need the media tools: only Workstation chats in the full profile show them.
function showCards() {
  return chatMode() === 'workstation' && s.ctx.store.profile() !== 'chat';
}

// Links in agent chats name their host: a page the agent read can make it write a link that carries private text off
// this Mac in one click.
function showHosts() {
  return chatMode() === 'agent';
}

// Kept across mounts for this session: the open chat, the composer draft, the chosen images, the thinking level picked
// per chat ('' = the chat the first message creates) and the mode for the next new chat.
const memo = {activeId: null, draft: '', attach: [], efforts: {}, mode: null};

let s = null; // the mounted view

// ---------- mount ----------

function mount(root, ctx, params) {
  root.classList.add('v-chat', 'view-full');
  s = {
    root, ctx, api: ctx.api, els: {}, offs: [], chats: [], chatsLoaded: false, chat: null, loading: false, live: null,
    query: '', openSeq: 0, sending: false, confirmFor: null, confirmEl: null, dragDepth: 0,
  };
  build();
  s.offs.push(
    store.on('llm', onModelState),
    store.on('system', onModelState),
    store.on('models', onModelState),
    store.on('jobs', () => {
      renderHead();
      if (s.live) paintLive(s.live);
      s.root.querySelectorAll('.chat-job[data-job]').forEach(paintJob);
    }),
    store.on('tools', () => {
      if (s.chat && !s.loading) renderThread(false);
    }),
    store.on('profile', onProfile),
    store.on('offline', (off) => {
      if (!off && memo.activeId && !s.live) reloadChat(memo.activeId);
    }),
  );
  s.listPoll = ctx.api.poll(loadList, (_, err) => (err ? 5000 : s && s.chats.some((c) => c.running) ? 3000 : 30000));
  document.addEventListener('keydown', onKey);
  renderAll();
  openChat((params && params.id) || memo.activeId || null);
  s.els.input.focus();
}

function unmount() {
  if (!s) return;
  closeMenu();
  closeTools();
  detachLive();
  s.listPoll.stop();
  for (const off of s.offs) off();
  document.removeEventListener('keydown', onKey);
  s = null;
}

// Another chat in the address (#/chat?id=...), or the Chat section opened again.
function update(params) {
  if (!s) return;
  const id = params && params.id;
  if (id && id !== memo.activeId) openChat(id);
  else if (!id && memo.activeId) s.ctx.router.replace('chat', {id: memo.activeId});
}

function onKey(e) {
  if (e.key === 'Escape' && s.els.chat.classList.contains('show-convos')) {
    e.preventDefault();
    showConvos(false);
  }
}

function onModelState() {
  renderHead();
  renderNotices();
  renderComposer();
  const blocked = modelFacts().blocked;
  if (blocked !== s.blocked) {
    s.blocked = blocked; // the examples and Regenerate follow it
    if (!s.loading) renderThread(false);
  }
  if (s.live) paintLive(s.live);
}

// The profile changed (web/README.md): the modes on offer, the "Use in" cards and the examples follow it.
function onProfile() {
  closeMenu();
  s.els.newMode.hidden = offered().length < 2;
  renderHead();
  renderComposer();
  if (!s.loading) renderThread(false);
}

// ---------- layout ----------

function build() {
  const els = s.els;
  els.search = h('input', {
    class: 'input', id: 'chat-search', type: 'search', placeholder: 'Search chats', autocomplete: 'off',
    oninput: () => {
      s.query = els.search.value;
      renderList();
    },
  });
  els.newBtn = h('button', {type: 'button', class: 'btn new-chat-main', onclick: () => newChat()}, icon('plus', 'ic-sm'), 'New chat');
  els.newMode = h('button', {
    type: 'button', class: 'btn btn-icon new-chat-mode', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
    'aria-label': 'New chat in a mode', title: 'New chat in a mode',
    hidden: offered().length < 2,
    onclick: () => menu(els.newMode, offered().map((m) => ({
      label: 'New ' + MODE_INFO[m].label + ' chat', icon: MODE_INFO[m].icon, run: () => newChat(m),
    }))),
  }, icon('chev-down', 'ic-sm'));
  els.list = h('ul', {class: 'convo-list', role: 'list', 'aria-label': 'Chats'});
  els.convos = h('aside', {class: 'convos', id: 'chat-convos', 'aria-label': 'Conversations'},
    h('div', {class: 'convos-head'}, h('div', {class: 'new-chat'}, els.newBtn, els.newMode),
      h('div', {class: 'search'}, h('label', {class: 'sr-only', for: 'chat-search'}, 'Search chats'), icon('search', 'ic-sm'), els.search)),
    els.list);

  els.toggle = h('button', {
    type: 'button', class: 'btn btn-ghost btn-icon conv-toggle', 'aria-label': 'Show conversations',
    'aria-controls': 'chat-convos', 'aria-expanded': 'false',
    onclick: () => showConvos(!els.chat.classList.contains('show-convos')),
  }, icon('menu'));
  els.title = h('h2', {id: 'chat-title'});
  els.modeChip = h('span', {class: 'chip mode-chip'});
  els.chip = h('span', {class: 'model-chip', role: 'status'});
  els.unload = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', hidden: true, onclick: unload}, 'Unload');
  els.msgs = h('div', {class: 'msgs', role: 'region', 'aria-labelledby': 'chat-title', tabindex: '0'});
  els.status = h('div', {class: 'sr-only', role: 'status', 'aria-live': 'polite'});

  els.notices = h('div', {class: 'chat-notices'});
  els.note = h('div', {class: 'composer-note', role: 'alert', hidden: true});
  els.attach = h('div', {class: 'composer-attach', hidden: true});
  els.input = h('textarea', {id: 'chat-input', rows: '1', 'aria-describedby': 'chat-hint'});
  els.input.value = memo.draft;
  els.input.addEventListener('input', () => {
    memo.draft = els.input.value;
    grow(els.input);
  });
  els.input.addEventListener('keydown', onInputKey);
  els.input.addEventListener('paste', onPaste);
  els.file = h('input', {
    type: 'file', accept: IMAGE_TYPES.join(','), multiple: true, hidden: true, tabindex: '-1',
    onchange: () => {
      addFiles([...els.file.files]);
      els.file.value = '';
    },
  });
  els.attachBtn = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => els.file.click()},
    icon('clip', 'ic-sm'), 'Image');
  els.toolsBtn = h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm tools-btn', 'aria-haspopup': 'dialog', 'aria-expanded': 'false',
    'aria-controls': 'chat-tools', title: 'The tools this chat may use, the folder it may read, and what it may do without asking.',
    onclick: () => (s.tools ? closeTools(true) : openTools()),
  }, icon('wand', 'ic-sm'), 'Tools');
  els.toolsPop = h('div', {class: 'tools-pop', id: 'chat-tools', role: 'dialog', 'aria-label': 'Tools for this chat', hidden: true});
  els.toolsPop.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeTools(true);
    }
  });
  els.effort = h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm effort-btn', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
    onclick: () => menu(els.effort, EFFORTS.map(([id, label, hint]) => ({
      label, sub: hint, checked: id === effortLevel(), run: () => pickEffort(id),
    }))),
  });
  els.send = h('button', {type: 'button', class: 'btn btn-primary btn-sm', onclick: () => (s.live ? stop() : send())});
  const composer = h('div', {class: 'composer'},
    els.attach,
    h('label', {class: 'sr-only', for: 'chat-input'}, 'Message'),
    els.input,
    h('div', {class: 'composer-bar'},
      els.attachBtn, els.file, els.toolsBtn,
      els.effort,
      h('span', {class: 'spacer'}),
      h('span', {class: 'composer-hint', id: 'chat-hint'},
        h('span', {class: 'kbd'}, 'Return'), ' sends, ', h('span', {class: 'kbd'}, 'Shift'), ' ', h('span', {class: 'kbd'}, 'Return'), ' new line'),
      els.send));

  els.thread = h('section', {class: 'thread', 'aria-label': 'Conversation'},
    h('div', {class: 'thread-head'}, els.toggle, h('div', {class: 'thread-title'}, els.title, els.modeChip), els.chip, els.unload),
    els.msgs,
    h('div', {class: 'composer-wrap'}, els.toolsPop, els.notices, els.note, composer),
    els.status);
  dropTarget(els.thread);

  els.chat = h('div', {class: 'chat'}, els.convos, els.thread);
  s.root.append(els.chat);
  grow(els.input);
}

function renderAll() {
  renderList();
  renderHead();
  renderNotices();
  renderThread(true);
  renderAttach();
  renderComposer();
}

function showConvos(open) {
  const els = s.els;
  els.chat.classList.toggle('show-convos', open);
  els.toggle.setAttribute('aria-expanded', String(open));
  if (open) els.search.focus();
  else if (els.convos.contains(document.activeElement)) els.toggle.focus();
}

// ---------- model state ----------

function modelName() {
  const llm = store.get('llm');
  return (llm && llm.model) || 'the local model';
}

// The server's rule (app/models.py fits): the model's peak plus the reserve must fit in this Mac's memory.
function modelFacts() {
  const llm = store.get('llm');
  const mem = store.memTotal();
  if (!llm) return {llm: null, blocked: false};
  const reserve = store.reserve();
  const tooLarge = !!(mem && llm.peak_gb + reserve > mem);
  return {llm, mem, reserve, tooLarge, missing: !llm.installed, blocked: tooLarge || !llm.installed};
}

function chipState(f) {
  const llm = f.llm;
  if (!llm) return ['idle', '...', ''];
  const peak = Math.round(llm.peak_gb) + ' GB';
  if (f.tooLarge) return ['err', 'Too large', 'needs ' + Math.round(llm.peak_gb + f.reserve) + ' GB, this Mac has ' + Math.round(f.mem) + ' GB'];
  if (f.missing) return ['warn', 'Not installed', 'install it in Models'];
  if ((s.live && s.live.waiting) || llm.waiting > 0) {
    const job = store.runningJob();
    return ['warn', 'Waiting for memory', job ? (job.tool_label || job.tool) + ' runs' : 'a media job runs'];
  }
  if (llm.state === 'loading') return ['run', 'Loading', 'about ' + peak];
  if (llm.state === 'ready') return ['ok', 'Ready', (llm.busy ? 'answering · ' : '') + peak + ' in memory'];
  return ['idle', 'Off', 'loads with the next message'];
}

// The toolbar line follows the mode: the agent's web tools send a query off this Mac after the user approves it.
function subtitleFor(mode) {
  return mode === 'agent' ? 'Runs on this Mac. Web tools send data only after you approve.' : 'Runs on this Mac. Nothing is uploaded.';
}

function renderHead() {
  const els = s.els;
  els.title.textContent = s.chat ? s.chat.title : memo.activeId ? 'Chat' : 'New chat';
  const mode = chatMode();
  s.ctx.shell.setSubtitle(subtitleFor(mode));
  els.modeChip.hidden = !mode;
  if (mode) {
    const info = MODE_INFO[mode] || MODE_INFO.workstation;
    els.modeChip.title = info.label + ' mode: ' + modeHelp(mode);
    els.modeChip.replaceChildren(icon(info.icon, 'ic-sm'), h('span', {class: 'sr-only'}, 'Mode: '), info.label);
  }
  const f = modelFacts();
  const [pill, label, sub] = chipState(f);
  els.chip.replaceChildren(icon('chip', 'ic-sm'), h('b', null, f.llm ? f.llm.model : 'Local model'),
    h('span', {class: 'pill pill-' + pill}, label), h('span', {class: 'sub'}, sub));
  const canUnload = !!(f.llm && f.llm.installed && f.llm.state !== 'off');
  if (!canUnload && document.activeElement === els.unload) els.input.focus();
  els.unload.hidden = !canUnload;
  els.unload.disabled = !!s.live;
  els.unload.title = s.live ? 'Stop the answer first.' : 'Free its memory now. It loads again with the next message.';
}

function renderNotices() {
  const f = modelFacts();
  const box = s.els.notices;
  box.replaceChildren();
  if (f.tooLarge) {
    box.append(h('div', {class: 'banner banner-warn'}, icon('alert'), h('div', {class: 'banner-body'}, h('span', null,
      h('b', null, f.llm.model + ' needs about ' + Math.round(f.llm.peak_gb) + ' GB of memory plus ' + Math.round(f.reserve) + ' GB for the system.'),
      ' This Mac has ' + Math.round(f.mem) + ' GB, so chat cannot run here.'))));
  } else if (f.missing) {
    box.append(h('div', {class: 'banner banner-warn'}, icon('alert'), h('div', {class: 'banner-body'},
      h('span', null, h('b', null, f.llm.model + ' is not installed.'), ' Chat and prompt help need it.'),
      h('div', {class: 'banner-actions'},
        h('button', {type: 'button', class: 'btn btn-sm btn-primary', onclick: () => s.ctx.router.go('models')}, 'Open Models')))));
  }
}

async function unload() {
  const me = s;
  me.els.unload.disabled = true;
  try {
    await me.api.unloadLlm();
    toast(modelName() + ' is unloaded. It loads again with the next message.');
  } catch (e) {
    toast(errText(e), {kind: 'err'});
  }
  await store.refresh('llm');
  if (s === me) renderHead();
}

// ---------- conversation list ----------

async function loadList() {
  const me = s;
  if (!me) return null;
  const chats = await me.api.chats();
  if (s !== me) return chats;
  s.chats = chats;
  s.chatsLoaded = true;
  renderList();
  return chats;
}

function renderList() {
  const list = s.els.list;
  // Do not redraw under an open inline confirm that has the focus.
  if (s.confirmEl && s.confirmEl.contains(document.activeElement)) return;
  // Keep keyboard focus on the same row when the list is drawn again (the poll redraws it).
  const focused = list.contains(document.activeElement) ? document.activeElement : null;
  const keep = focused && focused.dataset.chat ? {id: focused.dataset.chat, part: focused.dataset.part} : null;
  const q = s.query.trim().toLowerCase();
  const items = s.chats.filter((c) => !q || c.title.toLowerCase().includes(q));
  const running = (c) => !!(c.running || (s.live && s.live.chatId === c.id));
  const sub = (c) => timeAgo(c.updated) + ' · ' + c.messages + (c.messages === 1 ? ' message' : ' messages');
  // Redraw only when something shown changed: a redraw between mousedown and mouseup loses the click.
  const sig = JSON.stringify([q, memo.activeId, s.confirmFor, s.chatsLoaded, items.map((c) => [c.id, c.title, running(c), sub(c)])]);
  if (sig === s.listSig) return;
  s.listSig = sig;
  list.replaceChildren();
  if (!s.chats.length) {
    list.append(h('li', {class: 'convo-none'}, s.chatsLoaded ? 'No chats yet.' : 'Loading chats...'));
    return;
  }
  if (!items.length) list.append(h('li', {class: 'convo-none'}, 'No chat title contains "' + s.query.trim() + '".'));
  for (const c of items) {
    const current = c.id === memo.activeId;
    const open = h('button', {
      type: 'button', class: 'convo', 'data-chat': c.id, 'data-part': 'open', 'aria-current': current ? 'true' : null,
      onclick: () => {
        showConvos(false);
        if (c.id !== memo.activeId) openChat(c.id);
      },
    },
    h('span', {class: 'convo-title'}, c.title),
    h('span', {class: 'convo-sub'}, running(c) ? [h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Answering'] : sub(c)));
    const del = h('button', {
      type: 'button', class: 'btn btn-ghost btn-icon btn-sm convo-del', 'data-chat': c.id, 'data-part': 'del',
      'aria-label': 'Delete chat ' + c.title, title: 'Delete', onclick: () => askDelete(c),
    }, icon('trash', 'ic-sm'));
    const li = h('li', {class: 'convo-item' + (current ? ' is-current' : '')}, h('div', {class: 'convo-row'}, open, del));
    if (s.confirmFor === c.id && s.confirmEl) li.append(s.confirmEl);
    list.append(li);
  }
  if (keep) focusRow(keep.id, keep.part);
}

function askDelete(c) {
  if (s.confirmFor === c.id && s.confirmEl) {
    s.confirmEl.querySelector('button').focus();
    return;
  }
  s.confirmFor = c.id;
  s.confirmEl = inlineConfirm({
    text: 'Delete this chat and its images?',
    confirmLabel: 'Delete',
    onConfirm: () => deleteChat(c),
    onCancel: () => {
      s.confirmFor = s.confirmEl = null;
      renderList();
      focusRow(c.id, 'del');
    },
  });
  renderList();
}

async function deleteChat(c) {
  const me = s;
  try {
    await me.api.deleteChat(c.id);
  } catch (e) {
    // 409 while it answers: say so in the confirm row; the user can stop the answer and try again.
    if (s === me && me.confirmEl) me.confirmEl.querySelector('span').textContent = errText(e);
    return;
  }
  if (s !== me) return;
  const at = s.chats.findIndex((x) => x.id === c.id);
  s.confirmFor = s.confirmEl = null;
  s.chats = s.chats.filter((x) => x.id !== c.id);
  if (memo.activeId === c.id) openChat(null);
  renderList();
  toast('Chat deleted.');
  const next = s.chats[Math.min(at, s.chats.length - 1)];
  if (next) focusRow(next.id, 'open');
  else s.els.newBtn.focus();
}

function focusRow(id, part) {
  const el = s.els.list.querySelector('[data-chat="' + CSS.escape(id) + '"][data-part="' + part + '"]');
  if (el) el.focus();
}

// A new chat in the given mode, or in the mode of the last new chat.
function newChat(mode) {
  showConvos(false);
  if (mode) rememberMode(mode);
  if (memo.activeId || s.chat) openChat(null);
  else if (mode) {
    renderHead();
    renderThread(true);
    renderComposer();
  }
  s.els.input.focus();
}

// ---------- modes ----------

// The mode for the next new chat: the last one chosen in this browser, if the page still offers it.
function nextMode() {
  if (!memo.mode) {
    const saved = lsGet(MODE_KEY);
    memo.mode = saved;
  }
  return offered().includes(memo.mode) ? memo.mode : offered()[0];
}

function rememberMode(mode) {
  memo.mode = mode;
  lsSet(MODE_KEY, mode);
}

// The open chat's mode (a chat saved before modes is workstation), the next mode when no chat is open yet, or null
// while a chat loads.
function chatMode() {
  if (s.chat) return s.chat.mode || 'workstation';
  return memo.activeId ? null : nextMode();
}

// The segmented control on an empty chat. With no chat yet it only picks the mode for the chat that the first message
// creates; an existing empty chat changes on the server.
function modePicker() {
  if (offered().length < 2) return null;
  const mode = chatMode();
  const seg = h('div', {class: 'seg', role: 'group', 'aria-label': 'Chat mode', 'aria-describedby': 'chat-mode-help'},
    offered().map((m) => h('button', {
      type: 'button', 'data-mode': m, 'aria-pressed': String(m === mode), disabled: s.sending || s.modeBusy,
      onclick: () => pickMode(m),
    }, icon(MODE_INFO[m].icon, 'ic-sm'), MODE_INFO[m].label)));
  return h('div', {class: 'mode-pick'}, seg, h('p', {class: 'mode-help', id: 'chat-mode-help'}, modeHelp(mode)));
}

async function pickMode(mode) {
  if (mode === chatMode() || s.sending || s.modeBusy) return;
  const me = s;
  rememberMode(mode);
  if (s.chat) {
    const id = s.chat.id;
    s.modeBusy = true;
    renderComposer();
    try {
      const c = await me.api.setChatMode(id, mode);
      if (s !== me || !s.chat || s.chat.id !== id) return;
      s.chat.mode = c.mode;
      const row = s.chats.find((x) => x.id === id);
      if (row) row.mode = c.mode;
    } catch (e) {
      if (s !== me) return;
      showNote(errText(e)); // 409: a message arrived from another window
      reloadChat(id);
    } finally {
      me.modeBusy = false;
      if (s === me) renderComposer(); // also when another chat opened meanwhile: Send is free again
    }
  }
  renderHead();
  renderThread(false);
  renderComposer();
  const btn = s.els.msgs.querySelector('.mode-pick [data-mode="' + mode + '"]');
  if (btn) btn.focus();
  announce(MODE_INFO[mode].label + ' mode.');
}

// ---------- open and reload ----------

async function openChat(id) {
  detachLive();
  closeTools();
  const seq = ++s.openSeq;
  memo.activeId = id;
  s.ctx.router.replace('chat', id ? {id} : null);
  s.confirmFor = s.confirmEl = null;
  showNote('');
  if (!id) {
    s.chat = null;
    s.loading = false;
    renderAll();
    return;
  }
  if (!s.chat || s.chat.id !== id) {
    s.chat = null;
    s.loading = true;
    renderAll();
  }
  const me = s;
  let chat;
  try {
    chat = await me.api.chat(id);
  } catch (e) {
    if (s !== me || seq !== me.openSeq) return;
    s.loading = false;
    if (e.status === 404) {
      toast('That chat no longer exists.');
      openChat(null);
      loadList().catch(() => {});
      return;
    }
    renderThread(true);
    showNote(errText(e));
    return;
  }
  if (s !== me || seq !== me.openSeq) return;
  s.loading = false;
  s.chat = chat;
  renderAll();
  if (chat.running) resume(chat.id);
}

// The saved chat after an answer ended; resumes once more if it still runs (the stream broke).
async function reloadChat(id, resumeOk = true) {
  const me = s;
  if (!me) return;
  if (memo.activeId === id) {
    const seq = me.openSeq;
    try {
      const chat = await me.api.chat(id);
      if (s !== me || seq !== me.openSeq) return;
      s.chat = chat;
      renderAll();
      if (chat.running && resumeOk && !s.live) resume(id);
    } catch (e) {
      if (s === me && seq === me.openSeq) showNote(errText(e));
    }
  }
  if (s === me) loadList().catch(() => {});
}

// ---------- send, regenerate, stop, resume ----------

async function send() {
  const els = s.els;
  if (s.live || s.sending || s.loading || s.modeBusy || modelFacts().blocked) return;
  const text = els.input.value.trim();
  const files = memo.attach.slice();
  if (!text && !files.length) {
    showNote('Write a message or attach an image first.');
    els.input.focus();
    return;
  }
  showNote('');
  const me = s;
  let id = memo.activeId;
  if (!id || !s.chat) {
    me.sending = true;
    renderComposer();
    try {
      id = await createChatNow();
    } catch (e) {
      if (s !== me) return;
      showNote(errText(e));
      return;
    } finally {
      me.sending = false;
    }
    if (s !== me) return;
  }
  const fd = new FormData();
  fd.append('text', text);
  fd.append('effort', effortLevel());
  for (const a of files) fd.append('images', a.file, a.name);
  const local = files.map((a) => ({url: a.url, name: a.name}));
  s.chat.messages.push({role: 'user', content: text, images: [], local, time: new Date().toISOString()});
  if (s.chat.title === 'New chat' && text) {
    s.chat.title = text.split('\n')[0].slice(0, 60);
    const row = s.chats.find((c) => c.id === id);
    if (row) row.title = s.chat.title;
  }
  els.input.value = memo.draft = '';
  grow(els.input);
  memo.attach = [];
  renderAttach();
  const res = await runStream(id, (onEvent, signal) => me.api.sendMessage(id, fd, onEvent, signal));
  if (res.rejected && s === me && memo.activeId === id) {
    // Not saved by the server: give the text and the images back.
    if (!els.input.value) {
      els.input.value = memo.draft = text;
      grow(els.input);
    }
    if (!memo.attach.length) memo.attach = files;
    renderAttach();
    showNote(errText(res.error));
  }
  if (!res.aborted) await reloadChat(id); // resumes when the server said 409: this chat answers elsewhere
  // The saved chat shows the server's copies now; free the local previews that are not back in the composer.
  if (!memo.attach.includes(files[0])) files.forEach((a) => URL.revokeObjectURL(a.url));
}

// Creates the chat that the page shows as new (on the first message, or when the Tools menu opens) and returns its id.
async function createChatNow() {
  const me = s;
  const c = await me.api.createChat(nextMode());
  if (memo.efforts[''] !== undefined) { // the level picked before the chat existed is now this chat's
    memo.efforts[c.id] = memo.efforts[''];
    delete memo.efforts[''];
  }
  if (s !== me) return c.id;
  memo.activeId = c.id;
  s.openSeq++;
  s.ctx.router.replace('chat', {id: c.id});
  s.chats = [c, ...s.chats.filter((x) => x.id !== c.id)];
  s.chat = Object.assign({}, c, {messages: []}); // the summary's `messages` is a count
  renderList();
  renderHead();
  return c.id;
}

async function regenerate() {
  if (!s.chat || s.live) return;
  const me = s;
  const id = s.chat.id;
  const msgs = s.chat.messages;
  // the answer: its assistant messages and an agent's tool messages, as the server drops them
  while (msgs.length && ['assistant', 'tool'].includes(msgs[msgs.length - 1].role)) msgs.pop();
  showNote('');
  s.els.input.focus();
  const fd = new FormData();
  fd.append('regenerate', 'true');
  fd.append('effort', effortLevel());
  const res = await runStream(id, (onEvent, signal) => me.api.sendMessage(id, fd, onEvent, signal));
  if (res.aborted) return;
  if (res.rejected && s === me) showNote(errText(res.error));
  reloadChat(id);
}

async function resume(id) {
  const me = s;
  const res = await runStream(id, (onEvent, signal) => me.api.resumeStream(id, onEvent, signal));
  if (res.aborted) return;
  // A 404 means it ended between the two requests: the saved chat has the answer.
  reloadChat(id, false);
}

async function stop() {
  const live = s.live;
  if (!live || live.stopping) return;
  live.stopping = true;
  renderComposer();
  paintLive(live);
  try {
    await s.api.stopChat(live.chatId);
  } catch (e) {
    live.stopping = false;
    if (s && s.live === live) {
      renderComposer();
      paintLive(live);
      showNote(errText(e));
    }
  }
}

// Runs one event stream into a live answer under the messages. Resolves {aborted} when the view left or another chat
// opened (the answer goes on in the server), {rejected, error} when the server refused before streaming, else {msg}.
async function runStream(chatId, start) {
  detachLive();
  const me = s;
  const live = newLive(chatId);
  s.live = live;
  renderList();
  renderHead();
  renderThread(true);
  renderComposer();
  announce(modelName() + ' is answering.');
  let error = null;
  try {
    await start((ev) => onEvent(live, ev), live.ctrl.signal);
  } catch (e) {
    error = e;
  }
  live.release();
  if (s !== me || s.live !== live || (error && error.name === 'AbortError')) return {aborted: true};
  cancelAnimationFrame(live.frame);
  s.live = null;
  const focusBack = document.activeElement === s.els.send || document.activeElement === document.body;
  renderComposer();
  renderHead();
  if (focusBack) s.els.input.focus();
  const m = live.msg;
  if (error && error.name === 'ApiError') return {rejected: true, error};
  if (error) announce('The connection to the answer broke.');
  else if (m.finish === 'error') announce('The answer failed: ' + (m.error || ''));
  else if (m.finish === 'cancelled') announce('Stopped.');
  else announce('Answer finished.');
  return {msg: m};
}

// Leaves the live answer (another chat opens, or the view unmounts); it goes on in the server.
function detachLive() {
  if (!s || !s.live) return;
  const live = s.live;
  s.live = null;
  cancelAnimationFrame(live.frame);
  live.ctrl.abort();
  live.release();
  const row = s.chats.find((c) => c.id === live.chatId);
  if (row) row.running = true;
}

function onEvent(live, ev) {
  const m = live.msg;
  if (live.agent && onAgentEvent(live, ev)) {
    if (!live.frame) live.frame = requestAnimationFrame(() => paintLive(live));
    return;
  }
  if (ev.event === 'waiting') {
    live.waiting = ev.message || 'Waiting for memory.';
    announce('Waiting for memory. The answer starts when the media job ends.');
    if (s && s.live === live) renderHead();
  } else if (ev.event === 'reasoning') {
    live.waiting = null;
    m.reasoning += ev.text || '';
  } else if (ev.event === 'content') {
    live.waiting = null;
    m.content += ev.text || '';
  } else if (ev.event === 'done') {
    live.waiting = null;
    Object.assign(m, {content: ev.content || '', reasoning: ev.reasoning || '', finish: ev.finish, stats: ev.stats});
  } else if (ev.event === 'error') {
    live.waiting = null;
    Object.assign(m, {finish: 'error', error: ev.message});
  }
  if (!live.frame) live.frame = requestAnimationFrame(() => paintLive(live));
}

// ---------- the live answer ----------

function newLive(chatId) {
  const live = {
    chatId, ctrl: new AbortController(), release: store.fast('llm'), msg: {role: 'assistant', content: '', reasoning: ''},
    waiting: null, stopping: false, thinkTouched: false, frame: 0, agent: chatMode() === 'agent',
  };
  if (live.agent) {
    Object.assign(live, {segs: [], step: 0, maxSteps: 0, last: false, plan: null, asking: null, focusAsk: false});
    live.flow = h('div', {class: 'agent-flow'});
    live.state = h('div', {class: 'msg-wait'});
    live.node = h('article', {class: 'msg msg-ai is-live', 'aria-label': 'Answer from ' + modelName()},
      h('span', {class: 'who'}, modelName()), live.flow, live.state);
    return live;
  }
  live.thinkLabel = h('span', {class: 'think-label'});
  live.thinkBody = h('div', {class: 'think-body'});
  live.think = h('details', {class: 'think', hidden: true},
    h('summary', {onclick: () => (live.thinkTouched = true)}, icon('chev', 'chev ic-sm'), live.thinkLabel), live.thinkBody);
  live.bubble = h('div', {class: 'bubble md', 'aria-live': 'polite', 'aria-busy': 'true', hidden: true});
  live.state = h('div', {class: 'msg-wait'});
  live.node = h('article', {class: 'msg msg-ai is-live', 'aria-label': 'Answer from ' + modelName()},
    h('span', {class: 'who'}, modelName()), live.think, live.bubble, live.state);
  return live;
}

function paintLive(live) {
  live.frame = 0;
  if (!s || s.live !== live) return;
  if (live.agent) {
    paintAgent(live);
    return;
  }
  const stick = nearBottom();
  const m = live.msg;
  if (m.reasoning) {
    const thinking = !m.content && !m.finish;
    live.think.hidden = false;
    live.thinkLabel.replaceChildren(...(thinking ? [h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Thinking'] : ['Thinking']));
    live.thinkBody.textContent = m.reasoning;
    live.thinkBody.classList.toggle('caret', thinking);
    if (!live.thinkTouched) live.think.open = thinking;
  }
  live.bubble.replaceChildren(renderMarkdown(m.content, true, showCards(), showHosts()));
  live.bubble.hidden = !m.content;
  if (m.content && !m.finish) addCaret(live.bubble);
  live.state.replaceChildren(...liveStatus(live));
  if (stick) scrollToEnd();
}

function liveStatus(live) {
  const m = live.msg;
  const llm = store.get('llm') || {};
  const spin = () => h('span', {class: 'spinner sm', 'aria-hidden': 'true'});
  if (live.stopping) return [spin(), 'Stopping'];
  if (live.agent && live.asking) {
    return [icon('lock', 'ic-sm'), h('span', null, stepChip(live), h('b', null, 'Waiting for your answer above. '), 'Stop also works.')];
  }
  if (live.agent && !live.waiting && !m.finish && live.step) return agentStatus(live, spin);
  if (live.waiting) {
    const job = store.runningJob();
    const left = job && job.progress ? fmtLeft(job.progress.eta_s) : '';
    return [spin(), h('span', null, h('b', null, 'Waiting for memory. '),
      job ? (job.tool_label || job.tool) + ' needs it now' + (left ? ' (' + left + ')' : '') + '. The answer starts when it ends, before the next queued job.' : live.waiting,
      ' Stop works while it waits.')];
  }
  if (m.content || m.reasoning || m.finish) return [];
  if (llm.state === 'loading' || llm.state === 'off') {
    return [spin(), 'Loading ' + modelName() + ' into memory (about ' + Math.round(llm.peak_gb || 18) + ' GB). The first answer takes longer.'];
  }
  return [spin(), 'Reading your message'];
}

// ---------- agent answers ----------

// The running agent answer's line under the steps: "Step 3 of 12" and what it does now.
function agentStatus(live, spin) {
  const last = live.segs[live.segs.length - 1];
  let now;
  if (last && last.kind === 'tool' && !last.result) now = 'Running ' + (last.call.label || last.call.name);
  else if (last && last.kind === 'text' && last.msg.content) now = live.last ? 'Writing the final answer' : 'Writing';
  else if (last && last.kind === 'text' && last.msg.reasoning) now = 'Thinking';
  else if (live.step > 1) now = 'Reading the tool results';
  else {
    const llm = store.get('llm') || {};
    now = llm.state === 'loading' || llm.state === 'off'
      ? 'Loading ' + modelName() + ' into memory (about ' + Math.round(llm.peak_gb || 18) + ' GB). The first answer takes longer.'
      : 'Reading your message';
  }
  return [spin(), h('span', null, stepChip(live), now)];
}

function stepChip(live) {
  if (!live.step) return null;
  return h('span', {class: 'step-n num'}, 'Step ' + live.step + ' of ' + live.maxSteps + (live.last ? ', no more tools' : ''));
}

// The plan of an answer as a compact checklist: done, current and open steps.
const PLAN_MARK = {done: 'Done', in_progress: 'Now', pending: 'Open'};
function planBlock(items) {
  const done = items.filter((i) => i.status === 'done').length;
  return h('section', {class: 'agent-plan', 'aria-label': 'Plan, ' + done + ' of ' + items.length + ' steps done'},
    h('div', {class: 'agent-plan-head'}, h('b', null, 'Plan'), h('span', {class: 'num'}, done + ' of ' + items.length + ' done')),
    h('ol', {class: 'plan-list'}, items.map((i) => h('li', {class: 'plan-item is-' + i.status},
      h('span', {class: 'plan-mark', title: PLAN_MARK[i.status] || i.status}, i.status === 'done' ? icon('check', 'ic-sm') : null),
      h('span', {class: 'sr-only'}, (PLAN_MARK[i.status] || i.status) + ': '),
      h('span', {class: 'plan-text'}, visible(i.step))))));
}

// A plan call is shown in the checklist; only a failed one (an error the model reads) keeps its card.
function planCallHidden(name, status) {
  return name === PLAN_TOOL && (!status || status === 'ok');
}

// The agent events (docs/API.md): step, tool_call, approval, approval_done, tool_result, and the text deltas of the
// current step. Returns false for the events the plain path handles (waiting, done, error).
function onAgentEvent(live, ev) {
  const textSeg = () => {
    const last = live.segs[live.segs.length - 1];
    if (last && last.kind === 'text') return last;
    return addSeg(live, newTextSeg());
  };
  const toolSeg = (id) => live.segs.find((x) => x.kind === 'tool' && x.call.id === id);
  if (ev.event === 'step') {
    live.waiting = null;
    Object.assign(live, {step: ev.n, maxSteps: ev.max, last: !!ev.last});
    live.segs.filter((x) => x.kind === 'text').forEach((x) => (x.dirty = true)); // drop the caret of the step before
    addSeg(live, newTextSeg());
  } else if (ev.event === 'reasoning' || ev.event === 'content') {
    live.waiting = null;
    const seg = textSeg();
    seg.msg[ev.event] += ev.text || '';
    seg.dirty = true;
  } else if (ev.event === 'tool_call') {
    addSeg(live, {kind: 'tool', call: ev, result: null, approval: null, decision: null, dirty: true, open: false, node: h('div')});
  } else if (ev.event === 'approval') {
    const seg = toolSeg(ev.id);
    if (!seg) return true;
    Object.assign(seg, {approval: ev, decision: null, dirty: true});
    live.asking = seg;
    live.focusAsk = true;
    announce('Approval needed: ' + (ev.label || ev.name) + '. ' + (ev.describe || ''));
  } else if (ev.event === 'approval_done') {
    const seg = toolSeg(ev.id);
    if (seg) Object.assign(seg, {decision: ev.decision, answering: null, dirty: true});
    if (live.asking === seg) live.asking = null;
  } else if (ev.event === 'tool_result') {
    const seg = toolSeg(ev.id);
    if (seg) Object.assign(seg, {result: ev, dirty: true});
  } else if (ev.event === 'plan') {
    const plan = planBlock(ev.items || []);
    if (live.plan) live.plan.replaceWith(plan);
    else live.flow.before(plan);
    live.plan = plan;
  } else if (ev.event === 'done') {
    live.waiting = null;
    Object.assign(live.msg, {finish: ev.finish, stats: ev.stats, note: ev.note, content: ev.content || ''});
    const last = live.segs[live.segs.length - 1];
    if (last && last.kind === 'text') {
      if (ev.content) last.msg.content = ev.content;
      last.dirty = true;
    }
    live.asking = null;
  } else {
    return false;
  }
  return true;
}

function newTextSeg() {
  const seg = {kind: 'text', msg: {content: '', reasoning: ''}, dirty: true, touched: false};
  seg.thinkLabel = h('span', {class: 'think-label'});
  seg.thinkBody = h('div', {class: 'think-body'});
  seg.think = h('details', {class: 'think', hidden: true},
    h('summary', {onclick: () => (seg.touched = true)}, icon('chev', 'chev ic-sm'), seg.thinkLabel), seg.thinkBody);
  seg.bubble = h('div', {class: 'bubble md', hidden: true});
  seg.node = h('div', {class: 'agent-text'}, seg.think, seg.bubble);
  return seg;
}

function addSeg(live, seg) {
  live.segs.push(seg);
  live.flow.append(seg.node);
  return seg;
}

function paintAgent(live) {
  const stick = nearBottom();
  const m = live.msg;
  const segs = live.segs;
  segs.forEach((seg, i) => {
    if (!seg.dirty) return;
    seg.dirty = false;
    if (seg.kind === 'text') {
      const active = i === segs.length - 1 && !m.finish;
      const t = seg.msg;
      if (t.reasoning) {
        const thinking = active && !t.content;
        seg.think.hidden = false;
        seg.thinkLabel.replaceChildren(...(thinking ? [h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Thinking'] : ['Thinking']));
        seg.thinkBody.textContent = t.reasoning;
        seg.thinkBody.classList.toggle('caret', thinking);
        if (!seg.touched) seg.think.open = thinking;
      }
      seg.bubble.replaceChildren(renderMarkdown(t.content, active, showCards(), showHosts()));
      seg.bubble.hidden = !t.content;
      if (t.content && active) addCaret(seg.bubble);
      seg.node.hidden = !t.content && !t.reasoning;
      return;
    }
    const hadFocus = seg.node.contains(document.activeElement);
    const node = toolSegNode(seg, live);
    seg.node.replaceWith(node);
    seg.node = node;
    if (hadFocus) (node.querySelector('summary, button') || s.els.input).focus();
  });
  live.node.querySelector('.agent-end')?.remove();
  if (m.note) live.node.insertBefore(h('p', {class: 'msg-end agent-end'}, icon('alert', 'ic-sm'), m.note), live.state);
  live.state.replaceChildren(...liveStatus(live));
  if (live.focusAsk && live.asking) {
    live.focusAsk = false;
    const card = live.asking.node.querySelector('.approval');
    if (card) {
      closeTools(); // the open Tools menu would cover the card
      // the focus moves to the card itself, not to a button: a Return meant for the message box approves nothing
      const typing = document.activeElement === s.els.input && s.els.input.value.trim();
      if (!typing) card.focus({preventScroll: true});
      card.scrollIntoView({block: 'nearest'});
      return;
    }
  }
  if (stick) scrollToEnd();
}

// A live tool call: the approval card while it waits for the user, else its card.
function toolSegNode(seg, live) {
  if (seg.approval && !seg.decision) return approvalCard(seg, live);
  const r = seg.result || {};
  if (planCallHidden(seg.call.name, r.status)) return h('div', {hidden: true});
  const c = seg.call;
  return toolBlock({
    id: c.id, name: c.name, label: c.label, arguments: c.arguments, describe: c.describe, risk: c.risk,
    status: r.status || (seg.decision && seg.decision !== 'once' && seg.decision !== 'chat' ? 'denied' : null),
    content: r.content, truncated: r.truncated, files: r.files, seconds: r.seconds,
    approval: r.approval || seg.decision, job: r.job,
  }, seg);
}

// Characters that hide or reorder text (app/agent/registry.py HIDDEN): C0 and C1 controls, zero-width characters and
// marks, bidi embeddings, overrides and isolates, and the byte order mark. The arguments show them as \uXXXX, so the
// user approves what the call really sends. keepLines leaves line breaks and tabs (of code and of the JSON layout;
// JSON.stringify writes those inside strings as \n and \t).
const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufe00-\ufe0f\ufeff\u{e0000}-\u{e007f}]/gu;

function visible(text, keepLines) {
  return String(text).replace(HIDDEN, (c) => (keepLines && (c === '\n' || c === '\t') ? c
    : '\\u' + c.codePointAt(0).toString(16).padStart(4, '0')));
}

function argsLine(args) {
  const parts = Object.entries(args || {}).map(([k, v]) => k + ': ' + (typeof v === 'string' ? v : JSON.stringify(v)));
  const text = visible(parts.join(', ').replace(/[ \n\r\t]+/g, ' '));
  return text.length > 140 ? text.slice(0, 139) + '…' : text;
}

// The arguments as the user should read them: run_python's code and run_shell's command as code (the other
// arguments on a line below), anything else as JSON.
const CODE_ARGS = {run_python: ['code', 'Code'], run_shell: ['command', 'Command']};

function argsBlock(t) {
  const args = t.arguments || {};
  if (!Object.keys(args).length) return null;
  const [key, title] = CODE_ARGS[t.name] || [];
  const code = key && typeof args[key] === 'string';
  const rest = code ? Object.fromEntries(Object.entries(args).filter(([k]) => k !== key)) : {};
  return h('div', {class: 'tool-sec'}, h('span', {class: 'eyebrow'}, code ? title : 'Arguments'),
    h('pre', {class: 'tool-pre', tabindex: '0'}, h('code', null, visible(code ? args[key] : JSON.stringify(args, null, 2), true))),
    Object.keys(rest).length ? h('p', {class: 'tool-note'}, argsLine(rest)) : null);
}

// run_shell's folders on its approval card: where the command runs and what it may read.
function shellFolders(ap) {
  if (!ap.cwd) return null;
  return [
    h('p', {class: 'approval-host'}, 'Runs in: ', h('code', null, visible(ap.cwd))),
    h('p', {class: 'approval-host'}, 'Can read: ',
      ap.read_only ? [h('code', null, visible(ap.read_only)), ' (read-only)'] : 'no granted folder'),
  ];
}

// run_shell's result (app/agent/shell_tool.py format_result): the exit code line, then stdout and stderr, each
// headed by its length in characters (code points), then notes (the file names). After the first note that follows
// a part, nothing counts as a part any more, so a file name cannot fake one. A long part is folded. null when the
// text has another form.
function shellResult(t) {
  const text = String(t.content).replace(/\n\n\[Truncated: showing the first [\d,]+ of [\d,]+ characters\.\]$/, '');
  const nl = text.indexOf('\n');
  const m = /^exit code: (-?\d+) \(([\d.]+) s\)$/.exec(nl < 0 ? text : text.slice(0, nl));
  if (!m) return null;
  let rest = nl < 0 ? '' : text.slice(nl + 1);
  const notes = [];
  const parts = [];
  let tail = false;
  while (rest) {
    const p = tail ? null : /^(stdout|stderr) \((\d+) characters(, cut at [\d,]+)?\):\n/.exec(rest);
    if (!p) {
      const end = rest.indexOf('\n');
      notes.push(end < 0 ? rest : rest.slice(0, end));
      rest = end < 0 ? '' : rest.slice(end + 1);
      tail = tail || parts.length > 0 || notes[notes.length - 1] === 'no output';
      continue;
    }
    rest = rest.slice(p[0].length);
    const body = Array.from(rest).slice(0, Number(p[2])).join('');
    rest = rest.slice(body.length).replace(/^\n/, '');
    parts.push({name: p[1] + (p[3] ? ' (cut)' : ''), body});
  }
  return h('div', {class: 'tool-sec'},
    h('span', {class: 'eyebrow'}, 'Exit code ' + m[1] + ' · ' + m[2] + ' s'),
    notes.filter(Boolean).map((n) => h('p', {class: 'tool-note'}, n)),
    parts.map((p) => {
      const lines = p.body.split('\n').length;
      const pre = h('pre', {class: 'tool-pre tool-out', tabindex: '0'}, p.body);
      if (lines <= 12 && p.body.length <= 1200) return h('div', {class: 'tool-sec'}, h('span', {class: 'eyebrow'}, p.name), pre);
      return h('details', {class: 'tool-fold'}, h('summary', null, p.name + ' · ' + lines + ' lines'), pre);
    }),
    t.truncated ? h('p', {class: 'tool-note'}, 'The start of the result. The saved chat has the rest.') : null);
}

function filesBlock(t) {
  const saved = (t.files || []).filter((f) => f.kind === 'saved' && typeof f.path === 'string');
  const files = (t.files || []).filter((f) => s.api.safeFileUrl(f.url));
  if (!files.length && !saved.length) return null;
  const images = files.filter((f) => f.kind === 'image');
  const items = images.map((f) => ({url: f.url, name: f.path}));
  return h('div', {class: 'tool-files'},
    images.map((f, k) => h('button', {
      type: 'button', class: 'att', 'aria-label': 'Enlarge ' + f.path, onclick: () => lightbox(items, k),
    }, h('img', {src: f.url, alt: f.path, loading: 'lazy'}))),
    files.filter((f) => f.kind !== 'image').map((f) => h('a', {class: 'file-link', href: f.url, download: f.path.split('/').pop()},
      icon('file', 'ic-sm'), f.path)),
    saved.map((f) => savedFile(f.path)));
}

// A file a tool wrote in the granted folder: its path, and a button that shows it in Finder (POST .../reveal).
function savedFile(path) {
  const chatId = memo.activeId;
  const err = h('span', {class: 'field-err', role: 'alert', hidden: true});
  const reveal = async () => {
    err.hidden = true;
    try {
      await s.api.request('/api/chats/' + encodeURIComponent(chatId) + '/reveal', {method: 'POST',
        headers: {'content-type': 'application/json'}, body: JSON.stringify({path})});
    } catch (e) {
      if (!err.isConnected) return;
      err.textContent = 'Show in Finder failed: ' + errText(e);
      err.hidden = false;
    }
  };
  return h('span', {class: 'saved-file'}, icon('file', 'ic-sm'), h('code', null, visible(path)),
    chatId ? h('button', {type: 'button', class: 'btn btn-ghost btn-sm', onclick: reveal, 'aria-label': 'Show ' + path + ' in Finder'},
      icon('reveal', 'ic-sm'), 'Show in Finder') : null, err);
}

// The change a call makes (edit_file, write_file), as the server's unified diff: each line is text in its own span,
// colored by its first character. The server escapes hiding characters (registry.visible_lines).
function diffBlock(text) {
  const lines = String(text).split('\n').map((line) => {
    const cls = line.startsWith('@@') ? 'd-hunk' : line.startsWith('+++') || line.startsWith('---') ? 'd-file'
      : line.startsWith('+') ? 'd-add' : line.startsWith('-') ? 'd-del' : line.startsWith('[') ? 'd-note' : null;
    return h('span', {class: cls ? 'd-line ' + cls : 'd-line'}, line);
  });
  return h('div', {class: 'tool-sec'}, h('span', {class: 'eyebrow'}, 'Change'),
    h('pre', {class: 'tool-pre tool-diff', tabindex: '0', 'aria-label': 'The change as a diff'}, h('code', null, lines)));
}

// One tool call as a compact card: name, what it did, status and time; the arguments and the result inside. Files it
// wrote show under the card. seg (live) keeps the open state across repaints.
function toolBlock(t, seg) {
  const st = t.status ? STATUS[t.status] || ['idle', t.status] : ['run', 'Running'];
  const sum = t.describe ? visible(t.describe) : argsLine(t.arguments);
  const risk = t.risk === null || t.risk === undefined ? null : {...RISK[t.risk], label: riskLabel(t.name, t.risk)};
  const shell = t.name === 'run_shell' && t.status === 'ok' && t.content ? shellResult(t) : null;
  const body = h('div', {class: 'tool-body'},
    t.approval && DECISION[t.approval] ? h('p', {class: 'tool-note'}, icon('lock', 'ic-sm'), DECISION[t.approval]) : null,
    argsBlock(t),
    shell || (t.content ? h('div', {class: 'tool-sec'}, h('span', {class: 'eyebrow'}, 'Result'),
      h('pre', {class: 'tool-pre tool-out', tabindex: '0'}, t.content),
      t.truncated ? h('p', {class: 'tool-note'}, 'The start of the result. The saved chat has the rest.') : null) : null),
    !t.status ? h('p', {class: 'tool-note'}, h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Running') : null);
  const card = h('details', {class: 'tool-card is-' + (t.status || 'running'), open: !!(seg && seg.open)},
    h('summary', null, icon('chev', 'chev ic-sm'),
      h('span', {class: 'tool-name'}, t.label || t.name || 'Unknown tool'),
      sum ? h('span', {class: 'tool-sum', title: sum}, sum) : null,
      h('span', {class: 'tool-end'},
        risk && t.risk >= 2 ? h('span', {class: 'tool-risk'}, risk.label) : null,
        h('span', {class: 'pill pill-' + st[0]}, st[1]),
        typeof t.seconds === 'number' ? h('span', {class: 'tool-time num'}, t.seconds.toFixed(1) + ' s') : null)),
    body);
  if (seg) card.addEventListener('toggle', () => (seg.open = card.open));
  return h('div', {class: 'tool'}, card, filesBlock(t), jobBlock(t));
}

// The media job a call queued (create_job, transcribe_audio): its live state from the shared job list, a link to the
// Jobs view, and its outputs when it is done. paintJob runs again on each job list update.
const JOB_STATUS = {
  queued: ['idle', 'Queued'], running: ['run', 'Running'], done: ['ok', 'Done'], failed: ['err', 'Failed'],
  cancelled: ['idle', 'Cancelled'],
};
const jobsAsked = new Set(); // jobs not in the list yet: the list is fetched once for each

function jobBlock(t) {
  if (!t.job) return null;
  const el = h('div', {class: 'chat-job', 'data-job': t.job});
  paintJob(el);
  return el;
}

function paintJob(el) {
  const id = el.getAttribute('data-job');
  const job = (store.get('jobs') || []).find((j) => j.id === id);
  if (!job && !jobsAsked.has(id)) {
    jobsAsked.add(id);
    store.refresh('jobs');
  }
  // rebuilt only when the job changes, so a playing output keeps playing
  const sig = job ? JSON.stringify([job.status, job.position, job.progress, job.outputs, job.error]) : '';
  if (el.dataset.sig === sig && el.firstChild) return;
  el.dataset.sig = sig;
  const st = job ? JOB_STATUS[job.status] || ['idle', job.status] : null;
  const p = job && job.status === 'running' && job.progress ? job.progress : null;
  const facts = [
    job && job.status === 'queued' && job.position ? 'place ' + job.position + ' in the queue' : '',
    p && typeof p.fraction === 'number' ? Math.round(p.fraction * 100) + '%' : '',
    p && p.eta_s !== null && p.eta_s !== undefined ? fmtLeft(p.eta_s) : '',
  ].filter(Boolean).join(' · ');
  const outputs = job && job.status === 'done' ? (job.outputs || []).map((o) => media(o, {actions: downloadLink(o)})).filter(Boolean) : [];
  el.replaceChildren(...[
    h('div', {class: 'chat-job-head'},
      icon('jobs', 'ic-sm'),
      h('span', {class: 'chat-job-name'}, job ? job.tool_label || job.tool : 'Job ' + id),
      st ? h('span', {class: 'pill pill-' + st[0]}, st[1]) : null,
      facts ? h('span', {class: 'chat-job-facts num'}, facts) : null,
      h('a', {class: 'chat-job-open', href: '#/jobs'}, 'Open in Jobs')),
    job && job.error && job.status !== 'done' ? h('p', {class: 'chat-job-err'}, job.error) : null,
    outputs.length ? h('div', {class: 'chat-job-outputs'}, outputs) : null,
  ].filter(Boolean));
}

// The question for a call that needs the user's approval. Keyboard: the focus moves to the card, Tab reaches the
// buttons, Escape denies.
function approvalCard(seg, live) {
  const ap = seg.approval;
  const label = ap.label || ap.name;
  const risk = {...(RISK[ap.risk] || RISK[3]), label: riskLabel(ap.name, ap.risk, ap.network)};
  const id = 'ap-' + ap.id;
  const err = h('p', {class: 'field-err', role: 'alert', hidden: true});
  const buttons = [];
  const answer = async (decision) => {
    if (seg.answering) return;
    seg.answering = decision;
    buttons.forEach((b) => (b.disabled = true));
    err.hidden = true;
    try {
      await s.api.approve(live.chatId, ap.id, decision);
      announce(decision === 'deny' ? 'Denied.' : 'Allowed.');
    } catch (e) {
      seg.answering = null;
      buttons.forEach((b) => (b.disabled = false));
      err.textContent = errText(e);
      err.hidden = false;
    }
  };
  buttons.push(h('button', {type: 'button', class: 'btn btn-primary btn-sm', onclick: () => answer('once')},
    icon('check', 'ic-sm'), 'Allow once'));
  if (ap.chat_scope) {
    buttons.push(h('button', {type: 'button', class: 'btn btn-sm', onclick: () => answer('chat'),
      title: 'Runs this tool without asking for the rest of this chat, until web pages or files are in it. Revoke it in the Tools menu.'},
    'Allow in this chat'));
  }
  buttons.push(h('button', {type: 'button', class: 'btn btn-sm btn-danger', onclick: () => answer('deny')},
    icon('x', 'ic-sm'), 'Deny'));
  if (seg.answering) buttons.forEach((b) => (b.disabled = true));
  const minutes = Math.max(1, Math.round((ap.timeout_s || 600) / 60));
  const card = h('section', {
    class: 'approval risk-' + ap.risk, role: 'group', tabindex: '-1', 'aria-labelledby': id + '-t', 'aria-describedby': id + '-d',
    onkeydown: (e) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      answer('deny');
    },
  },
  h('div', {class: 'approval-head'}, icon('lock'), h('b', {id: id + '-t'}, 'Allow ' + label + '?'),
    h('span', {class: 'pill pill-' + risk.pill}, risk.label)),
  h('p', {class: 'approval-what', id: id + '-d'}, ap.describe ? visible(ap.describe) : argsLine(ap.arguments)),
  ap.host ? h('p', {class: 'approval-host'}, 'Host: ', h('code', null, ap.host)) : null,
  ap.reason ? h('p', {class: 'approval-why'}, ap.reason) : null,
  ap.preview ? diffBlock(ap.preview) : null,
  ap.preview ? h('details', {class: 'approval-args'}, h('summary', null, 'Arguments'), argsBlock(ap)) : argsBlock(ap),
  shellFolders(ap),
  h('div', {class: 'approval-actions'}, buttons),
  err,
  h('p', {class: 'approval-help'}, 'No answer in ' + minutes + (minutes === 1 ? ' minute' : ' minutes') + ' denies it. Escape denies.'));
  return h('div', {class: 'tool'}, card);
}

// A saved agent answer: every step's text and tool cards, then the end note, the actions and the stats.
function agentTurn(run, isLast) {
  const results = new Map(run.filter((m) => m.role === 'tool').map((m) => [m.tool_call_id, m]));
  const steps = run.filter((m) => m.role === 'assistant');
  const final = steps[steps.length - 1] || {};
  const plans = run.filter((m) => m.role === 'tool' && Array.isArray(m.plan));
  const body = [];
  for (const m of steps) {
    if (m.reasoning) body.push(thinkBlock(m.reasoning));
    if (m.content) body.push(h('div', {class: 'bubble md'}, renderMarkdown(m.content, false, showCards(), showHosts())));
    for (const c of m.tool_calls || []) {
      const r = results.get(c.id) || {};
      if (planCallHidden(c.name, r.status || 'skipped')) continue;
      body.push(toolBlock({
        id: c.id, name: c.name, label: r.label || c.name, arguments: c.arguments, describe: r.describe, risk: r.risk,
        status: r.status || 'skipped', content: r.content, files: r.files, seconds: r.seconds, approval: r.approval,
        job: r.job,
      }));
    }
  }
  return h('article', {class: 'msg msg-ai', 'aria-label': 'Answer from ' + modelName()},
    h('span', {class: 'who', title: fmtTime(final.time)}, modelName()),
    plans.length ? planBlock(plans[plans.length - 1].plan) : null,
    h('div', {class: 'agent-flow'}, body),
    finishNote(final),
    msgTools(final, isLast));
}

// ---------- the Tools menu (agent chats) ----------

async function openTools() {
  if (s.tools || chatMode() !== 'agent') return;
  const me = s;
  let id = memo.activeId;
  me.tools = {state: null, busy: false, error: '', folderError: ''};
  me.els.toolsBtn.setAttribute('aria-expanded', 'true');
  me.els.toolsPop.hidden = false;
  renderTools();
  try {
    if (!id || !me.chat) id = await createChatNow(); // the menu changes the chat on the server
    if (s !== me || !me.tools) return;
    me.tools.state = await me.api.agentState(id);
  } catch (e) {
    if (s !== me || !me.tools) return;
    me.tools.error = errText(e);
  }
  if (s !== me || !me.tools) return;
  renderTools();
  const first = me.els.toolsPop.querySelector('input, button');
  if (first) first.focus();
  me.toolsOutside = (e) => {
    if (!me.els.toolsPop.contains(e.target) && !me.els.toolsBtn.contains(e.target)) closeTools();
  };
  document.addEventListener('mousedown', me.toolsOutside, true);
}

function closeTools(focusBack) {
  if (!s || !s.tools) return;
  s.tools = null;
  s.els.toolsPop.hidden = true;
  s.els.toolsPop.replaceChildren();
  s.els.toolsBtn.setAttribute('aria-expanded', 'false');
  if (s.toolsOutside) document.removeEventListener('mousedown', s.toolsOutside, true);
  s.toolsOutside = null;
  if (focusBack) s.els.toolsBtn.focus();
}

async function changeTools(body, focusKey) {
  const me = s;
  const t = me.tools;
  if (!t || t.busy || !memo.activeId) return;
  t.busy = true;
  t.error = '';
  if ('folder' in body) t.folderError = '';
  renderTools(focusKey);
  try {
    t.state = await me.api.changeAgent(memo.activeId, body);
    if (s === me && me.chat) me.chat.agent = {tools: t.state.enabled, folder: t.state.folder, grants: t.state.grants, tainted: t.state.tainted};
  } catch (e) {
    if ('folder' in body) t.folderError = errText(e);
    else t.error = errText(e);
  } finally {
    t.busy = false;
  }
  if (s === me && me.tools === t) renderTools(focusKey);
}

function renderTools(focusKey) {
  const t = s.tools;
  const pop = s.els.toolsPop;
  if (!t) return;
  const active = document.activeElement;
  const key = focusKey || (pop.contains(active) && active.dataset ? active.dataset.key : null);
  const st = t.state;
  const close = h('button', {type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Close the Tools menu', onclick: () => closeTools(true)}, icon('x', 'ic-sm'));
  const head = h('div', {class: 'tools-head'}, h('h3', null, 'Tools for this chat'), close);
  if (!st) {
    pop.replaceChildren(head, t.error ? h('p', {class: 'field-err', role: 'alert'}, t.error)
      : h('p', {class: 'muted'}, h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Loading...'));
    return;
  }
  const rows = st.tools.map((tool) => {
    const risk = RISK[tool.risk] || RISK[3];
    const label = riskLabel(tool.name, tool.risk, tool.network);
    const sw = h('input', {
      type: 'checkbox', role: 'switch', id: 'tool-' + tool.name, 'data-key': 'tool-' + tool.name, checked: tool.enabled, disabled: t.busy,
      'aria-describedby': 'tool-' + tool.name + '-help',
      onchange: () => {
        const on = st.tools.filter((x) => (x.name === tool.name ? sw.checked : x.enabled)).map((x) => x.name);
        changeTools({tools: on}, 'tool-' + tool.name);
      },
    });
    return h('li', {class: 'tool-row'},
      h('label', {class: 'switch', for: 'tool-' + tool.name}, sw, h('span', {class: 'track'}), h('span', {class: 'sr-only'}, tool.label)),
      h('div', {class: 'tool-row-text'},
        h('div', {class: 'tool-row-head'}, h('b', null, tool.label), h('span', {class: 'pill pill-' + risk.pill}, label)),
        h('p', {class: 'help', id: 'tool-' + tool.name + '-help'}, tool.risk_text),
        tool.granted ? h('div', {class: 'tool-grant'}, h('span', {class: 'chip'}, icon('check', 'ic-sm'), 'Allowed in this chat'),
          h('button', {type: 'button', class: 'btn btn-ghost btn-sm', 'data-key': 'revoke-' + tool.name, disabled: t.busy,
            onclick: () => changeTools({revoke: [tool.name]}, 'tool-' + tool.name)}, 'Revoke')) : null));
  });
  const input = h('input', {
    class: 'input', id: 'tools-folder', 'data-key': 'folder', type: 'text', placeholder: '/Users/you/Documents/notes',
    autocomplete: 'off', spellcheck: 'false', disabled: t.busy, 'aria-invalid': t.folderError ? 'true' : null,
    'aria-describedby': 'tools-folder-help' + (t.folderError ? ' tools-folder-err' : ''),
    onkeydown: (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        grant();
      }
    },
  });
  if (t.folderDraft) input.value = t.folderDraft;
  input.addEventListener('input', () => (t.folderDraft = input.value));
  const grant = () => {
    if (input.value.trim()) changeTools({folder: input.value.trim()}, 'folder');
  };
  const folder = st.folder
    ? h('div', {class: 'tools-folder-set'}, icon('folder', 'ic-sm'), h('code', {title: st.folder}, st.folder),
      h('button', {type: 'button', class: 'btn btn-sm', 'data-key': 'folder', disabled: t.busy, onclick: () => {
        t.folderDraft = '';
        changeTools({folder: null}, 'folder');
      }}, 'Revoke'))
    : h('div', {class: 'tools-folder-row'}, input,
      h('button', {type: 'button', class: 'btn btn-sm', disabled: t.busy, onclick: grant}, 'Grant'));
  pop.replaceChildren(...[head,
    h('p', {class: 'tools-intro'}, 'The model uses a tool only when it needs one. Calls marked "Leaves this Mac", "Uses this Mac", "Writes files", "Changes files" or "Runs code" ask you first.'),
    st.tainted ? h('p', {class: 'tools-taint'}, icon('info', 'ic-sm'),
      'Web pages, files or program output are in this chat, so tools that send data off this Mac ask before every call.') : null,
    t.error ? h('p', {class: 'field-err', role: 'alert'}, t.error) : null,
    h('ul', {class: 'tool-list', role: 'list'}, rows),
    h('div', {class: 'field tools-folder'},
      h('label', {class: 'label', for: 'tools-folder'}, 'Folder the file tools and Shell may use'),
      folder,
      t.folderError ? h('p', {class: 'field-err', id: 'tools-folder-err', role: 'alert'}, t.folderError) : null,
      h('p', {class: 'help', id: 'tools-folder-help'}, 'This folder and its subfolders only. The file tools read text files here, and write new files or change files when you allow it. Shell reads its files and never changes them. Hidden files and key files are never read or written. Not your home folder itself or a system folder.'))].filter(Boolean));
  if (key) {
    const el = pop.querySelector('[data-key="' + CSS.escape(key) + '"]');
    if (el && !el.disabled) el.focus();
    else if (el) t.refocus = key;
  } else if (t.refocus) {
    const el = pop.querySelector('[data-key="' + CSS.escape(t.refocus) + '"]');
    t.refocus = null;
    if (el) el.focus();
  }
}

function addCaret(bubble) {
  const leaves = bubble.querySelectorAll('p, li, h3, h4, h5, h6, pre code, .prompt-text, td');
  const last = leaves[leaves.length - 1] || bubble;
  last.append(h('span', {class: 'caret-mark', 'aria-hidden': 'true'}));
}

// ---------- messages ----------

function renderThread(toEnd) {
  const box = s.els.msgs;
  const stick = toEnd || nearBottom();
  box.replaceChildren();
  const msgs = s.chat ? s.chat.messages : [];
  box.classList.toggle('is-empty', !s.loading && !msgs.length && !s.live);
  if (s.loading) {
    box.append(h('p', {class: 'muted chat-loading'}, h('span', {class: 'spinner sm', 'aria-hidden': 'true'}), 'Opening the chat...'));
    return;
  }
  if (!msgs.length && !s.live) {
    box.append(emptyChat());
    return;
  }
  const last = msgs.length - 1;
  const idle = !s.live && !s.chat.running;
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (m.role === 'user') {
      box.append(userMessage(m));
      continue;
    }
    // an answer: the messages up to the next user message; an agent answer has steps and tool messages
    let j = i;
    while (j < last && msgs[j + 1].role !== 'user') j++;
    const run = msgs.slice(i, j + 1);
    if (run.some((x) => x.role === 'tool' || x.tool_calls)) box.append(agentTurn(run, j === last && idle));
    else run.forEach((x, k) => box.append(aiMessage(x, i + k === last && idle)));
    i = j;
  }
  if (s.live) {
    box.append(s.live.node);
    paintLive(s.live);
  }
  if (stick) scrollToEnd();
}

function emptyChat() {
  const blocked = modelFacts().blocked;
  const info = MODE_INFO[chatMode()] || MODE_INFO.workstation;
  return h('div', {class: 'chat-empty'},
    modePicker(),
    h('div', {class: 'empty'}, icon(info.icon),
      h('h3', null, info.title),
      h('p', null, info.text)),
    h('div', {class: 'examples', role: 'list', 'aria-label': 'Examples'}, info.examples.filter((ex) => !ex.full || s.ctx.store.profile() !== 'chat').map((ex) => h('button', {
      type: 'button', class: 'example', role: 'listitem', disabled: blocked, onclick: () => useExample(ex),
    }, h('b', null, ex.title), h('span', null, ex.text)))));
}

function useExample(ex) {
  const input = s.els.input;
  input.value = memo.draft = ex.text;
  grow(input);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  if (ex.attach && !memo.attach.length) s.els.file.click();
}

function userMessage(m) {
  const imgs = m.local || (m.images || []).map((name) => ({url: s.api.chatFileUrl(s.chat.id, name), name}));
  return h('article', {class: 'msg msg-user', 'aria-label': 'Your message'},
    h('span', {class: 'who', title: fmtTime(m.time)}, 'You'),
    imgs.length ? h('div', {class: 'msg-images'}, imgs.map((im, k) => h('button', {
      type: 'button', class: 'att', 'aria-label': 'Enlarge image ' + (k + 1) + ' of ' + imgs.length,
      onclick: () => lightbox(imgs, k),
    }, h('img', {src: im.url, alt: '', loading: 'lazy'})))) : null,
    m.content ? h('div', {class: 'bubble'}, m.content) : null);
}

function aiMessage(m, isLast) {
  return h('article', {class: 'msg msg-ai', 'aria-label': 'Answer from ' + modelName()},
    h('span', {class: 'who', title: fmtTime(m.time)}, modelName()),
    m.reasoning ? thinkBlock(m.reasoning) : null,
    m.content ? h('div', {class: 'bubble md'}, renderMarkdown(m.content, false, showCards(), showHosts())) : null,
    finishNote(m),
    msgTools(m, isLast));
}

function thinkBlock(reasoning) {
  const words = reasoning.trim().split(/\s+/).length;
  return h('details', {class: 'think'},
    h('summary', null, icon('chev', 'chev ic-sm'), h('span', {class: 'think-label'}, 'Thinking', h('span', {class: 'think-n'}, words + ' words'))),
    h('div', {class: 'think-body'}, reasoning));
}

function msgTools(m, isLast) {
  return h('div', {class: 'msg-tools'},
    m.content ? copyButton(m.content, 'Copy', 'Copy the answer') : null,
    isLast ? h('button', {type: 'button', class: 'btn btn-ghost btn-sm', onclick: regenerate, disabled: modelFacts().blocked},
      icon('refresh', 'ic-sm'), 'Regenerate') : null,
    statsLine(m.stats));
}

function finishNote(m) {
  if (m.finish === 'cancelled') return h('p', {class: 'msg-end'}, icon('stop', 'ic-sm fill'), 'Stopped. The answer above is incomplete.');
  if (m.finish === 'length') return h('p', {class: 'msg-end'}, icon('alert', 'ic-sm'), 'The answer reached the length limit and ends early. Ask it to continue.');
  if (m.note) return h('p', {class: 'msg-end'}, icon('alert', 'ic-sm'), m.note); // an agent answer that hit a limit
  if (m.finish === 'error') {
    const text = m.error || 'The answer failed.';
    return h('div', {class: 'banner banner-err msg-err'}, icon('alert'), h('div', {class: 'banner-body'}, h('span', null, text),
      /not installed/i.test(text) ? h('div', {class: 'banner-actions'},
        h('button', {type: 'button', class: 'btn btn-sm', onclick: () => s.ctx.router.go('models')}, 'Open Models')) : null));
  }
  return null;
}

function statsLine(st) {
  if (!st) return null;
  const parts = [];
  if (st.steps > 1) parts.push(st.steps + ' steps');
  if (st.tool_calls) parts.push(st.tool_calls + (st.tool_calls === 1 ? ' tool call' : ' tool calls'));
  if (st.generation_tokens) parts.push(st.generation_tokens + ' tokens');
  if (st.generation_tps) parts.push(st.generation_tps.toFixed(1) + ' tokens/s');
  if (typeof st.seconds === 'number') parts.push(st.seconds.toFixed(1) + ' s');
  return parts.length ? h('span', {class: 'msg-stats'}, parts.join(' · ')) : null;
}

function nearBottom() {
  const b = s.els.msgs;
  return b.scrollHeight - b.scrollTop - b.clientHeight < 80;
}

function scrollToEnd() {
  const b = s.els.msgs;
  b.scrollTop = b.scrollHeight;
}

// ---------- composer ----------

// The thinking level the composer sends: the one picked in this session for this chat, else the chat's saved level,
// else the Settings default for a new chat.
function effortLevel() {
  const known = (id) => EFFORTS.some((e) => e[0] === id);
  const picked = memo.efforts[memo.activeId || ''];
  if (known(picked)) return picked;
  if (s.chat && known(s.chat.effort)) return s.chat.effort;
  const llm = store.get('llm');
  return llm && known(llm.default_effort) ? llm.default_effort : 'off';
}

function pickEffort(id) {
  memo.efforts[memo.activeId || ''] = id;
  renderComposer();
  announce('Thinking: ' + EFFORTS.find((e) => e[0] === id)[1] + '.');
}

function renderEffort(blocked) {
  const [id, label, hint] = EFFORTS.find((e) => e[0] === effortLevel());
  const el = s.els.effort;
  el.disabled = blocked;
  el.classList.toggle('on', id !== 'off');
  el.title = 'How long the model may think before it answers. Now ' + label + '. ' + hint + '.';
  el.setAttribute('aria-label', 'Thinking level: ' + label);
  el.replaceChildren(icon('bulb', 'ic-sm'), 'Thinking', h('span', {class: 'effort-level'}, label), icon('chev-down', 'ic-sm'));
}

function renderComposer() {
  const els = s.els;
  const f = modelFacts();
  els.input.disabled = f.blocked;
  els.input.placeholder = f.tooLarge ? 'Chat is not available on this Mac'
    : f.missing ? 'Install ' + modelName() + ' in Models to chat' : 'Message ' + modelName();
  els.attachBtn.disabled = f.blocked || memo.attach.length >= MAX_IMAGES;
  els.attachBtn.title = memo.attach.length >= MAX_IMAGES
    ? 'At most ' + MAX_IMAGES + ' images per message.'
    : 'Attach PNG, JPEG or WebP images (up to ' + MAX_IMAGES + ', 20 MB each). You can also drop or paste them.';
  renderEffort(f.blocked);
  const agent = chatMode() === 'agent';
  els.toolsBtn.hidden = !agent;
  els.toolsBtn.disabled = !!(s.sending || s.loading);
  if (!agent && s.tools) closeTools();
  const live = s.live;
  if (live) {
    els.send.className = 'btn btn-sm';
    els.send.disabled = false;
    els.send.setAttribute('aria-disabled', String(live.stopping));
    els.send.replaceChildren(icon('stop', 'ic-sm fill'), live.stopping ? 'Stopping' : 'Stop');
  } else {
    els.send.className = 'btn btn-primary btn-sm';
    els.send.removeAttribute('aria-disabled');
    els.send.disabled = f.blocked || s.sending || !!s.modeBusy; // a mode change waits for the server
    els.send.replaceChildren(icon('send', 'ic-sm'), 'Send');
  }
  els.unload.disabled = !!live;
}

function onInputKey(e) {
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
  if (e.shiftKey || e.altKey) return;
  e.preventDefault();
  if (!s.live) send();
}

function onPaste(e) {
  const files = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => f.type.startsWith('image/'));
  if (!files.length) return;
  if (!e.clipboardData.types.includes('text/plain')) e.preventDefault();
  addFiles(files);
}

function dropTarget(el) {
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  el.addEventListener('dragenter', (e) => {
    if (!hasFiles(e) || modelFacts().blocked) return;
    s.dragDepth += 1;
    el.classList.add('dragging');
  });
  el.addEventListener('dragleave', () => {
    s.dragDepth = Math.max(0, s.dragDepth - 1);
    if (!s.dragDepth) el.classList.remove('dragging');
  });
  el.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = modelFacts().blocked ? 'none' : 'copy';
  });
  el.addEventListener('drop', (e) => {
    e.preventDefault();
    s.dragDepth = 0;
    el.classList.remove('dragging');
    if (!hasFiles(e) || modelFacts().blocked) return;
    addFiles([...e.dataTransfer.files]);
    s.els.input.focus();
  });
}

function addFiles(files) {
  const errs = [];
  for (const f of files) {
    if (memo.attach.length >= MAX_IMAGES) {
      errs.push('At most ' + MAX_IMAGES + ' images per message.');
      break;
    }
    if (!IMAGE_TYPES.includes(f.type)) errs.push((f.name || 'This file') + ': use a PNG, JPEG or WebP image.');
    else if (f.size > MAX_IMAGE) errs.push((f.name || 'This image') + ': too large (max 20 MB).');
    else memo.attach.push({file: f, name: f.name || 'pasted.png', size: f.size, url: URL.createObjectURL(f)});
  }
  showNote(errs.join(' '));
  renderAttach();
  renderComposer();
}

function renderAttach() {
  const box = s.els.attach;
  box.replaceChildren();
  box.hidden = !memo.attach.length;
  memo.attach.forEach((a, i) => {
    box.append(h('div', {class: 'file-chip'},
      h('span', {class: 'file-thumb'}, h('img', {src: a.url, alt: ''})),
      h('span', {class: 'file-info'}, h('span', {class: 'file-name', title: a.name}, a.name), h('span', {class: 'file-meta'}, fmtBytes(a.size))),
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Remove ' + a.name,
        onclick: () => {
          URL.revokeObjectURL(a.url);
          memo.attach.splice(i, 1);
          renderAttach();
          renderComposer();
          s.els.input.focus();
        },
      }, icon('x', 'ic-sm'))));
  });
}

function showNote(text) {
  const el = s.els.note;
  el.textContent = text || '';
  el.hidden = !text;
}

function announce(text) {
  if (s) s.els.status.textContent = text;
}

function errText(e) {
  if (e && e.offline) return 'The server is offline. Start the app again and retry.';
  return (e && e.message) || String(e);
}

// ---------- copy ----------

function copyButton(text, label, aria) {
  const b = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': aria || null}, icon('copy', 'ic-sm'), label);
  b.addEventListener('click', async () => {
    const ok = await copyText(text);
    b.replaceChildren(icon(ok ? 'check' : 'alert', 'ic-sm'), ok ? 'Copied' : 'Copy failed');
    announce(ok ? 'Copied.' : 'Copy failed.');
    setTimeout(() => {
      if (b.isConnected) b.replaceChildren(icon('copy', 'ic-sm'), label);
    }, 1500);
  });
  return b;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // WebKit without clipboard access: copy through a selection
    const back = document.activeElement;
    const ta = h('textarea', {class: 'sr-only', readonly: true});
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
    if (back && back.isConnected) back.focus();
    return ok;
  }
}

// ---------- Markdown ----------
// A small renderer for model text: headings, paragraphs (single line breaks kept), bold, italic, strikethrough,
// inline code, fenced code, nested lists, block quotes, rules, tables and links. It builds element and text nodes
// only. Only http(s) links become links; they open in the system browser (target _blank, rel noopener).
// With cards on (workstation chats), a fence "```prompt <tool id> [field]" for a known tool becomes a card with
// "Use in <tool>"; otherwise it stays a code block.

const RE = {
  fence: /^( {0,3})(`{3,}|~{3,})(.*)$/,
  fenceEnd: /^ {0,3}(`{3,}|~{3,})[ \t]*$/,
  heading: /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/,
  hr: /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/,
  quote: /^ {0,3}> ?(.*)$/,
  item: /^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*)|$)/,
  tableSep: /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/,
};

const INLINE = new RegExp([
  '(`+)([\\s\\S]*?[^`])\\1(?!`)', // 1, 2: code
  '\\*\\*(?=\\S)([\\s\\S]*?\\S)\\*\\*', // 3: strong
  '(?<![\\p{L}\\p{N}])__(?=\\S)([\\s\\S]*?\\S)__(?![\\p{L}\\p{N}])', // 4: strong
  '\\*(?=[^\\s*])([\\s\\S]*?[^\\s*])\\*(?!\\*)', // 5: em
  '(?<![\\p{L}\\p{N}_])_(?=[^\\s_])([\\s\\S]*?[^\\s_])_(?![\\p{L}\\p{N}_])', // 6: em
  '~~(?=\\S)([\\s\\S]*?\\S)~~', // 7: strikethrough
  '\\[([^\\]\\n]+)\\]\\(\\s*<?([^)\\s>]+)>?(?:\\s+"[^"\\n]*")?\\s*\\)', // 8, 9: link
  '(https?:\\/\\/[^\\s<>()\\[\\]]*[^\\s<>()\\[\\].,;:!?\'"*_~])', // 10: bare URL
  '\\\\([!-\\/:-@\\[-`{-~])', // 11: escaped punctuation
].join('|'), 'gu');

export function renderMarkdown(src, streaming, cards = true, hosts = false) {
  const out = document.createDocumentFragment();
  renderBlocks(String(src || '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n'), out, {streaming, cards, hosts});
  return out;
}

const indentOf = (line) => line.match(/^ */)[0].length;
const isTableStart = (lines, i) => lines[i].includes('|') && i + 1 < lines.length && lines[i + 1].includes('-') &&
  RE.tableSep.test(lines[i + 1]);

function startsBlock(lines, i) {
  const l = lines[i];
  return RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.quote.test(l) || RE.item.test(l) || isTableStart(lines, i);
}

// o: {streaming, cards, hosts}
function renderBlocks(lines, out, o) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    let m = RE.fence.exec(line);
    if (m && !(m[2][0] === '`' && m[3].includes('`'))) {
      const indent = m[1].length;
      const marker = m[2];
      const body = [];
      let closed = false;
      for (i++; i < lines.length; i++) {
        const end = RE.fenceEnd.exec(lines[i]);
        if (end && end[1][0] === marker[0] && end[1].length >= marker.length) {
          closed = true;
          i++;
          break;
        }
        body.push(lines[i].slice(Math.min(indent, indentOf(lines[i]))));
      }
      out.append(fenced(m[3].trim(), body.join('\n'), closed || !o.streaming, o.cards));
      continue;
    }
    if (RE.hr.test(line)) {
      out.append(h('hr'));
      i++;
      continue;
    }
    if ((m = RE.heading.exec(line))) {
      out.append(inline(h('h' + Math.min(6, m[1].length + 2), {class: 'md-h' + m[1].length}), m[2] || '', o));
      i++;
      continue;
    }
    if (RE.quote.test(line)) {
      const body = [];
      while (i < lines.length && lines[i].trim() && (m = RE.quote.exec(lines[i]))) {
        body.push(m[1]);
        i++;
      }
      const q = h('blockquote');
      renderBlocks(body, q, o);
      out.append(q);
      continue;
    }
    if (RE.item.test(line)) {
      i = renderMdList(lines, i, out, o);
      continue;
    }
    if (isTableStart(lines, i)) {
      i = renderTable(lines, i, out, o);
      continue;
    }
    const p = h('p');
    inline(p, line.trim(), o);
    for (i++; i < lines.length && lines[i].trim() && !startsBlock(lines, i); i++) {
      p.append(h('br'));
      inline(p, lines[i].trim(), o);
    }
    out.append(p);
  }
}

function renderMdList(lines, start, out, o) {
  const first = RE.item.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = h(ordered ? 'ol' : 'ul');
  const n = parseInt(first[2], 10);
  if (ordered && n !== 1) list.setAttribute('start', String(n));
  const sibling = (m) => m && m[1].length >= base && m[1].length <= base + 1 && /\d/.test(m[2]) === ordered;
  let i = start;
  while (i < lines.length) {
    const m = RE.item.exec(lines[i]);
    if (!sibling(m)) break;
    const inner = Math.min(m[1].length + m[2].length + 1, base + 4);
    const body = [m[3] || ''];
    for (i++; i < lines.length; i++) {
      const l = lines[i];
      if (!l.trim()) {
        // A blank line stays in the item only when the next text is indented into it.
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        if (j < lines.length && indentOf(lines[j]) >= Math.min(inner, base + 2)) {
          for (; i < j; i++) body.push('');
          i--;
          continue;
        }
        break;
      }
      const ind = indentOf(l);
      if (ind >= Math.min(inner, base + 2)) body.push(l.slice(Math.min(ind, inner)));
      else if (startsBlock(lines, i)) break;
      else body.push(l.trim()); // a lazy continuation line
    }
    const li = h('li');
    renderBlocks(body, li, o);
    list.append(li);
    let j = i;
    while (j < lines.length && !lines[j].trim()) j++;
    if (j > i && j < lines.length && sibling(RE.item.exec(lines[j]))) i = j;
  }
  out.append(list);
  return i;
}

function renderTable(lines, i, out, o) {
  const cells = (line) => {
    let t = line.trim();
    if (t.startsWith('|')) t = t.slice(1);
    if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
    return t.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
  };
  const head = cells(lines[i]);
  const align = cells(lines[i + 1]).map((c) => (c.endsWith(':') ? (c.startsWith(':') ? 'center' : 'right') : null));
  const cell = (tag, text, k) => inline(h(tag, align[k] ? {style: {textAlign: align[k]}} : null), text, o);
  const thead = h('thead', null, h('tr', null, head.map((c, k) => cell('th', c, k))));
  const tbody = h('tbody');
  for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) {
    const row = cells(lines[i]);
    tbody.append(h('tr', null, head.map((_, k) => cell('td', row[k] || '', k))));
  }
  out.append(h('div', {class: 'md-table', tabindex: '0'}, h('table', null, thead, tbody)));
  return i;
}

function inline(parent, text, o) {
  let pos = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > pos) parent.append(text.slice(pos, m.index));
    pos = m.index + m[0].length;
    if (m[1] !== undefined) parent.append(h('code', null, m[2].replace(/^ (.*[^ ].*) $/, '$1')));
    else if (m[3] !== undefined || m[4] !== undefined) parent.append(inline(h('strong'), m[3] ?? m[4], o));
    else if (m[5] !== undefined || m[6] !== undefined) parent.append(inline(h('em'), m[5] ?? m[6], o));
    else if (m[7] !== undefined) parent.append(inline(h('del'), m[7], o));
    else if (m[8] !== undefined) link(parent, m[8], m[9], o);
    else if (m[10] !== undefined) link(parent, null, m[10], o);
    else parent.append(m[11]);
  }
  if (pos < text.length) parent.append(text.slice(pos));
  return parent;
}

const LINK_TEXT = 80; // characters of a bare URL shown in an agent chat; the title has all of it

// The host a link opens, as the browser sends it (IDNA, so a look-alike letter shows as xn--), with the query that
// goes with it: a link the model wrote can carry chat text to that host in its query. Null when the URL does not
// parse.
function linkHost(url) {
  try {
    const u = new URL(url);
    if (!u.search) return u.host;
    const sent = u.host + u.pathname + u.search;
    return sent.length > LINK_TEXT ? sent.slice(0, LINK_TEXT - 1) + '…' : sent;
  } catch {
    return null;
  }
}

// o.hosts (agent chats): "label (host)", and a long bare URL is shortened with all of it in the title. A URL whose
// host does not parse stays text there.
function link(parent, label, url, o) {
  const web = /^https?:\/\//i.test(url);
  const host = web && o && o.hosts ? linkHost(url) : '';
  if (web && host !== null) {
    const a = h('a', {href: url, target: '_blank', rel: 'noopener noreferrer', title: url});
    const shown = label === null && host && url.length > LINK_TEXT ? url.slice(0, LINK_TEXT - 1) + '…' : url;
    // in agent chats the label is isolated, so a bidi control in it cannot reorder the host after it
    if (label === null) a.append(shown);
    else if (host) a.append(inline(h('bdi'), label, o));
    else inline(a, label, o);
    // a bare URL names its host already, unless it hides it (user info, a non-ASCII name, a cut before its end)
    const named = label === null && shown.toLowerCase().startsWith(url.split(':')[0].toLowerCase() + '://' + host.replace(/…$/, ''));
    if (host && !named) a.append(' ', h('bdi', {class: 'link-host', dir: 'ltr'}, '(' + host + ')'));
    parent.append(a);
  } else if (label === null) {
    parent.append(url);
  } else {
    inline(parent, label, o);
    parent.append(' (' + url + ')');
  }
}

function fenced(info, text, done, cards) {
  const m = cards && /^prompt\s+([\w.-]+)(?:\s+([\w-]+))?\s*$/i.exec(info);
  const target = m && promptTarget(m[1], m[2]);
  return target ? promptCard(target, text, done) : codeBlock(info.split(/\s+/)[0], text, done);
}

// The tool and text field of a "prompt <tool id> [field]" fence; null for an unknown tool or field.
function promptTarget(toolId, fieldName) {
  const tool = store.get('toolsById').get(toolId);
  if (!tool) return null;
  const fields = tool.fields || [];
  const field = fieldName
    ? fields.find((f) => f.name === fieldName && (f.type === 'text' || f.type === 'textarea'))
    : fields.find((f) => f.assist);
  if (!field) return null;
  const several = fields.filter((f) => f.assist).length > 1;
  return {tool, field, label: tool.label + (several ? ' › ' + field.label : '')};
}

function promptCard(t, raw, done) {
  const text = raw.replace(/^\n+/, '').replace(/\s+$/, '');
  return h('div', {class: 'prompt-card g-' + String(t.tool.group).toLowerCase(), role: 'group', 'aria-label': 'Text for ' + t.label},
    h('div', {class: 'prompt-head'}, toolIcon(t.tool.group, true), h('span', {class: 'prompt-tool'}, t.tool.label),
      h('span', {class: 'prompt-field'}, t.field.label)),
    h('div', {class: 'prompt-text'}, text),
    done ? h('div', {class: 'prompt-actions'},
      h('button', {
        type: 'button', class: 'btn btn-sm',
        onclick: () => s.ctx.handoff.useText({tool: t.tool.id, field: t.field.name, text}),
      }, icon('arrow', 'ic-sm'), 'Use in ' + t.label),
      copyButton(text, 'Copy', 'Copy the text for ' + t.label)) : null);
}

function codeBlock(lang, text, done) {
  return h('div', {class: 'code-block'},
    h('div', {class: 'code-head'}, h('span', {class: 'code-lang'}, lang || 'text'), done ? copyButton(text, 'Copy', 'Copy the code') : null),
    h('pre', {tabindex: '0'}, h('code', null, text)));
}

export default {
  id: 'chat',
  title: 'Chat',
  subtitle: 'Runs on this Mac.', // renderHead adds what the mode sends
  mount,
  update,
  unmount,
};
