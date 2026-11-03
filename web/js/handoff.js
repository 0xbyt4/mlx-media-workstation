// The cross-view contract: any view hands a file, a text or a job's settings to a tool; the Create view takes them
// when it shows that tool (handoff.take(toolId) on mount and on every route change).
//
//   handoff.send({tool, field, file: {url, name, kind}})  an output file goes to a tool's file field
//   handoff.useText({tool, field, text})                  a text (from Chat) goes to a tool's text field
//   handoff.useSettings(job)                              a job's params fill its tool's form (not its files)
//   handoff.targets(kind)                                 the "Send to" menu entries for an output kind
//   handoff.take(toolId)                                  Create only: the waiting items for that tool, oldest first

import {safeFileUrl} from './api.js';
import {router} from './router.js';
import {store} from './store.js';
import {toast} from './ui.js';

// "Send to" targets per output kind, in menu order: [tool id, file field]. Missing tools or fields are skipped.
export const SEND_TARGETS = {
  image: [['video.i2v', 'image'], ['video.a2v', 'image'], ['image.klein_edit', 'references']],
  audio: [['video.a2v', 'audio'], ['edit.mix', 'music'], ['voice.drama3', 'ref0'], ['voice.drama3', 'ref1']],
  video: [['video.extend', 'video'], ['edit.mix', 'video']],
};

const pending = new Map();

function findField(toolId, fieldName, types) {
  const tool = store.get('toolsById').get(toolId);
  const field = tool && (tool.fields || []).find((f) => f.name === fieldName);
  return field && types.includes(field.type) ? {tool, field} : null;
}

function queue(toolId, item) {
  if (!pending.has(toolId)) pending.set(toolId, []);
  pending.get(toolId).push(item);
  router.go('create', {tool: toolId});
}

// [{tool, field, label, sub}] for the tools and file fields that exist.
export function targets(kind) {
  const out = [];
  for (const [toolId, fieldName] of SEND_TARGETS[kind] || []) {
    const t = findField(toolId, fieldName, ['file', 'files']);
    if (t) out.push({tool: t.tool.id, field: t.field.name, label: t.tool.label, sub: t.field.label});
  }
  return out;
}

export function send({tool, field, file}) {
  if (!findField(tool, field, ['file', 'files']) || !file || !safeFileUrl(file.url)) {
    toast('That file cannot go to ' + tool + ' › ' + field + '.', {kind: 'err'});
    return false;
  }
  queue(tool, {type: 'file', field, file: {url: file.url, name: file.name || file.url.split('/').pop(), kind: file.kind}});
  return true;
}

export function useText({tool, field, text}) {
  if (!findField(tool, field, ['text', 'textarea'])) {
    toast('That text cannot go to ' + tool + ' › ' + field + '.', {kind: 'err'});
    return false;
  }
  queue(tool, {type: 'text', field, text: String(text)});
  return true;
}

export function useSettings(job) {
  if (!store.get('toolsById').has(job.tool)) {
    toast('Tool "' + job.tool + '" is not available.', {kind: 'err'});
    return false;
  }
  queue(job.tool, {type: 'settings', job: {id: job.id, tool: job.tool, params: job.params || {}}});
  return true;
}

export function take(toolId) {
  const items = pending.get(toolId) || [];
  pending.delete(toolId);
  return items;
}

export const handoff = {SEND_TARGETS, targets, send, useText, useSettings, take};

export default handoff;
