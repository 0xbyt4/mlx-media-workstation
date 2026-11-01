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
- Only one job runs at a time (each model needs most of the memory of a 32-48 GB Mac).

`POST /api/jobs/{id}/cancel` cancels a queued or running job. `DELETE /api/jobs/{id}` removes a finished job from the
list and deletes its folder.

`GET /files/{id}/{name}` serves an output file.

## System

`GET /api/system` returns `{"mem_total_gb": 48.0, "mem_free_gb": 37.5, "swap_used_gb": 2.8, "running": "job id or null", "queued": 1}`.

## UI behaviour

- Left sidebar: the groups and their tools; a small system line at the bottom (free memory, queue).
- Right side: the selected tool's form and a Generate button; below it the job list (queue and results).
- Poll `/api/jobs` every 2 s while a job is queued or running, every 10 s otherwise; poll `/api/system` every 5 s.
- Results: images as thumbnails (click to enlarge), audio with `<audio controls>`, video with `<video controls>`.
  Each result shows its tool, status, elapsed time, peak memory, its parameters (collapsed) and notes.
- "Use these settings" on a result fills the form with its `params` (not files).
- Dark, calm theme; readable on a laptop screen. No external fonts or scripts.
