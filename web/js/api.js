// Fetch wrappers for every endpoint in docs/API.md. Errors throw ApiError with the server's `detail` as the message.

export class ApiError extends Error {
  constructor(message, {status = 0, offline = false, detail = null} = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.offline = offline;
    this.detail = detail;
  }
}

async function send(path, opts) {
  try {
    return await fetch(path, Object.assign({cache: 'no-store'}, opts));
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw new ApiError('Server offline', {offline: true});
  }
}

async function failure(res) {
  const text = await res.text();
  let body = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // not JSON: keep the text
  }
  const detail = body && typeof body === 'object' ? body.detail : body;
  return new ApiError(detailText(detail, res), {status: res.status, detail});
}

export function detailText(d, res) {
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d)) {
    // FastAPI validation errors
    return d.map((x) => {
      if (x && x.msg) {
        const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
        return (loc ? loc + ': ' : '') + x.msg;
      }
      return JSON.stringify(x);
    }).join('\n');
  }
  if (d) return JSON.stringify(d);
  return res ? ('HTTP ' + res.status + ' ' + res.statusText).trim() : 'Request failed';
}

export async function request(path, opts) {
  const res = await send(path, opts);
  if (!res.ok) throw await failure(res);
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const post = (path, body, opts) => request(path, Object.assign({method: 'POST', body}, opts));
const del = (path) => request(path, {method: 'DELETE'});
const enc = encodeURIComponent;

// Reads a text/event-stream body and calls onEvent with each parsed `data: {...}` line. Resolves with the last event.
export async function readSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let last = null;
  const handle = (line) => {
    if (!line.startsWith('data:')) return;
    const raw = line.slice(5).trim();
    if (!raw) return;
    let ev;
    try {
      ev = JSON.parse(raw);
    } catch {
      return;
    }
    last = ev;
    onEvent(ev);
  };
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) handle(line.replace(/\r$/, ''));
  }
  buf += decoder.decode();
  if (buf) handle(buf.replace(/\r$/, ''));
  return last;
}

async function stream(path, opts, onEvent) {
  const res = await send(path, opts);
  if (!res.ok) throw await failure(res);
  return readSSE(res, onEvent);
}

// Runs task() now and again after delay(result, error) ms. kick() runs it at once; stop() ends the loop.
export function poll(task, delay) {
  let timer = null;
  let seq = 0;
  let stopped = false;
  async function run() {
    clearTimeout(timer);
    const mine = ++seq;
    let result, error;
    try {
      result = await task();
    } catch (e) {
      error = e;
    }
    if (stopped || mine !== seq) return;
    timer = setTimeout(run, delay(result, error));
  }
  run();
  return {
    kick: run,
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
  };
}

// Output URLs must stay on this origin under /files/ (job outputs) or /api/chats/ (chat images).
export function safeFileUrl(url) {
  return typeof url === 'string' && (url.startsWith('/files/') || url.startsWith('/api/chats/')) ? url : null;
}

export const api = {
  request,
  readSSE,
  poll,
  safeFileUrl,

  // tools and jobs
  tools: () => request('/api/tools'),
  jobs: () => request('/api/jobs'),
  createJob: (formData) => post('/api/jobs', formData),
  cancelJob: (id) => post('/api/jobs/' + enc(id) + '/cancel'),
  deleteJob: (id) => del('/api/jobs/' + enc(id)),

  // system and models
  system: () => request('/api/system'),
  models: () => request('/api/models'),
  downloadModel: (id) => post('/api/models/' + enc(id) + '/download'),
  cancelDownload: (id) => post('/api/models/' + enc(id) + '/cancel'),

  // prompt help
  assist: () => request('/api/assist'),
  // formData carries request_id; cancelEnhance(request_id) stops that request (it then fails with HTTP 409).
  enhance: (formData, opts) => post('/api/assist/enhance', formData, opts),
  cancelEnhance: (requestId) => post('/api/assist/cancel', JSON.stringify({request_id: requestId}),
    {headers: {'Content-Type': 'application/json'}}),

  // local LLM and chat
  llm: () => request('/api/llm'),
  unloadLlm: () => post('/api/llm/unload'),
  chats: () => request('/api/chats'),
  createChat: () => post('/api/chats'),
  chat: (id) => request('/api/chats/' + enc(id)),
  deleteChat: (id) => del('/api/chats/' + enc(id)),
  chatFileUrl: (id, name) => '/api/chats/' + enc(id) + '/files/' + enc(name),
  // formData: text, thinking ('true'/'false'), images (up to 4), or regenerate='true'.
  sendMessage: (id, formData, onEvent, signal) =>
    stream('/api/chats/' + enc(id) + '/messages', {method: 'POST', body: formData, signal}, onEvent),
  resumeStream: (id, onEvent, signal) => stream('/api/chats/' + enc(id) + '/stream', {signal}, onEvent),
  stopChat: (id) => post('/api/chats/' + enc(id) + '/stop'),
};

export default api;
