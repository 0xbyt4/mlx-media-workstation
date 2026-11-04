# API (contract between the server and the web UI)

The server (`app/server.py`, FastAPI) listens on `http://127.0.0.1:7870` only. The UI is static files in `web/`
(`index.html`, `css/`, `js/`; see `web/README.md`), served at `/`. Plain HTML, CSS and JavaScript: no framework, no build step,
no CDN or other network request. All JSON is UTF-8.

Every request needs the session cookie (`workstation_7870`, HttpOnly, SameSite=Strict). Without it the server
answers HTTP 401: a JSON `detail` for `/api/` and `/files/`, a short HTML page for the rest. `GET /auth?t=<token>&next=<path>`
sets the cookie when `t` is this run's token (see `GET /api/system?nonce=` below) and answers 303 to `next`, a path of
this app (`/` for anything else). A wrong token is HTTP 403. `start.py` opens the window on this link and prints it in
`--browser` mode. So another program or another user on this Mac, which cannot read `outputs/.server-token`, cannot use
the API. Limit: cookies ignore the port, so in `--browser` mode the browser also sends the cookie to other servers on
127.0.0.1 that its pages open. The app window keeps its cookies to itself. Only `/auth` and `/api/system?nonce=` work without the cookie.

Every response has `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`, so no other page
can show the app in a frame. A response with its own policy (the workspace files) gets `frame-ancestors 'none'`
added to it.

## Tools

`GET /api/tools` returns the list of tools. The Create screen builds its tool list and its forms from it.

```json
[
  {
    "id": "image.moss",
    "group": "Image",
    "label": "MOSS-Image",
    "description": "Photoreal people and scenes. About 4 min for 768x1344.",
    "fields": [
      {"name": "prompt", "label": "Prompt", "type": "textarea", "default": "", "required": true, "help": "..."},
      {"name": "size", "label": "Size", "type": "select", "default": "768x1344",
       "options": [{"value": "768x1344", "label": "9:16 (768x1344)"}]},
      {"name": "seed", "label": "Seed", "type": "number", "default": null, "help": "Empty = random"},
      {"name": "steps", "label": "Steps", "type": "number", "default": 50, "min": 1, "max": 100, "advanced": true},
      {"name": "image", "label": "Start image", "type": "file", "accept": "image/*", "required": true},
      {"name": "references", "label": "References", "type": "files", "accept": "image/*", "max": 10},
      {"name": "instrumental", "label": "Instrumental", "type": "checkbox", "default": true}
    ]
  }
]
```

- `group` is one of `Image`, `Voice`, `Music`, `Video`, `Edit`, in that order in the tool list.
- Field `type` is one of `text`, `textarea`, `number`, `select`, `checkbox`, `file` (one file), `files` (several).
- `advanced: true` fields go in a collapsed "Advanced" section.
- `step` may be given for numbers (for example 0.05). A `null` default means an empty input.
- `assist: true` marks a text field that has an "Improve" button (see Prompt help below).

`GET /api/tools?installed=true` adds `"installed"` to each tool: `true` when its model files are on disk
(`tools.REQUIRES`). The MCP server ([MCP.md](MCP.md)) uses it.

## Jobs

`POST /api/jobs` takes `multipart/form-data` with `tool` (the tool id) and one part per field (files as file parts,
checkboxes as `true`/`false`). It returns `{"id": "20261023-231500-ab12"}`. Errors return HTTP 400 with
`{"detail": "message"}`.

An optional `client` part with the value `mcp` marks a job from the MCP server ([MCP.md](MCP.md)). The job gets the
checks of an agent's job (see docs/AGENT.md, "Input files": a file must have a media type that its field accepts) and
`"chat": "mcp"` in its `job.json`. Another `client` value is HTTP 400.

`GET /api/jobs` returns all jobs, newest first:

```json
[
  {
    "id": "20261023-231500-ab12",
    "tool": "image.moss",
    "tool_label": "MOSS-Image",
    "status": "queued | running | done | failed | cancelled",
    "created": "2026-10-23T23:15:00",
    "started": "2026-10-23T23:15:02",
    "finished": null,
    "elapsed": 42.5,
    "progress": {"stage": "Stage 2: 1/3", "fraction": 0.52, "eta_s": 95},
    "position": 0,
    "params": {"prompt": "...", "size": "768x1344"},
    "outputs": [
      {"url": "/files/20261023-231500-ab12/image.png", "kind": "image | audio | video | text", "name": "image.png",
       "note": "optional text, e.g. what the speech check heard"}
    ],
    "peak_gb": 21.9,
    "error": null,
    "log_tail": "last lines of the log"
  }
]
```

- `position` is the place in the queue for queued jobs (1 = next), 0 otherwise.
- `progress` is set only while a job runs, otherwise `null`. It comes from the model's own output (app/progress.py).
  `stage` is a short label, `fraction` is the share of the whole job done (0-1), and `eta_s` is the seconds left.
  `eta_s` blends the live rate with the median time of finished jobs with the same settings. Each field is `null`
  until the output shows it, and `eta_s` is `null` when the job has run past every estimate.
- `waiting` (a text) is set only on the first queued job while it waits for prompt help or chat answers to finish
  before it starts.
- `elapsed` counts from `started` while the job runs; after it, it is the time the job took.
- `log_tail` holds the last 25 lines of `job.log`. While the job runs, it is read from the file on each request.
  It leaves out the report that `/usr/bin/time -l` writes after each command; `peak_gb` comes from that report.
- Only one job runs at a time (each model needs most of the memory of a 32-48 GB Mac).
- `kind: "text"` is a `.txt` output: the transcript of a `voice.transcribe` job.
- A job that an agent chat queued has `"chat": "<chat id>"` in its `job.json`; a job from the MCP server has
  `"chat": "mcp"`. `GET /api/jobs` lists the job like any other job, without this key.

`GET /api/jobs?limit=<1 to 100>` returns the queued and running jobs, then the newest finished ones, up to `limit` jobs
in all, newest first. `GET /api/jobs/{id}` returns one job. Both add `"chat"` (the chat id, `"mcp"` or `null`) to each
job. An unknown id is HTTP 404. The agent's `list_jobs` and `job_status` show these lists.

`POST /api/jobs/{id}/cancel` cancels a queued or running job and returns `{"status": "cancelled"}`, or the job's
status when it has already ended. `DELETE /api/jobs/{id}` removes a finished job from the list and deletes its folder
(HTTP 400 while the job is queued or running). Both return HTTP 404 for an unknown id.

`POST /api/jobs/{id}/move` takes JSON `{"to": "up" | "down" | "top" | "bottom"}` and moves a queued job one place up
or down, or to the first or last place of the queue. It returns `{"position": 2}`, the job's new `position`. A move
past an end changes nothing. Errors: HTTP 404 for an unknown job, HTTP 409 for a job that is not queued, HTTP 422
for another `to`.

- The queue order is the `order` number in each `job.json`. A new job goes last. A job without `order` (made by an
  older version) sorts before the others, by `created`.
- The server changes the order under the same lock the worker uses to pick the next job. When a move puts another job
  first while the first job waits for prompt help or chat answers, the worker starts the new first job instead.

`GET /files/{id}/{name}` serves an output file.

## Library

`GET /api/library` returns every output of the finished (`done`) jobs, newest job first:

```json
[
  {
    "job": "20261023-231500-ab12",
    "tool": "video.t2v",
    "tool_label": "Text to Video",
    "kind": "image | audio | video | text",
    "name": "video.mp4",
    "url": "/files/20261023-231500-ab12/video.mp4",
    "note": "",
    "size": 4194304,
    "created": "2026-10-23T23:15:00",
    "finished": "2026-10-23T23:19:40",
    "elapsed": 278.4,
    "peak_gb": 31.2,
    "text": "the job's prompt, script or description",
    "params": {"prompt": "...", "size": "704x1280"},
    "path": "/Users/me/mlx-media-workstation/outputs/20261023-231500-ab12/video.mp4",
    "width": 704,
    "height": 1280,
    "duration": 5.04
  }
]
```

- The list holds only the files in a job's `outputs`, never its inputs (`in_*` files). A file that was removed outside
  the app, or that is a symbolic link, is left out.
- `size` is in bytes. `text` is the first non-empty one of the `prompt`, `text` and `caption` params, else `""`.
- `width` and `height` are set for images and videos, `duration` (seconds) for audio and videos; each is `null` when
  ffprobe does not report it. The server reads the size of a PNG and the length of a PCM or float WAV from the file's
  header; it runs ffprobe for other files. It reads each file once and keeps the result in memory until the file
  changes.

`GET /api/library/search?q=<text>&limit=<1 to 100, default 10>` returns the outputs whose text, tool or file name
contains `q` (1 to 200 characters, any case), newest first, as `{"job", "tool", "tool_label", "kind", "name",
"created", "text"}`. The agent's `search_library` shows this list.

`POST /api/library/reveal` takes JSON `{"job": "<job id>", "name": "<file name>"}` and shows the file in Finder
(`open -R`). It returns `{"status": "shown"}`. The file must be a listed output of that job: HTTP 404 for any other
name, a symbolic link or an unknown job, HTTP 400 for an input file, and HTTP 409 while the job is queued or
running. HTTP 500 when Finder cannot show the file.

`DELETE /api/library/{job}/{name}` deletes one output file and removes it from the job's `outputs`. It returns
`{"status": "deleted"}`. HTTP 400 for an input file, HTTP 409 while the job is queued or running, HTTP 404 for any
other name. The job stays in the job list, also when it has no outputs left.

## System

`GET /api/system` returns `{"mem_total_gb": 48.0, "mem_free_gb": 37.5, "swap_used_gb": 2.8, "running": "job id or null", "queued": 1, "profile": "full"}`.

`profile` is what the UI shows: `"full"` (every section) or `"chat"` (Chat, Models and Settings). It is `[app] profile`
in `config.toml`, or `"chat"` when the app was started with `start.py --chat`. A change in Settings applies at once.
The profile changes only the UI: every endpoint works in both profiles, the job endpoints too. `start.py` also reads
this endpoint when the port is in use, to find out whether the Workstation runs there and in which profile.

`GET /api/system?nonce=<32 to 128 lowercase hex digits>` adds `"proof"`: the hex HMAC-SHA256 of the nonce with the
server's token as the key. At start the server writes a new random token to `outputs/.server-token` (mode 0600,
an atomic write) and removes it at a clean shutdown. `start.py` reads the file, sends a random nonce and opens a
window only when the proof matches, so another program on the port cannot get a window. The token itself is never
sent. Without the session cookie the answer has only `proof` and `profile`. A bad nonce is HTTP 400.

`GET /api/status` returns what the `system_status` tool shows (`app/server.py`, `Workstation.system_status`):

```json
{
  "memory": {"mem_total_gb": 48.0, "mem_free_gb": 37.5, "swap_used_gb": 2.8, "reserve_gb": 8.0},
  "running": {"id": "20261023-231500-ab12", "tool": "video.t2v", "tool_label": "Text to Video", "status": "running",
              "created": "2026-10-23T23:15:00", "position": 0, "waiting": null,
              "progress": {"stage": "Stage 2: 1/3", "fraction": 0.52, "eta_s": 95}, "elapsed": 42.5, "chat": null},
  "queue": [],
  "chat_model": {"label": "Qwen3.8 27B (4-bit)", "installed": true, "state": "off | loading | ready", "busy": false,
                 "peak_gb": 18.0, "held_gb": 0.0},
  "tools": [{"id": "image.klein", "label": "FLUX.3 Klein", "installed": true, "peak_gb": 14.0, "fit": "fits"}],
  "profile": "full"
}
```

- `memory` has the numbers of `GET /api/system` and `reserve_gb`, the memory a tool leaves for macOS.
- `running` is the running job or `null`. `queue` has the queued jobs in the order they run. Each job has the keys
  above, with the values of `GET /api/jobs`.
- `held_gb` is the chat model's peak while it is loaded or loading, else 0.
- `fit` uses the queue's rule (`models.fits`, `llm.fits_with`): `"fits"` (the tool fits next to the chat model, or
  the model is off), `"unloads_chat_model"` (the job unloads the model before it starts) or `"too_big"` (its peak
  plus `reserve_gb` is more than `mem_total_gb`).

## Models

`GET /api/models` returns every installable component, its install status and this Mac's memory:

```json
{
  "machine": {"mem_total_gb": 48.0, "mem_reserve_gb": 8.0},
  "models": [
    {
      "id": "ltx-2.5-mlx-q8",
      "label": "LTX-2.5 (q8)",
      "kind": "download | manual",
      "used_by": ["Text to Video", "Image to Video"],
      "tools": ["video.t2v", "video.i2v"],
      "features": [],
      "repo": "dgrauet/ltx-2.5-mlx-q8",
      "revision": "746ca9aacb697d2c739f544d68b584214dedcc75",
      "size_gb": 54.1,
      "licence": "LTX-2 Community License",
      "licence_url": "https://huggingface.co/...",
      "peak_gb": 35.0,
      "gated": true,
      "path_key": "ltx_model",
      "path": "/Users/me/mlx-media/models/ltx-2.5-mlx-q8",
      "installed": false,
      "fits": true,
      "disk_free_gb": 412.3,
      "needs": null,
      "install": null,
      "install_path": null,
      "download": {"status": "queued | running | done | failed | cancelled", "position": null, "error": null,
                   "done_gb": 12.4, "total_gb": 54.1, "percent": 22.9, "speed_mbs": 85.2, "eta_s": 489,
                   "idle_s": 0, "stalled": false}
    }
  ],
  "queue": {
    "running": "ltx-2.5-mlx-q8",
    "queued": ["qwen3.8-27b-4bit"],
    "count": 2,
    "left_gb": 57.8,
    "batch": ["flux-3-image-klein", "ltx-2.5-mlx-q8", "qwen3.8-27b-4bit"],
    "batch_id": 3,
    "disk_margin_gb": 10.0
  }
}
```

- `kind: "download"` entries are weights the app downloads. `kind: "manual"` entries are model environments and
  checkouts: `install` holds the README commands, and `repo`, `size_gb`, `peak_gb`, `fits` and `disk_free_gb` are
  `null`.
- `features` lists the app features outside the tool list that need the entry (`"Prompt help"`, `"Chat"`). The chat
  profile shows only the entries whose `features` hold `"Chat"`. `used_by` is the tool labels plus `features`.
- `path_key` is the `config.toml` key of the install path; `null` means the Hugging Face cache.
- `install_path` is where the `install` commands put a manual entry: the default path of `path_key`. When it differs
  from `path`, the user sets `path_key` to it after the install. It is `null` for downloads.
- `fits` is true when `peak_gb` plus `mem_reserve_gb` is at most `mem_total_gb`.
- `needs` names an entry to install first. `download` is `null` unless this model is queued, downloading, or was
  downloaded since the app started. A queued entry has `status: "queued"` and `position` (1 = it starts next); its
  other numbers are 0 or `null`. `position` is `null` in every other status. A failed or cancelled download keeps its
  `error` until the model is queued again.
- `download.done_gb` counts the bytes the download has received or written (as the download reports them), or the
  bytes on disk when that is more. Xet writes files in large blocks, so the bytes on disk alone can stand still while
  data arrives.
- `download.speed_mbs` is the speed in MB/s (10^6 bytes), averaged over at least the last 10 s of polls, and
  `download.eta_s` the seconds left at that speed. Both are `null` until the download has run for about 2 s, and when
  it is not running; `eta_s` is also `null` while the speed is 0. The speed is 0 only when no byte arrived in that
  window.
- `download.idle_s` is the seconds since the bytes last grew (`null` when the download is not running), and
  `download.stalled` is true when `idle_s` is 60 or more.
- Sizes are in GB of 10^9 bytes (as Hugging Face shows them); memory is in GB of 2^30 bytes (as `/api/system`).

`queue` describes the download queue:

- `running` is the id of the running download, or `null`. `queued` lists the waiting ids in the order they start.
- `count` is the running and the queued downloads together. `left_gb` is what they still have to download: the Hub
  sizes minus the files on disk, or the catalog `size_gb` before the download has asked the Hub.
- `batch` lists every entry of the current run of downloads in order: the running, the queued and the finished ones.
  A new run (with a higher `batch_id`) starts when models are queued while nothing downloads.
- `disk_margin_gb` is the free space the queue keeps on each disk.

One download runs at a time. When it ends (done, failed or cancelled), the next queued one starts. A failed download
does not stop the queue. An entry whose `needs` entry is not installed when its turn comes fails with a message.

`POST /api/models/queue` with `{"ids": ["flux-3-image-klein", "ltx-2.5-mlx-q8"]}` adds downloads in that order and
starts the first one if nothing runs. Each entry goes after the entry it `needs`. Installed, running and queued ids are
skipped. It returns `{"added": [...], "skipped": [...], "queue": {...}}` (`queue` as in `GET /api/models`). It queues
nothing and returns HTTP 400 with a message when:

- `ids` is empty, or names an unknown id or a manual entry (the message names them);
- an entry needs a manual entry that is not installed, or a download entry that is neither installed, queued nor in
  `ids`;
- the downloads (with the running and queued ones) would leave less than `disk_margin_gb` free on a disk.

The server cannot know if the licence of a gated model was accepted on Hugging Face. The UI asks first; a refused
download fails with the reason in `download.error`.

`POST /api/models/queue/clear` takes every queued download out of the queue (they become `cancelled`) and leaves the
running one. It returns `{"removed": [...ids], "queue": {...}}`.

`POST /api/models/{id}/download` queues one download (the same rules as above) and returns
`{"status": "running", "position": null}` when it started, or `{"status": "queued", "position": 2}` when it waits.
Asking again for a running or queued model returns its status. Errors return HTTP 400 with `{"detail": "message"}`
(a manual entry, already installed, a missing need, not enough disk). Poll `GET /api/models` for progress; a failed
download has the reason in `download.error`.

`POST /api/models/{id}/cancel` stops the running download, or takes a queued one out of the queue, and returns
`{"status": "cancelled"}` (or the finished status). The next queued download starts. Finished files stay, and the next
download skips them. HTTP 400 when the model is neither running nor queued.

Both `POST /api/models/{id}/...` endpoints return HTTP 404 for an unknown id.

## Prompt help

`GET /api/assist` returns `{"available": true, "provider": "local", "model": "Qwen3.8 27B (4-bit)", "llm": {...}}`.
`available` is false when the provider's model is not installed, or when it does not fit this Mac (its peak plus the
8 GB reserve is more than the Mac's memory); the UI then hides the "Improve" buttons. `llm` is the local model's
state (see `GET /api/llm`). When `config.toml` names an unknown provider, the answer is
`{"available": false, "provider": "...", "error": "message"}`.

`POST /api/assist/enhance` takes the same `multipart/form-data` as `POST /api/jobs`, plus `field`: the name of a field
with `assist: true`, and `request_id`: a random token that the client makes (1 to 64 characters of `A-Z`, `a-z`,
`0-9`, `-` and `_`; optional, but without it the request can only be cancelled by closing the connection). Image
fields go along as images; a video field goes along as the frame where the clip ends (at `end_at` when that field is
set). Audio files are not sent. It returns:

```json
{"text": "the new field text", "notes": "what the model added or assumed", "model": "Qwen3.8 27B (4-bit)",
 "cost_usd": null, "seconds": 9.4}
```

Errors (model not installed, too little memory on this Mac, an empty draft, an answer in the wrong form) return HTTP
400 with `{"detail": "message"}`. The first request loads the model (16 GB from disk); later ones reuse it until it
is unloaded. While a media job that does not fit next to the model runs, the request waits and answers when the job
ends; `GET /api/llm` shows `waiting` > 0 meanwhile.

`POST /api/assist/cancel` takes JSON `{"request_id": "..."}` and cancels that request while it waits for memory, loads
the model or writes. It returns `{"status": "cancelling"}`, or `{"status": "idle"}` when no request with that id runs
(it ended, the id is unknown, or the server is still reading the request's upload). A cancel that comes after the
upload but before the request has reached the model is kept and applied when it does, so the model does not load for
it. The cancelled `POST /api/assist/enhance` returns HTTP 409 with `{"detail": "Cancelled."}`, also when its answer
was ready at the same moment; errors stay HTTP 400. A request whose client closes the connection (the page was closed,
or the fetch was aborted) is cancelled the same way, so a client should abort its fetch after the cancel call. Two
running requests cannot use the same `request_id` (HTTP 400).

## Local LLM and chat

`GET /api/llm` returns the local model's state:

```json
{"installed": true, "state": "ready", "busy": false, "waiting": 0, "media_peak_gb": 0.0,
 "model": "Qwen3.8 27B (4-bit)", "peak_gb": 18.0, "idle_stop_s": 600.0, "default_effort": "off"}
```

- `state` is `off`, `loading` or `ready`. The model loads on the first request and unloads after `idle_stop_s`
  without use (`null`: never; Settings > Jobs sets it), or before a media job that does not fit next to it.
- `waiting` counts requests that wait for such a job to end. They run before the next job starts; that job waits
  for them at most 10 minutes, then it stops the model and the answer ends with an error.
- `media_peak_gb` is the peak memory of the job that holds the memory now (0 when none).
- `default_effort` is the thinking level of a new chat (`[app] chat_effort`, Settings > Local LLM). See
  [Thinking levels](#thinking-levels).

`POST /api/llm/unload` stops the model and returns `{"status": "off"}`.

### Python interface for server code (`app/llm.py`)

Chat and prompt help call the model through these functions; no HTTP endpoint exposes them directly.

```python
llm.generate(messages, images=(), *, thinking=False, schema=None, max_tokens=4096, chat=None,
             tools=None, tool_choice=None, reasoning_effort=None, thinking_budget=None, allow_emoji=False,
             session=None) -> Iterator[dict]
llm.complete(messages, images=(), on_start=None, **same_keywords) -> dict   # the "done" event; LLMError on error
llm.cancel(request_id) -> bool
with llm.session(abort=lambda: False, limit=600.0) as s: ...
```

- `generate` yields `{"event": "started", "id": "r12"}`, `{"event": "waiting", "message": "..."}` (only while a
  media job holds the memory), `reasoning` and `content` deltas (`{"event": "content", "text": "..."}`), and at the
  end one `done` or `{"event": "error", "message": "..."}`. Closing the iterator early cancels the request.
- `done` is `{"event": "done", "finish": "stop" | "length" | "cancelled" | "tool_calls", "content": "...",
  "reasoning": "...", "stats": {...}}`, plus `"tool_calls": [...]` when the request had `tools`.
- `chat` keeps the prompt cache of that chat in the worker, so a request that extends the previous prompt reads only
  the new tokens. Use the same `chat` for every step of a tool turn.
- `reasoning_effort` is `"low"`, `"medium"` or `"xhigh"` (the Qwen3.8 template's default). It applies only with
  `thinking=True`. Another value raises `ValueError`: the template itself fails on any other value.
- `thinking_budget` (a positive whole number, or `None` for no limit) applies only with `thinking=True`. After that
  many thinking tokens the worker makes the model end its thinking (mlx-vlm forces `</think>`), so it must answer.
  Keep `max_tokens` larger than the budget, or the answer has no room. The `done` stats then have
  `thinking_tokens`. `app/effort.py` picks these values for a chat answer.
- `tool_choice` is `None` or `"none"`. With `"none"` the tools stay in the prompt (so the cached start of the prompt
  stays the same), but the worker bans the call marker, so the model answers with text. Another value raises
  `ValueError`.
- `allow_emoji=False` (the default) makes the worker ban emoji tokens. It never bans them inside a tool call's
  arguments (from the call marker to its end), where an emoji can be data.

**Tools.** `tools` is a list of OpenAI-style function schemas:

```json
{"type": "function", "function": {"name": "get_weather", "description": "...",
 "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
                "required": ["city"]}}}
```

- The worker passes `tools` to the chat template. The model writes a call as
  `<tool_call><function=NAME><parameter=P>value</parameter></function></tool_call>`.
- The `content` deltas never contain that markup. The worker shows the text before the first `<tool_call>` and holds
  back the rest, also a partial marker at the end of a chunk (`<tool`). Text that only looks like a marker
  (`<toolbox>`) is shown when it cannot be one any more, at the latest at the end of the answer.
- `done.tool_calls` is a list of `{"id": "call_3f2a...", "name": "get_weather", "arguments": {"city": "Ankara",
  "days": 2}}`. mlx-vlm's parser for the model (`qwen3_coder`) parses each call and converts each argument to the
  type in the schema. With one or more calls, `finish` is `"tool_calls"`; without, the list is empty.
- A call that does not parse is still in the list, with `"arguments": {}`, an `"error": "Invalid tool call: ..."` and
  its `"raw"` text. A call cut off by `max_tokens` gets an error too. The worker goes on with the next request.
- The tool name is not checked against `tools`; the caller validates the name and the arguments.
- `content` in `done` is the answer without call markup. Text the model wrote after its calls (the template asks for
  none) is sent as a last `content` delta and is in `content`.
- A cancelled answer has `"tool_calls": []`, so nothing it started can run.
- Without `tools`, nothing changes: the deltas are the model's text as it is.

**History with tools.** `messages` may contain:

```json
{"role": "assistant", "content": "", "reasoning_content": "...",
 "tool_calls": [{"id": "call_3f2a...", "name": "get_weather", "arguments": {"city": "Ankara"}}]}
{"role": "tool", "tool_call_id": "call_3f2a...", "name": "get_weather", "content": "9 C, rain"}
```

- The calls of a `done` event can go back unchanged. OpenAI's form `{"id", "type": "function", "function": {"name",
  "arguments": "<JSON string>"}}` works too: the arguments become a dict, because the template renders them with
  `|items`. Arguments that are not a JSON object become `{}`.
- The Qwen3.8 template renders a tool message from its `content` and position only (consecutive tool messages
  become one user turn of `<tool_response>` blocks). Keep tool messages right after their assistant message, in call
  order. Content that is not a string or a list of text parts is sent as JSON.

**Session (memory lease).** A tool turn runs several requests with tool work between them. Between two requests no
answer runs, so a queued media job could claim the memory and unload the model in the middle of the turn.
`llm.session()` prevents that:

1. Open `with llm.session(abort) as s:` around the whole turn, in the thread that calls `generate`. Requests in that
   thread use the session; from another thread, pass `session=s`.
2. The lease starts at once when the memory is free. When a media job holds the memory, it starts with the first
   request of the session that gets the memory after the job.
3. While the lease holds, a media job that does not fit next to the model waits for it, as it waits for a running
   answer. The session's requests skip the memory check, so they never wait for that job.
4. The lease ends when the block ends, when `abort()` returns `True`, or after `limit` seconds (default 600, the same
   as a job's wait). After that, requests in the block wait for memory like any other request.
5. `close()` ends every lease. The idle unload does not stop the model while a lease holds.

`abort` runs with the module lock held: it must be fast and must not call `app.llm`. Cancel a request of a session
with `llm.cancel(request_id)` as usual. Do not wait for a media job inside a session: a big job waits for the lease,
so both would wait until the time limit.

Chats are stored in `outputs/chats/<id>/` (`chat.json` and the images sent in the chat). The endpoints with `{id}`
return HTTP 404 for an unknown chat.

- `GET /api/chats` lists the chats, most recently updated first: `id`, `title`, `created`, `updated`, `messages`
  (count), `mode`, `running`.
- `POST /api/chats` creates an empty chat and returns its summary. An optional JSON body `{"mode": "general"}` sets
  the mode (default `workstation`). HTTP 400 for an unknown mode.
- `GET /api/chats/{id}` returns the chat with all messages, `mode` and `running`.
- `PATCH /api/chats/{id}` with the JSON body `{"mode": "..."}` changes the mode of a chat that has no messages yet
  and returns its summary. HTTP 409 after the first message, HTTP 400 for an unknown mode.
- `DELETE /api/chats/{id}` deletes it (HTTP 409 while it answers). The folder is renamed to a hidden trash name
  first, so the chat is gone at once, then removed; immutable or locked files that sandboxed code made are removed
  too.
- `GET /api/chats/{id}/files/{name}` serves an image of the chat: one sent in it (`img_N.png`, `.jpg` or `.webp`) or
  a copy that `view_image` made for the model (`view_<12 hex digits>.png`). A symlink gives HTTP 404.
- `POST /api/chats/{id}/messages` takes `multipart/form-data`: `text`, `effort` (a thinking level: `off`, `low`,
  `medium`, `high`, `xhigh` or `max`; see [Thinking levels](#thinking-levels)), up to 4 `images` (PNG, JPEG or
  WebP, 20 MB each), or `regenerate=true` to answer the last user message again. The older field `thinking` still
  works: `true` is the level `high`, any other value is `off`; `effort` wins when both are sent. Without both, the
  chat's last level applies, or `[app] chat_effort` for a chat that has none. The chat saves the level it used as
  `effort` (`GET /api/chats/{id}`), so Regenerate and the page use it again. It returns server-sent events
  (`data: {...}` lines):
  - `{"event": "waiting", "message": "..."}` while a media job holds the memory;
  - `{"event": "reasoning", "text": "..."}` and `{"event": "content", "text": "..."}` deltas;
  - `{"event": "done", "finish": "stop" | "length" | "cancelled", "content": "...", "reasoning": "...", "stats": {...}}`
    or `{"event": "error", "message": "..."}` at the end.
  - An agent chat has more events: see [Agent mode](#agent-mode).
  - `stats` has `seconds`, and when the model ran, `prompt_tokens`, `generation_tokens`, `cached_tokens`,
    `prompt_tps`, `generation_tps` and `peak_gb`.
  - `text` is at most 20,000 characters. An answer is at most 4,096 tokens after its thinking (see
    [Thinking levels](#thinking-levels)).
  - Errors before the stream starts: HTTP 400 with `{"detail": "message"}` (an empty message, a text that is too
    long, too many images, an image of another type or over 20 MB, nothing to answer again, an unknown `effort`),
    HTTP 409 while the chat still answers.
- `GET /api/chats/{id}/stream` replays the running answer's events from the start (for a page that came back); HTTP
  404 when nothing runs.
- `POST /api/chats/{id}/stop` stops the answer between tokens, also while it waits. It returns
  `{"status": "stopping"}`, or `{"status": "idle"}` when nothing runs. The reply is saved with `finish: "cancelled"`.

The mode of a chat sets its system prompt:

| Mode | System prompt | "Use in" cards |
|---|---|---|
| `workstation` | The app's tools with their prompt-help fields, and the `prompt` block rule below. | yes |
| `general` | A short assistant prompt with today's date. It does not list the tools. | no |
| `agent` | The `general` prompt without the "no internet" line, plus the agent rules and the granted folder. The model gets the chat's tools (see [Agent mode](#agent-mode)). | no |

Every mode tells the model to answer in the user's language and not to use emoji. The LLM worker also bans emoji
tokens in every answer ([AGENT.md](AGENT.md#how-the-chat-loop-uses-the-package)). A chat saved before modes existed has
no `mode` in `chat.json` and is a `workstation` chat. The mode is fixed after the first message, because a different
system prompt changes what the earlier answers meant. The context budget counts the chat's own system prompt.

A text the model writes for a tool comes in a fenced block whose info string is `prompt`, the tool id and, for a tool
with several prompt-help fields, the field name (`prompt video.t2v`, `prompt music.ace lyrics`). In a workstation
chat the UI shows a "Use in <tool>" button on such a block; without a field name it fills the tool's first `assist`
field.

The answer runs on the server whether or not a page listens, and it is saved when it ends. The model sees the newest
turns that fit in 32K tokens.

### Thinking levels

A chat message has a thinking level: how long the model may think before it answers. `app/effort.py` holds the
table. The model writes about 17 tokens a second on an M5 Pro, so 1,000 tokens are about 1 minute. An answer may
take at most 10 minutes, in every mode. At the limit the answer stops with `finish: "timeout"` and a `note`, and the
saved message keeps both. The time that the answer waits for memory (a media job runs) does not count.

| Level | Template effort | Thinking budget (tokens) | `max_tokens` | Thinking time, at most |
| --- | --- | --- | --- | --- |
| `off` | thinking off | none | 4,096 | none |
| `low` | `low` | 1,024 | 5,120 | about 1 minute |
| `medium` | `medium` | 3,072 | 7,168 | about 3 minutes |
| `high` | `xhigh` | 6,144 | 10,240 | about 6 minutes |
| `xhigh` | `xhigh` | 8,192 | 12,288 | about 8 minutes |
| `max` | `xhigh` | no limit | the free context, at least 12,288 | until the answer's time limit |

- The template effort goes to the Qwen3.8 chat template, which knows only `low`, `medium` and `xhigh`. The levels
  `high`, `xhigh` and `max` differ only in the budget.
- When the thinking reaches the budget, the model must stop thinking and answer. `max_tokens` is the budget plus
  4,096, so the answer always has room.
- `max` has no budget. Its `max_tokens` is the context (32,768 tokens) less the prompt, at least 12,288. The chat
  history that the model sees is the same as with `xhigh`.
- In an agent answer, the budget applies to each step. The first step gets the full budget. Each later step gets 40%
  of it (`high`: 6,144, then 2,457 a step). The template effort is the same in every step: it is part of the system
  prompt, and a change would make the model read the whole chat again.
- `thinking=true` of the older API is the level `high`.

### Agent mode

An agent chat can call tools: the Workstation tools, the file tools (read and write), the media tools, the web tools,
`run_python` and `run_shell`. `docs/AGENT.md`
describes each tool, its risk level and when it asks. An answer is a loop of steps. Each step is one model request.
When the model calls tools, the server runs them (or asks the user first) and sends the results back in the next step.

The chat's agent state is in `chat.json` under `agent`:

```json
{"tools": ["update_plan", "list_workstation_tools", "job_status", "search_library", "create_job", "list_jobs",
           "wait_for_job", "cancel_job", "transcribe_audio", "system_status", "list_files", "read_file",
           "search_files", "write_file", "edit_file", "copy_to_workspace", "export_from_workspace", "media_info",
           "view_image", "web_search", "fetch_url"],
 "folder": null, "grants": [], "tainted": false}
```

- `tools` are the tools this chat offers. A new agent chat has every tool except `run_python` and `run_shell`. They
  run code and commands that the model writes, so the user turns them on in the Tools menu.
- `folder` is the folder that the file tools may use, or `null`. The tools that read or write the user's files are
  offered only while a folder is granted. `media_info` and `view_image` are offered without one: they also read the
  chat's work folder and the Library.
- `grants` are the tools the user allowed "in this chat".
- `tainted` is `true` after a tool returned untrusted text (a page, a file, program output). From then on every
  network or job call asks again.

Endpoints. Like every endpoint they need the session cookie, so a program on this Mac that cannot read the token
file cannot approve calls or grant folders. The writes also need an `Origin` header of the app's own page (HTTP 403
with another one).

- `GET /api/chats/{id}/agent` returns the state for the Tools menu: `folder`, `grants`, `tainted`, `enabled` (the
  names), `limits` (`steps`, `seconds`, `approval_s`) and `tools`, a list of `{name, label, description, risk,
  risk_text, network, enabled, granted}` for every tool this app has. HTTP 409 for a chat that is not an agent chat.
- `PATCH /api/chats/{id}/agent` changes it and returns the new state. The JSON body has any of:
  - `tools`: the names of the enabled tools (HTTP 400 for an unknown name);
  - `folder`: a folder to grant, or `null` or `""` to revoke it;
  - `revoke`: tools whose "allow in this chat" ends.
  The changes apply from the next tool call, also while an answer runs. The folder must be an absolute path to an
  existing folder. The server stores its real path as the disk spells it (`F_GETPATH`). It refuses `/`, a folder
  directly under `/`, the home folder and the folders above it, system folders (`/System`, `/Library`, `/usr`,
  `/private/etc`...), `~/Library`, `/private/var` and `/private/tmp` (also as `/var` and `/tmp`), other users' folders
  under `/Users`, the app's own folder and the folders that hold it, everything inside these, and any path with a
  hidden part (`.ssh`). Each
  refusal is HTTP 400 with the reason.
- `POST /api/chats/{id}/approve` with `{"call_id": "call_3f2a...", "decision": "once" | "chat" | "deny"}` answers
  the call that waits. It returns `{"status": "answered", "decision": "..."}`, and the call then runs with that
  answer, also when the wait timed out in the same moment. HTTP 409 when that call does not wait any more (the
  `approval_done` event was sent), HTTP 400 for `"chat"` when the approval did not offer it.
- `POST /api/chats/{id}/reveal` with `{"path": "notes/plan.md"}` shows a file of the granted folder in Finder (`open
  -R`). The page uses it for the files that `write_file`, `edit_file` and `export_from_workspace` wrote. The path gets
  the file tools' checks. It returns `{"status": "shown"}`. HTTP 409 without a granted folder, HTTP 404 for a path
  outside the folder, a hidden or secret part or a missing file, HTTP 500 when Finder fails.
- `GET /api/chats/{id}/ws/{path}` serves a file that `run_python` or `run_shell` wrote in the chat's workspace
  (`outputs/chats/<id>/ws`). PNG, JPEG, GIF and WebP come as images. `.txt`, `.csv`, `.tsv`, `.json`, `.md`, `.py`
  and `.log` come as `text/plain`. Every other file is a download (`Content-Disposition: attachment`). Each response
  has `Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`. Symlinks, hidden names, files
  that are not regular files (a FIFO too) and files over 64 MB give HTTP 404.

Events. The message stream of an agent chat has these events besides `waiting`, `reasoning`, `content` and `error`:

- `{"event": "step", "n": 2, "max": 12, "last": false}` before each model request. `last` is `true` for the last
  step, which cannot call tools.
- `{"event": "tool_call", "id", "name", "label", "arguments", "describe", "risk", "network"}` for each call the model
  made. `describe` is one line about what the call does, with bidi, zero-width and control characters written as
  `\uXXXX`. `risk` is 0 to 3, or `null` for an unknown tool. `network` says whether the tool sends data off this Mac
  (`null` for an unknown tool): the page labels a local R2 tool "Uses this Mac". `arguments` are as the model sent
  them: the page writes those characters as `\uXXXX` when it shows them.
- `{"event": "approval", "id", "name", "label", "arguments", "describe", "host", "reason", "risk", "network",
  "chat_scope", "timeout_s", "preview"}` when the call waits for the user. Offer "Allow in this chat" only when
  `chat_scope` is `true`. `host` is the host that `fetch_url` contacts, as the network sees it (IDNA: `xn--...` for a
  non-ASCII name), or `null` for other tools and for a URL without a valid host. The page shows it on its own line.
  `network` says whether the tool sends data off this Mac. `preview` is the change as a unified diff (`edit_file`,
  `write_file`), at most 400 lines, with hiding characters written as `\uXXXX` and tabs kept, or `null`. `edit_file`
  refuses a change whose diff is longer, so its card always shows the whole change. The page
  shows each line as text, never as HTML. For `run_shell` the event also has `cwd` (the folder the command runs in:
  the chat's work folder) and `read_only` (the granted folder that the command can read, or `null`). The page shows
  the command in a code block and both folders.
- `{"event": "approval_done", "id", "decision"}` with `once`, `chat`, `deny`, `timeout` or `stopped`.
- `{"event": "tool_result", "id", "name", "status", "content", "truncated", "files", "seconds", "approval", "risk",
  "network"}`, plus `"job": "<job id>"` when the call queued or read a media job (`create_job`, `transcribe_audio`).
  `status` is `ok`, `error`, `cancelled`, `denied`, `refused`, `skipped` or `repeat` (the same call a second time
  after an `ok` result: it did not run). `content` is the first 4,000 characters. `files` are `{path, url, kind}`.
  `kind` is `image` or `file` for a file in the workspace (`url` under `.../ws/`), `image` for a `view_image` copy
  (`url` under `.../files/`), or `saved` for a file written in the granted folder (`path` relative to it, `url` empty:
  the page shows it with a "Show in Finder" button). The page shows a card for `job` with the job's live status from
  `GET /api/jobs`, a link to the Jobs view and the outputs when the job is done.
- `{"event": "plan", "id", "items": [{"step", "status"}]}` after an `update_plan` call that ran, before its
  `tool_result`. `status` is `pending`, `in_progress` or `done`. Each event has the whole plan: the page replaces the
  checklist of the answer and shows no card for such a call.
- `done` as above, plus `note` (why the answer ended early, or `""`). `finish` can also be `steps` (the step limit:
  `content` is then the model's summary from the last step, which could not call tools), `repeat` (the same call a
  third time) or `timeout` (the time limit). `stats` adds `steps`, `tool_calls` and `generation_tokens` over all
  steps; `seconds` is the whole answer.

`GET /api/chats/{id}/stream` replays these events too. A page that comes back while a call waits sees its `approval`
event without an `approval_done`, so it can show the card again. `POST /api/chats/{id}/stop` also answers a waiting
call with `stopped`.

Rules of the loop (`app/chat.py`, `Turn`):

1. All steps run in one `llm.session()`, so a queued media job does not unload the model between steps.
2. An answer has at most 12 steps and 10 minutes. The time a step waits for memory (a media job runs) does not count,
   and the memory lease's limit counts from the moment it first holds the memory. Step 12 cannot call tools: it keeps
   the tool schemas (the prompt's start stays the same), gets a user message from the app that says so, and asks the
   worker with `tool_choice: "none"`, which bans the call marker. So the answer ends with a summary. A call that comes
   anyway does not run.
3. Stop is checked before each step, before each call, while a call waits for approval and inside the tools.
4. A malformed call, an unknown or disabled tool, a refusal and a denial each give the model a tool message. The
   answer goes on.
5. The same call (name and arguments) a second time in one answer, after the first one returned `ok`, does not run:
   its tool message (`status: "repeat"`) says that the call already ran and its result is above. A third time ends
   the answer with a note. The rule does not apply to the tools whose result can change (`job_status`, `list_jobs`,
   `wait_for_job`, `transcribe_audio`, `system_status`, `search_library`, `list_files`, `read_file`, `search_files`,
   `media_info`, `view_image`, `fetch_url`). After a call that changes state ran (a tool that writes files, `run_python`,
   `run_shell`, `copy_to_workspace`, `create_job`, `cancel_job`, `transcribe_audio`), the rule forgets the earlier
   calls; that call itself still counts.
6. An approval waits at most 10 minutes (less when the answer's time runs out). No answer counts as a denial.
7. `create_job` queues the job and returns its id at once. `wait_for_job` and `transcribe_audio` wait only for a job
   that fits next to the model. A job that needs the model's memory (or a job behind one in the queue) waits for the
   answer's memory lease, so the wait returns at once and says that the job starts after the answer. A wait takes at
   most 300 s, ends at Stop and leaves the answer's last 60 s for its next steps. It counts against the 10 minutes.
8. Every call of a step gets a tool message, also when the answer ends before the call runs.
9. When the app closes, every running answer gets Stop, as with the Stop button. The server waits a few
   seconds for the answers to save what they have.
10. Before a call asks, the loop makes the tool's preview (`edit_file`'s diff) from the files as they are. When the
    call cannot run (invalid arguments, `old_text` not found, a file that exists), the error goes back to the model
    at once and the user is not asked. After the approval, `edit_file` makes the diff again and writes nothing when it
    differs from the one the user saw.
11. A `view_image` result names a copy of the image in the chat's folder. The tool message keeps it in `images`, and
    the next model requests get it as an image part before the result text. Only the newest 2 tool images go to the
    model; an older result gets a note instead.

The answer's messages are saved when it ends. The assistant messages keep their `tool_calls`. The tool messages are
`{"role": "tool", "tool_call_id", "name", "label", "status", "content", "describe", "files", "seconds", "approval",
"risk", "time"}`, plus `images` for a `view_image` result and `job` as in `tool_result`, with `content` cut at
100,000 characters. An `update_plan` message also has `plan` (its items), so a saved answer shows its last plan. The
message of the last step has `nudge`, the app's note that the model got as a user message before it; the next turns
send it in the same place. The model gets at most 12,000 characters of each result.
When the history does not fit the context budget, old tool results are shortened first, oldest first, and then the
oldest turns are dropped, until the history fills 60% of the budget. The budget counts the system prompt and the tool
schemas. `chat.json` then stores the cut as `window`: `{"start": n, "short": n}`, indexes into `messages`. The model
input leaves out the turns before message `start` and shortens the tool results before message `short`, so the next
turns extend the same prompt and the worker's prompt cache holds. A chat in any mode can have `window`. The
results of the newest step always go in whole. Regenerate removes `window`. The window does not move back when the
budget grows (a lower thinking level, fewer tools): turns that would fit again stay out until the next regenerate.


## Settings

`GET /api/settings` returns what the Settings screen shows:

```json
{
  "config": {"path": "/Users/me/mlx-media-workstation/config.toml", "exists": true, "error": null},
  "paths": [
    {
      "key": "ltx_model",
      "label": "LTX-2.5 weights",
      "value": "~/mlx-media/models/ltx-2.5-mlx-q8",
      "default": "~/mlx-media/models/ltx-2.5-mlx-q8",
      "in_file": true,
      "path": "/Users/me/mlx-media/models/ltx-2.5-mlx-q8",
      "exists": true,
      "active": "/Users/me/mlx-media/models/ltx-2.5-mlx-q8",
      "used_by": {"models": ["LTX-2.5 (q8)"], "tools": ["Text to Video", "Image to Video"], "features": []}
    }
  ],
  "assist": {
    "provider": "local", "active": "local", "default": "local",
    "providers": [{"id": "local", "label": "Local LLM", "description": "...", "available": true,
                   "model": "Qwen3.8 27B (4-bit)", "llm": {}}]
  },
  "app": {"keep_awake": true, "notify": true, "llm_idle_minutes": 10, "profile": "full", "chat_effort": "off",
          "defaults": {"keep_awake": true, "notify": true, "llm_idle_minutes": 10, "profile": "full",
                       "chat_effort": "off"},
          "idle_choices": [5, 10, 30, 60, 0], "profiles": ["full", "chat"],
          "efforts": ["off", "low", "medium", "high", "xhigh", "max"], "saved_profile": "full"},
  "llm": {"idle_stop_s": 600.0},
  "agent": {"python": "", "searxng_url": "", "wikipedia_language": "en", "marginalia": true,
            "defaults": {"python": "", "searxng_url": "", "wikipedia_language": "en", "marginalia": true},
            "python_path": null, "python_found": false, "python_problem": null,
            "default_python": "/opt/homebrew/Cellar/python@3.12/3.12.15_1/Frameworks/Python.framework/Versions/3.12/bin/python3.12",
            "languages": ["en", "tr", "de", "fr", "es", "it", "ja", "zh"], "restart_required": false},
  "about": {"version": "0.1.0", "commit": "8172e42 2026-11-02", "root": "/Users/me/mlx-media-workstation",
            "outputs": "/Users/me/mlx-media-workstation/outputs",
            "readme": "/Users/me/mlx-media-workstation/README.md",
            "repo_url": "https://github.com/0xbyt4/mlx-media-workstation"},
  "restart_required": false
}
```

- `paths` has one entry per `[paths]` key, in the order of `app/config.py`. `value` is the text in `config.toml`, or
  the default when the file does not set it (`in_file` is then false). `path` is that value with "~" expanded (a
  relative path counts from the app folder), and `exists` tells whether it is on disk. `active` is the path the
  running app uses: it differs from `path` after a change until the app restarts.
- `used_by` lists the entries of `GET /api/models`, the tools and the app features that need the path.
- `config.error` is set when `config.toml` is not valid TOML; the values are then the defaults.
- `about.commit` is `null` when git or the git checkout is not there.
- `restart_required` is true when `config.toml` holds other `[paths]`, `[assist]` or `[agent]` values than the ones
  the app read at start.
- `app` holds the `[app]` values the app uses now (Settings > Profile, Settings > Jobs and Settings > Local LLM):
  `keep_awake` (no system sleep while a job is queued or running), `notify` (a macOS notification when a job is done
  or failed; none for a cancelled job), `llm_idle_minutes` (minutes without use before the local LLM unloads; 0 =
  never), `profile` (one of `profiles`; see [System](#system)) and `chat_effort` (the thinking level of a new chat,
  one of `efforts`; see [Thinking levels](#thinking-levels)). `idle_choices` are the minutes the screen offers. After
  `start.py --chat`, `profile` is `"chat"` although `config.toml` may say `"full"`. `saved_profile` is the profile
  in `config.toml` (the default when the file does not set a valid one): the profile of the next start without
  `--chat`.
- `agent` holds the `[agent]` values in `config.toml` (Settings > Agent; [AGENT.md](AGENT.md#settings)): `python` (the
  sandbox interpreter of `run_python`; `""` = the app's own Python with the standard library only), `searxng_url`,
  `wikipedia_language` and `marginalia`. A key the file does not set, or sets to a bad value, shows its default.
  `python_path` is `python` with "~" expanded (`null` when empty), and `python_found` tells whether it is an
  executable file now. `python_problem` is why the sandbox refuses that file although it is there (another user can
  change it, see [AGENT.md](AGENT.md#the-interpreter)), else `null`. `default_python` is the interpreter the
  sandbox uses when `python` is empty (it runs with `-I -S`). `languages` are the Wikipedia languages the screen
  offers. `agent.restart_required` is true when these values differ from the ones the agent tools were built from at
  start.

`PUT /api/settings` takes JSON with the values to change: `{"paths": {"ltx_model": "/Volumes/Models/ltx-2.5-mlx-q8"},
"assist": {"provider": "local"}, "app": {"notify": false, "llm_idle_minutes": 30}, "agent": {"wikipedia_language": "de"}}`.
Each part is optional. It writes `config.toml` and returns the same object as
`GET /api/settings`, plus `changed` (for example `["paths.ltx_model"]`; empty when nothing changed). It needs the
`Origin` header of the app's own page (HTTP 403 without it), like the agent writes: `[agent] python` and `[paths]`
name programs that the app runs.

- Only the lines of the changed keys change. Comments, blank lines, other keys and other sections stay. A key that
  is not in the file goes after the last key of its section; a missing section goes at the end; a missing file is
  made.
- The write is atomic (a temp file, then a rename) and keeps the file mode. A symbolic link stays a link.
- The app reads `[paths]`, `[assist]` and `[agent]` only at start, so such a change takes effect after a restart
  (`restart_required`). The agent tools get the `[agent]` values once, when they are built. An `[app]` value applies at once: the running app uses it as soon as the request returns.
- Errors: HTTP 400 with `{"detail": {"message": "...", "fields": {"paths.ltx_model": "message"}}}` for an unknown
  key, an unknown provider, an empty path, a path with a double quote, a backslash or a line break, an `[app]` switch
  that is not `true` or `false`, `llm_idle_minutes` that is not a whole number from 0 to 1440, a `profile` that
  is not `"full"` or `"chat"`, a `chat_effort` that is not a thinking level, or a bad `[agent]` value (the rules are in
  [AGENT.md](AGENT.md#settings)). HTTP 409
  when `config.toml` is not valid TOML or sets the key in a form this endpoint cannot edit (for example an inline
  table); the file is then not changed.
- Settings hold no secrets.

## UI behaviour

The page is split into ES modules; web/README.md describes the files, the view contract and the handoff between
views.

- Sidebar: six sections (Create, Jobs, Chat, Library, Models, Settings, also Cmd-1 to Cmd-6), a dock with the running
  job's stage and time left, and a memory meter (job, local LLM, other apps, swap). Narrow windows get a top tab bar.
- Chat profile (`profile` of `GET /api/system`, or `?profile=chat` in the page address): the sidebar and the tab bar
  show Chat, Models and Settings (Cmd-1 to Cmd-3), and the app opens on Chat. A route to Create, Jobs or Library
  opens Chat. There is no job dock, no job toast and no job count in the window title; the memory meter shows a job
  only while one runs. Models and its setup card show only the entries whose `features` hold `"Chat"`. Settings
  leaves out prompt help, the job switches and the paths that Chat does not use.
- `?profile=full` or `?profile=chat` in the page address fixes the profile of that window. `start.py` opens such a
  window when the app on the port runs in the other profile.
- Create: the tool picker with time, peak memory and fit; the selected tool's form with prompt help ("Improve", with
  Cancel while it waits, loads or writes, and Undo and Redo after it); an "On this Mac" card with peak memory, time
  and queue wait; Generate ("Add to queue" while a job runs). Choosing another tool cancels the open tool's running
  Improve; going to another view does not: its text goes into the tool's saved form, with a toast.
- Jobs: the running job with progress, the queue with start estimates and move buttons, and finished jobs with their media, settings,
  "Use these settings", "Send to" (an output into another tool's file field), download and delete.
- Chat, Library, Models and Settings use the endpoints of their sections above.
- Chat: the Thinking button under the message box opens a menu of the thinking levels, each with a short hint (arrow
  keys, Return and Escape work). A chat opens with its saved `effort`, a new chat with `default_effort` of
  `GET /api/llm`. A level picked in a chat stays for this session, also before a message is sent.
- Polling: `/api/jobs` every 2 s while a job is queued or running, else 10 s; `/api/system` and `/api/llm` every 5 s;
  `/api/models` every 60 s (2 s during a download); `/api/assist` every 60 s; `/api/chats` every 30 s (3 s while an
  answer runs).
- Theme: System, Light or Dark, saved in the browser. No external fonts or scripts.
