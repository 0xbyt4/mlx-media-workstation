# Web UI

The interface is plain HTML, CSS and JavaScript modules. There is no build step and no framework. The page loads
nothing from the network. `app/server.py` serves this folder at `/`.

The design is in `docs/DESIGN.md` and `docs/design/mock.html`. The endpoints are in `docs/API.md`.

## File layout

| File | Contents |
|---|---|
| `index.html` | App frame: sidebar, toolbar, `<main id="view">`, the icon sprite, all stylesheets. |
| `css/tokens.css` | Design tokens for light and dark. `[data-theme]` overrides the system setting. |
| `css/base.css` | Shared components. The class names are listed at the top of the file. |
| `css/shell.css` | Sidebar, job dock, memory meter, toolbar, narrow layouts. |
| `css/views/<view>.css` | The styles of one view. |
| `css/motion.css` | Motion tokens (durations, easings) and the enter and leave animations of all parts. |
| `js/main.js` | Boot: theme, store, router, sidebar, toolbar, view mounting. |
| `js/api.js` | Fetch wrappers for every endpoint, `ApiError`, the SSE reader, `poll()`. |
| `js/store.js` | Shared state, events and the one polling loop. |
| `js/ui.js` | DOM helper `h()`, icons, formatters, `readablePrompt()`, `leave()` (exit animations), toasts, dialogs, menus, the fit rule, media, theme. |
| `js/router.js` | Hash routes, the sections of each profile and the Cmd-1 to Cmd-6 shortcuts. |
| `js/handoff.js` | "Send to", "Use in" and "Use these settings" between views. |
| `js/views/<view>.js` | One view: `create`, `jobs`, `chat`, `library`, `models`, `settings`. |

## Ownership rules

1. A view changes only its own files: `js/views/<view>.js` and `css/views/<view>.css`.
2. Scope every rule in a view stylesheet under `.v-<view>` (for example `.v-library .grid`).
3. Use the shared classes in `css/base.css` and the helpers in `js/ui.js` before you add new ones.
4. Keep a change to a shared file (`index.html`, `css/tokens.css`, `css/base.css`, `css/shell.css`, `js/*.js`) in
   its own commit. A shared file affects every view, so say in the commit message which views need the change and
   why.
5. Use only colours from `css/tokens.css`. Media placeholders are the one exception.

## View contract

Each view module has this default export:

```js
export default {
  id: 'library',
  title: 'Library',
  subtitle: 'Every output in outputs/',  // a string, or (ctx) => string
  mount(root, ctx, params) {},          // build into root (a <section class="view">)
  update(params) {},                    // optional: the route changed while this view is open
  unmount() {},                         // remove listeners, timers and open requests
};
```

- `root` is empty when `mount` runs. Add the class `v-<view>` to it. Add `view-full` for a view that fills the height
  and scrolls inside (Chat).
- `params` are the route params, for example `{tool: 'video.extend'}` for `#/create?tool=video.extend`.
- Without `update`, a route change to the same view does nothing. With it, the view gets the new params.
- `ctx` is `{api, store, ui, router, handoff, shell}`. `shell.setTitle(text)` and `shell.setSubtitle(text)` change the
  toolbar.
- `unmount` must call every unsubscribe function that `store.on()` returned.

## Store

`store.get(key)` returns the latest value. `store.on(event, fn)` subscribes and returns the unsubscribe function.

| Event | Value | Polling |
|---|---|---|
| `tools` | `GET /api/tools` | at start, retried until it works |
| `jobs` | `GET /api/jobs` | every 2 s while a job is queued or running, else every 10 s |
| `system` | `GET /api/system` | every 5 s |
| `llm` | `GET /api/llm` | every 5 s (1.5 s while a view asks with `store.fast('llm')`) |
| `assist` | `GET /api/assist` | every 60 s, and when the model is installed or removed |
| `models` | `GET /api/models` | every 60 s, every 2 s while a download runs |
| `offline` | `true` or `false` | when the server stops or comes back |
| `signedout` | `true` or `false` | when the server answers HTTP 401 (it restarted with a new session), and when it accepts the page again |
| `profile` | `'full'` or `'chat'` | with the first `GET /api/system` answer, and when the profile changes |
| `job:finished` | the job | a job goes from queued or running to done, failed or cancelled |
| `tick` | none | every second, for live clocks |

While the page is hidden (for example, the window is minimized), no source is polled. When the page shows again,
each source that is due is fetched at once.

Other store functions:

- `store.refresh(key)` fetches a source now, for example after a submit or a download start.
- `store.fast(key)` asks for faster polling and returns the function that ends the request.
- `store.toolPeak(id)`, `store.toolInstalled(id)`, `store.toolModels(id)`, `store.toolTime(id)` give the facts of a tool.
- `store.memTotal()`, `store.reserve()`, `store.runningJob()`, `store.queuedJobs()`, `store.queueSeconds()`,
  `store.liveElapsed(job)` and `store.llmUnloadsFor(peakGb)` give the facts of this Mac and the queue.
- `store.profile()` gives the profile, `store.profilePinned()` tells whether it comes from the page address, and
  `store.shownModel(m)` tells whether the profile shows an entry of `GET /api/models`.

## Profiles

The profile picks the sections. `full` shows all six. `chat` shows Chat, Models and Settings, opens on Chat and has
no job dock. The server sends the profile in `GET /api/system`; `?profile=full` or `?profile=chat` in the page
address overrides it for that window (`start.py` opens such a window next to a running app).

- `main.js` starts the router only when the profile is known, so no hidden section shows for a moment. Without a
  server it starts with `full`.
- `router.views()` lists the sections of the profile, and the shortcuts follow that list. `router.setProfile(p)`
  switches; when the open section is not in the new profile, the router opens the profile's home.
- A view that shows media tools or jobs checks `store.profile()` and listens to the `profile` event. Chat, Models
  and Settings do. In the chat profile, nothing may link to Create, Jobs or Library.

## Handoff

The handoff API moves work between views. Each call opens Create with the target tool. Create takes the items when it
shows that tool.

```js
handoff.send({tool: 'video.i2v', field: 'image', file: {url, name, kind}});  // an output file to a file field
handoff.useText({tool: 'video.i2v', field: 'prompt', text});                // a text to a text field
handoff.useSettings(job);                                                   // a job's params, not its files
handoff.targets(kind);  // [{tool, field, label, sub}]: the "Send to" menu for 'image', 'audio' or 'video'
handoff.take(toolId);   // Create only: the waiting items for that tool
```

`send` accepts only URLs under `/files/` or `/api/chats/`. A single file field replaces its file. A multi-file field
adds the file, up to its limit.

## API helpers

- Every `api.*` call throws `ApiError` on failure. `message` is the server's `detail`. `offline` is true when the
  server did not answer.
- `api.sendMessage(chatId, formData, onEvent, signal)` and `api.resumeStream(chatId, onEvent, signal)` read the chat
  event stream. They call `onEvent` with each `data:` object and resolve with the last one.
- `api.poll(task, delay)` runs `task` and then again after `delay(result, error)` ms. It returns `{kick, stop}`.
- `api.agentState(chatId)`, `api.changeAgent(chatId, body)` and `api.approve(chatId, callId, decision)` read and
  change an agent chat's Tools menu and answer an approval card. The chat view handles the agent events (`step`,
  `tool_call`, `approval`, `approval_done`, `tool_result`); a page that comes back shows a waiting approval again.

## Saved in the browser

Only per-viewer conveniences go to `localStorage`: `ws.theme`, `ws.view`, `ws.lastTool`, `ws.advancedOpen`,
`ws.jobsFilter`, `ws.chatMode` (the mode of the last new chat) and the form values per tool (`ws.form.<tool id>`). Chosen files stay in memory for the session.
