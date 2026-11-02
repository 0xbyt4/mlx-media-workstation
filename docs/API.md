# API (contract between the server and the web UI)

The server (`app/server.py`, FastAPI) listens on `http://127.0.0.1:7870` only. The UI is static files in `web/`
(`index.html`, `css/`, `js/`; see `web/README.md`), served at `/`. Plain HTML, CSS and JavaScript: no framework, no build step,
no CDN or other network request. All JSON is UTF-8.

## Tools

`GET /api/tools` returns the list of tools. The UI builds its left sidebar and its forms from it.

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

- `group` is one of `Image`, `Voice`, `Music`, `Video`, `Edit`, in that order in the sidebar.
- Field `type` is one of `text`, `textarea`, `number`, `select`, `checkbox`, `file` (one file), `files` (several).
- `advanced: true` fields go in a collapsed "Advanced" section.
- `step` may be given for numbers (for example 0.05). A `null` default means an empty input.
- `assist: true` marks a text field that has an "Improve" button (see Prompt help below).

## Jobs

`POST /api/jobs` takes `multipart/form-data` with `tool` (the tool id) and one part per field (files as file parts,
checkboxes as `true`/`false`). It returns `{"id": "20261023-231500-ab12"}`. Errors return HTTP 400 with
`{"detail": "message"}`.

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
      {"url": "/files/20261023-231500-ab12/image.png", "kind": "image | audio | video", "name": "image.png",
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
- `waiting` (a text) is set only on the first queued job while it waits for prompt help or chat answers to finish
  before it starts.
  `stage` is a short label, `fraction` is the share of the whole job done (0-1), and `eta_s` is the seconds left.
  `eta_s` blends the live rate with the median time of finished jobs with the same settings. Each field is `null`
  until the output shows it, and `eta_s` is `null` when the job has run past every estimate.
- Only one job runs at a time (each model needs most of the memory of a 32-48 GB Mac).

`POST /api/jobs/{id}/cancel` cancels a queued or running job. `DELETE /api/jobs/{id}` removes a finished job from the
list and deletes its folder.

`GET /files/{id}/{name}` serves an output file.

## Library

`GET /api/library` returns every output of the finished (`done`) jobs, newest job first:

```json
[
  {
    "job": "20261023-231500-ab12",
    "tool": "video.t2v",
    "tool_label": "Text to Video",
    "kind": "image | audio | video",
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
  ffprobe does not report it. The server probes each file once and keeps the result in memory until the file changes.

`POST /api/library/reveal` takes JSON `{"job": "<job id>", "name": "<file name>"}` and shows the file in Finder
(`open -R`). It returns `{"status": "shown"}`. The file must be a listed output of that job: HTTP 404 for any other
name, a symbolic link or an unknown job, HTTP 400 for an input file, and HTTP 409 while the job is queued or
running.

`DELETE /api/library/{job}/{name}` deletes one output file and removes it from the job's `outputs`. It returns
`{"status": "deleted"}`. HTTP 400 for an input file, HTTP 409 while the job is queued or running, HTTP 404 for any
other name. The job stays in the job list, also when it has no outputs left.

## System

`GET /api/system` returns `{"mem_total_gb": 48.0, "mem_free_gb": 37.5, "swap_used_gb": 2.8, "running": "job id or null", "queued": 1}`.

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
      "download": {"status": "running | done | failed | cancelled", "error": null, "done_gb": 12.4,
                   "total_gb": 54.1, "percent": 22.9, "speed_mbs": 85.2, "eta_s": 489}
    }
  ]
}
```

- `kind: "download"` entries are weights the app downloads. `kind: "manual"` entries are model environments and
  checkouts: `install` holds the README commands, and `repo`, `size_gb`, `peak_gb`, `fits` and `disk_free_gb` are
  `null`.
- `path_key` is the `config.toml` key of the install path; `null` means the Hugging Face cache.
- `fits` is true when `peak_gb` plus `mem_reserve_gb` is at most `mem_total_gb`.
- `needs` names an entry to install first. `download` is `null` unless this model is the current or last download.
- `download.speed_mbs` is the speed in MB/s (10^6 bytes), averaged over at least the last 10 s of polls, and
  `download.eta_s` the seconds left at that speed. Both are `null` until the download has run for about 2 s, and when
  it is not running; `eta_s` is also `null` while the speed is 0.
- Sizes are in GB of 10^9 bytes (as Hugging Face shows them); memory is in GB of 2^30 bytes (as `/api/system`).

`POST /api/models/{id}/download` starts a download in the background and returns `{"status": "running"}`. One
download runs at a time. Errors return HTTP 400 with `{"detail": "message"}` (a manual entry, already installed,
another download running). Poll `GET /api/models` for progress; a failed download has the reason in
`download.error`.

`POST /api/models/{id}/cancel` stops the download and returns `{"status": "cancelled"}` (or the finished status).
Finished files stay, and the next download skips them.
## Prompt help

`GET /api/assist` returns `{"available": true, "provider": "local", "model": "Qwen3.8 27B (4-bit)", "llm": {...}}`.
`available` is false when the provider's model is not installed; the UI then hides the "Improve" buttons. `llm` is the
local model's state (see `GET /api/llm`).

`POST /api/assist/enhance` takes the same `multipart/form-data` as `POST /api/jobs`, plus `field`: the name of a field
with `assist: true`. Image fields go along as images; a video field goes along as the frame where the clip ends (at
`end_at` when that field is set). Audio files are not sent. It returns:

```json
{"text": "the new field text", "notes": "what the model added or assumed", "model": "Qwen3.8 27B (4-bit)",
 "cost_usd": null, "seconds": 9.4}
```

Errors (model not installed, too little memory on this Mac, an empty draft, an answer in the wrong form) return HTTP
400 with `{"detail": "message"}`. The first request loads the model (16 GB from disk); later ones reuse it until it
is unloaded. While a media job that does not fit next to the model runs, the request waits and answers when the job
ends; `GET /api/llm` shows `waiting` > 0 meanwhile.

## Local LLM and chat

`GET /api/llm` returns the local model's state:

```json
{"installed": true, "state": "ready", "busy": false, "waiting": 0, "media_peak_gb": 0.0,
 "model": "Qwen3.8 27B (4-bit)", "peak_gb": 18.0, "idle_stop_s": 600.0}
```

- `state` is `off`, `loading` or `ready`. The model loads on the first request and unloads after `idle_stop_s`
  without use, or before a media job that does not fit next to it.
- `waiting` counts requests that wait for such a job to end. They run before the next job starts; that job waits
  for them at most 10 minutes, then it stops the model and the answer ends with an error.
- `media_peak_gb` is the peak memory of the job that holds the memory now (0 when none).

`POST /api/llm/unload` stops the model and returns `{"status": "off"}`.

Chats are stored in `outputs/chats/<id>/` (`chat.json` and the images sent in the chat).

- `GET /api/chats` lists the chats, newest first: `id`, `title`, `created`, `updated`, `messages` (count), `running`.
- `POST /api/chats` creates an empty chat and returns its summary.
- `GET /api/chats/{id}` returns the chat with all messages and `running`.
- `DELETE /api/chats/{id}` deletes it (HTTP 409 while it answers).
- `GET /api/chats/{id}/files/{name}` serves an image of the chat (`img_N.png`, `.jpg` or `.webp`).
- `POST /api/chats/{id}/messages` takes `multipart/form-data`: `text`, `thinking` (`true`/`false`), up to 4
  `images` (PNG, JPEG or WebP, 20 MB each), or `regenerate=true` to answer the last user message again. It returns
  server-sent events (`data: {...}` lines):
  - `{"event": "waiting", "message": "..."}` while a media job holds the memory;
  - `{"event": "reasoning", "text": "..."}` and `{"event": "content", "text": "..."}` deltas;
  - `{"event": "done", "finish": "stop" | "length" | "cancelled", "content": "...", "reasoning": "...", "stats": {...}}`
    or `{"event": "error", "message": "..."}` at the end.
- `GET /api/chats/{id}/stream` replays the running answer's events from the start (for a page that came back); HTTP
  404 when nothing runs.
- `POST /api/chats/{id}/stop` stops the answer between tokens, also while it waits. It returns
  `{"status": "stopping"}`, or `{"status": "idle"}` when nothing runs. The reply is saved with `finish: "cancelled"`.

A text the model writes for a tool comes in a fenced block whose info string is `prompt`, the tool id and, for a tool
with several prompt-help fields, the field name (`prompt video.t2v`, `prompt music.ace lyrics`). The UI shows a
"Use in <tool>" button on such a block; without a field name it fills the tool's first `assist` field.

The answer runs on the server whether or not a page listens, and it is saved when it ends. The model sees the newest
turns that fit in 32K tokens.


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
  "llm": {"idle_stop_s": 600.0},
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
- `restart_required` is true when `config.toml` holds other values than the ones the app read at start.

`PUT /api/settings` takes JSON with the values to change: `{"paths": {"ltx_model": "~/models/ltx"}, "assist":
{"provider": "local"}}`. Both parts are optional. It writes `config.toml` and returns the same object as
`GET /api/settings`, plus `changed` (for example `["paths.ltx_model"]`; empty when nothing changed).

- Only the lines of the changed keys change. Comments, blank lines, other keys and other sections stay. A key that
  is not in the file goes after the last key of its section; a missing section goes at the end; a missing file is
  made.
- The write is atomic (a temp file, then a rename) and keeps the file mode. A symbolic link stays a link.
- The app reads `config.toml` only at start, so a change takes effect after a restart (`restart_required`).
- Errors: HTTP 400 with `{"detail": {"message": "...", "fields": {"paths.ltx_model": "message"}}}` for an unknown
  key, an unknown provider, an empty path, or a path with a double quote, a backslash or a line break. HTTP 409
  when `config.toml` is not valid TOML or sets the key in a form this endpoint cannot edit (for example an inline
  table); the file is then not changed.
- Settings hold no secrets.

## UI behaviour

The page is split into ES modules; web/README.md describes the files, the view contract and the handoff between
views.

- Sidebar: six sections (Create, Jobs, Chat, Library, Models, Settings, also Cmd-1 to Cmd-6), a dock with the running
  job's stage and time left, and a memory meter (job, local LLM, other apps, swap). Narrow windows get a top tab bar.
- Create: the tool picker with time, peak memory and fit; the selected tool's form with prompt help ("Improve", with
  Undo and Redo); an "On this Mac" card with peak memory, time and queue wait; Generate ("Add to queue" while a job
  runs).
- Jobs: the running job with progress, the queue with start estimates, and finished jobs with their media, settings,
  "Use these settings", "Send to" (an output into another tool's file field), download and delete.
- Chat, Library, Models and Settings use the endpoints of their sections above.
- Polling: `/api/jobs` every 2 s while a job is queued or running, else 10 s; `/api/system` and `/api/llm` every 5 s;
  `/api/models` every 60 s (2 s during a download); `/api/assist` every 60 s; `/api/chats` every 30 s (3 s while an
  answer runs).
- Theme: System, Light or Dark, saved in the browser. No external fonts or scripts.
