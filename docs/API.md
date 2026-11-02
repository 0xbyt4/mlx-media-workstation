# API (contract between the server and the web UI)

The server (`app/server.py`, FastAPI) listens on `http://127.0.0.1:7870` only. The UI is static files in `web/`
(`index.html`, `app.js`, `style.css`), served at `/`. Plain HTML, CSS and JavaScript: no framework, no build step,
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
  `stage` is a short label, `fraction` is the share of the whole job done (0-1), and `eta_s` is the seconds left.
  `eta_s` blends the live rate with the median time of finished jobs with the same settings. Each field is `null`
  until the output shows it, and `eta_s` is `null` when the job has run past every estimate.
- Only one job runs at a time (each model needs most of the memory of a 32-48 GB Mac).

`POST /api/jobs/{id}/cancel` cancels a queued or running job. `DELETE /api/jobs/{id}` removes a finished job from the
list and deletes its folder.

`GET /files/{id}/{name}` serves an output file.

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
                   "total_gb": 54.1, "percent": 22.9}
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

Errors (model not installed, an empty draft, a media job using the memory, an answer in the wrong form) return HTTP
400 with `{"detail": "message"}`. The first request loads the model (16 GB from disk); later ones reuse it until it
is unloaded.


## UI behaviour

- Left sidebar: the groups and their tools; a small system line at the bottom (free memory, queue).
- Right side: the selected tool's form and a Generate button; below it the job list (queue and results).
- Poll `/api/jobs` every 2 s while a job is queued or running, every 10 s otherwise; poll `/api/system` every 5 s.
- Results: images as thumbnails (click to enlarge), audio with `<audio controls>`, video with `<video controls>`.
  Each result shows its tool, status, elapsed time, peak memory, its parameters (collapsed) and notes.
- "Use these settings" on a result fills the form with its `params` (not files).
- Dark, calm theme; readable on a laptop screen. No external fonts or scripts.
