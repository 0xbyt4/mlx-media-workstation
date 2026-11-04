// Shared state and one polling loop. Views read with store.get(key) and subscribe with store.on(event, fn), which
// returns the unsubscribe function (call it in unmount).
//
// Events: 'tools', 'jobs', 'system', 'llm', 'assist', 'models' (the new value), 'offline' (true/false),
// 'job:finished' (a job that went from queued/running to done, failed or cancelled while the page was open),
// 'profile' ('full' or 'chat': what the UI shows; see profile() below), 'signedout' (true/false: the server answers
// HTTP 401, because it restarted with a new session token).

import {api, poll} from './api.js';

export const ACTIVE = new Set(['queued', 'running']);
export const FINISHED = new Set(['done', 'failed', 'cancelled']);
export const DEFAULT_RESERVE_GB = 8;
export const PROFILES = ['full', 'chat'];

// A window that start.py opened next to a running app in another profile has ?profile= in its address: that window
// keeps its profile whatever the server says.
const pinned = (() => {
  const p = new URLSearchParams(location.search).get('profile');
  return PROFILES.includes(p) ? p : null;
})();

const state = {
  tools: [],
  toolsById: new Map(),
  jobs: [],
  jobsAt: 0, // performance.now() of the last jobs fetch, for live elapsed times
  system: null,
  llm: null,
  assist: null,
  models: null,
  offline: false,
  signedOut: false,
  profile: pinned, // null until /api/system answers
};
const listeners = new Map();
const offlineBy = {};
const signedOutBy = {};
const fastWanted = {jobs: 0, system: 0, llm: 0, models: 0};
const loops = {};
let started = false;

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, data) {
  for (const fn of [...(listeners.get(event) || [])]) {
    try {
      fn(data);
    } catch (e) {
      console.error('Listener for ' + event + ' failed', e);
    }
  }
}

export function get(key) {
  return state[key];
}

function setOffline(source, value) {
  offlineBy[source] = value;
  const any = Object.values(offlineBy).some(Boolean);
  if (any !== state.offline) {
    state.offline = any;
    emit('offline', any);
  }
}

function setSignedOut(source, value) {
  signedOutBy[source] = value;
  const any = Object.values(signedOutBy).some(Boolean);
  if (any !== state.signedOut) {
    state.signedOut = any;
    emit('signedout', any);
  }
}

// Wraps a fetch: stores the value, emits it, and tracks the offline and signed-out states.
function loader(key, fetcher, after) {
  let seq = 0;
  return async () => {
    const mine = ++seq;
    try {
      const value = await fetcher();
      if (mine !== seq) return value; // a newer fetch of this source started meanwhile
      setOffline(key, false);
      setSignedOut(key, false);
      const prev = state[key];
      state[key] = value;
      if (after) after(value, prev);
      emit(key, value);
      return value;
    } catch (e) {
      if (mine === seq) {
        setOffline(key, !!e.offline);
        setSignedOut(key, e.status === 401);
      }
      throw e;
    }
  };
}

let jobsLoaded = false;
function noteFinished(jobs, prev) {
  state.jobsAt = performance.now();
  if (!jobsLoaded) {
    jobsLoaded = true;
    return;
  }
  const before = new Map(prev.map((j) => [j.id, j.status]));
  for (const j of jobs) {
    if (ACTIVE.has(before.get(j.id)) && FINISHED.has(j.status)) emit('job:finished', j);
  }
}

const fetchJobs = loader('jobs', api.jobs, noteFinished);
const fetchSystem = loader('system', api.system, (sys) => {
  const next = pinned || (PROFILES.includes(sys.profile) ? sys.profile : 'full');
  if (next === state.profile) return;
  state.profile = next;
  emit('profile', next);
});
const fetchModels = loader('models', api.models);
const fetchAssist = loader('assist', api.assist);
const fetchLlm = loader('llm', api.llm, (llm, prev) => {
  if (prev && prev.installed !== llm.installed) refresh('assist');
});
const fetchTools = loader('tools', api.tools, (tools) => {
  state.toolsById = new Map(tools.map((t) => [t.id, t]));
});

const downloading = () => !!(state.models && ((state.models.queue && state.models.queue.count > 0) ||
  state.models.models.some((m) => m.download && m.download.status === 'running')));

function startLoops() {
  loops.jobs = poll(fetchJobs, (jobs, err) => {
    if (err) return 3000;
    return fastWanted.jobs || jobs.some((j) => ACTIVE.has(j.status)) ? 2000 : 10000;
  });
  loops.system = poll(fetchSystem, (_, err) => (err ? 5000 : fastWanted.system ? 2000 : 5000));
  loops.llm = poll(fetchLlm, (_, err) => (err ? 5000 : fastWanted.llm ? 1500 : 5000));
  loops.models = poll(fetchModels, (_, err) => (err ? 10000 : fastWanted.models || downloading() ? 2000 : 60000));
  loops.tools = poll(fetchTools, (_, err) => (err ? 3000 : 24 * 3600 * 1000));
  loops.assist = poll(fetchAssist, (_, err) => (err ? 10000 : 60000));
}

export function start() {
  if (started) return;
  started = true;
  startLoops();
}

// Fetch one source now (after a submit, a cancel, a download start). Resolves when it has been fetched.
export function refresh(key) {
  const loop = loops[key];
  return loop ? loop.kick() : Promise.resolve();
}

// Ask for faster polling of a source while something waits on it; returns the function that ends the request.
export function fast(key) {
  fastWanted[key] += 1;
  refresh(key);
  let done = false;
  return () => {
    if (!done) fastWanted[key] -= 1;
    done = true;
  };
}

// ---------- derived facts that several views need ----------

// What the UI shows: 'full' (every section) or 'chat' (Chat, Models and Settings). The server's [app] profile, or the
// window's ?profile=; 'full' until the first /api/system answer.
export function profile() {
  return state.profile || 'full';
}

// True when this window's profile comes from its address (?profile=), not from the server.
export function profilePinned() {
  return pinned !== null;
}

// The models the UI shows: in the chat profile only what Chat needs (the LLM environment and weights).
export function shownModel(m) {
  return profile() !== 'chat' || (m.features || []).includes('Chat');
}

export function memTotal() {
  if (state.system && state.system.mem_total_gb) return state.system.mem_total_gb;
  return state.models ? state.models.machine.mem_total_gb : null;
}

export function reserve() {
  return state.models ? state.models.machine.mem_reserve_gb : DEFAULT_RESERVE_GB;
}

export function toolModels(toolId) {
  return state.models ? state.models.models.filter((m) => (m.tools || []).includes(toolId)) : null;
}

// Peak memory in GB of a job of this tool (README numbers, measured on a 48 GB M5 Pro); 0 = no model; null = unknown.
export function toolPeak(toolId) {
  const ms = toolModels(toolId);
  if (!ms) return null;
  return ms.reduce((max, m) => Math.max(max, m.peak_gb || 0), 0);
}

// true or false from /api/models; null while the models are not loaded yet.
export function toolInstalled(toolId) {
  const ms = toolModels(toolId);
  return ms ? ms.every((m) => m.installed) : null;
}

// Median run time in seconds of this tool's finished jobs on this Mac, and how many it is based on.
export function toolTime(toolId) {
  const xs = state.jobs
    .filter((j) => j.tool === toolId && j.status === 'done' && typeof j.elapsed === 'number')
    .map((j) => j.elapsed)
    .sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  return {seconds: xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2, count: xs.length};
}

export function runningJob() {
  return state.jobs.find((j) => j.status === 'running') || null;
}

export function queuedJobs() {
  return state.jobs.filter((j) => j.status === 'queued').sort((a, b) => (a.position || 0) - (b.position || 0));
}

// Elapsed seconds of a job, counted on from the last fetch while it runs.
export function liveElapsed(job) {
  if (job.status !== 'running') return job.elapsed;
  if (typeof job.elapsed === 'number') return job.elapsed + (performance.now() - state.jobsAt) / 1000;
  const started = Date.parse(job.started);
  return isNaN(started) ? null : (Date.now() - started) / 1000;
}

// Seconds until the queue is empty, or null when a part of it has no estimate.
export function queueSeconds() {
  let total = 0;
  const run = runningJob();
  if (run) {
    const left = run.progress && typeof run.progress.eta_s === 'number' ? run.progress.eta_s : null;
    if (left === null) return null;
    total += left;
  }
  for (const j of queuedJobs()) {
    const t = toolTime(j.tool);
    if (!t) return null;
    total += t.seconds;
  }
  return total;
}

// True when the local LLM has to unload while a job with this peak runs (the rule in app/llm.py fits_with).
export function llmUnloadsFor(peakGb) {
  const llm = state.llm;
  const mem = memTotal();
  if (!llm || !llm.installed || !mem || peakGb === null) return false;
  return llm.peak_gb + peakGb + reserve() > mem;
}

export const store = {
  on,
  emit,
  get,
  start,
  refresh,
  fast,
  profile,
  profilePinned,
  shownModel,
  memTotal,
  reserve,
  toolModels,
  toolPeak,
  toolInstalled,
  toolTime,
  runningJob,
  queuedJobs,
  liveElapsed,
  queueSeconds,
  llmUnloadsFor,
  ACTIVE,
  FINISHED,
  PROFILES,
};

export default store;
