# Agent mode

In an agent chat the local model can call tools. It can read the Workstation's state, queue, wait for and cancel media
jobs, transcribe speech, read the text of images and PDF pages, read and write files in one folder, read media facts, look at images, search the web, read
web pages, and run Python and shell commands in a sandbox. The code is in `app/agent/`. The chat loop
that uses it is in `app/chat.py`. The endpoints and events are in `docs/API.md` under "Agent mode".

## Tools and risk levels

Each tool has a risk level. `app/agent/policy.py` decides from that level whether a call runs, asks or is refused.

| Level | Meaning | Rule |
|---|---|---|
| R0 | Reads app state only. | Runs. |
| R1 | Reads the user's files in the granted folder, or copies one into the chat's work folder. | Runs when the chat has a granted folder. Refused without one. The grant is the consent. `media_info` and `view_image` also run without a folder: they read the chat's work folder and the Library too. |
| R2 | Sends text off this Mac, queues minutes of compute, cancels a job, or writes a new file in the granted folder. | Asks on the first use in a chat. The user can answer "Allow in this chat", except for a tool whose result taints the chat (web_search, fetch_url, transcribe_audio, ocr_document): after its first result it asks every time, so a chat-wide grant would do nothing. When the chat is tainted, every R2 call asks, also with that grant. |
| R3 | Runs code, changes an existing file, or has effects the app cannot check. | Asks every time. "Allow in this chat" is not offered. |

A tool that is off in the chat's Tools menu is refused. An unknown tool name is refused. The model gets the reason as
the tool's result, so it can answer the user. For an unknown name the reason gives the nearest names (`difflib`) and
the tools in the chat's tools array.

| Tool | Level | What it does |
|---|---|---|
| `update_plan` | R0 | Writes or updates the answer's plan: a checklist of at most 10 steps, each `pending`, `in_progress` or `done` (at most one `in_progress`). The page shows it at the top of the answer. |
| `list_workstation_tools` | R0 | Lists the media tools, their fields and whether their models are installed. |
| `job_status` | R0 | Shows one job: status, progress, outputs (with their file references), error. |
| `search_library` | R0 | Finds finished outputs by text, tool or file name. Its result is untrusted. |
| `create_job` | R2 | Queues a media job and returns the job id at once. A file field takes file references (see [Input files](#input-files)). |
| `list_jobs` | R0 | Lists the queued and running jobs and the newest finished ones (10 by default, at most 30), one line each. |
| `wait_for_job` | R0 | Waits until a job ends (120 s by default, at most 300 s), then shows it as `job_status` does. |
| `cancel_job` | R2 | Cancels a queued or running job that this chat queued. |
| `transcribe_audio` | R2 | Speech to text for an audio or video file (Qwen3-ASR 0.6B as a media job): the text with a time for about every 30 s, and the language. Its result is untrusted. |
| `ocr_document` | R2 | The text of a photo, a scan or PDF pages (OvisOCR3 0.8B as a `doc.ocr` media job), as Markdown or plain text. Its result is untrusted. |
| `system_status` | R0 | Shows the Mac's memory, the running jobs and their progress, the queue, the chat model and whether each media tool fits now (`GET /api/status`). |
| `list_files` | R1 | Lists a folder inside the granted folder (at most 500 lines). |
| `read_file` | R1 | Reads a text file inside the granted folder (the first 1 MB), by line range (`start_line`, `end_line`). The result gives the line count and where to read on. It stops before the 12,000-character cap of a result. |
| `search_files` | R1 | Searches the text files of the granted folder (at most 5,000 files and 15 s). |
| `write_file` | R2 | Creates a new text file in the granted folder. Never overwrites. |
| `edit_file` | R3 | Changes an existing text file in the granted folder: an exact replacement or the whole text. The approval card shows the diff. A copy of the old file goes to the work folder first. |
| `copy_to_workspace` | R1 | Copies a file of any type (at most 500 MB) from the granted folder into the chat's work folder, for `run_python`. |
| `export_from_workspace` | R2 | Copies a file from the work folder, or a media job's output (`library:<job id>/<file name>`), into the granted folder, under a new name. |
| `media_info` | R1 | Duration, size, resolution, frame rate, codecs, channels and sample rate of an image, audio or video file (ffprobe). |
| `view_image` | R1 | Lets the model see an image: a copy of at most 1280 pixels on its long side goes into the next model request. |
| `web_search` | R2 | Searches the web. Sends the query off this Mac. |
| `fetch_url` | R2 | Reads one public web page as text, in parts of 10,000 characters. Sends the URL off this Mac. |
| `run_python` | R3 | Runs Python code in the sandbox. Present only when the sandbox works on this Mac. |
| `run_shell` | R3 | Runs a shell command (zsh) in the sandbox. Present only when the sandbox works on this Mac. |
| `spotlight_search` | R1 | Finds files in the granted folder with Spotlight (`mdfind -onlyin`), by content or by name (at most 200 results). See [Host tools](#host-tools). |
| `speak` | R0 | Saves text as speech (`say`) in the work folder: m4a, aiff or wav, at most 5,000 characters. Nothing plays. See [Host tools](#host-tools). |
| `convert_document` | R1 | Converts a document (docx, doc, odt, rtf, html, txt) to txt, rtf, html, docx or odt (`textutil`). The new file goes into the work folder. See [Host tools](#host-tools). |
| `list_shortcuts` | R0 | Lists the names of the user's Shortcuts. Its result is untrusted. See [Host tools](#host-tools). |
| `run_shortcut` | R3 | Runs one of the user's Shortcuts by name, with an optional text input, and returns its text output. See [Host tools](#host-tools). |
| `memory` | R3, or R0 when "Ask before saving" is off | Adds, replaces or removes one entry of the memory stores. See [Memory and chat search](#memory-and-chat-search). |
| `search_chats` | R0 | Finds words in the earlier chats and returns each chat's title, date and short snippets. Its result is untrusted. |
| `tool_search` | R0 | Finds the chat's tools by what they do (BM25) and returns the best matches with their full schema. In the tools array only when the chat has deferred tools. |
| `tool_describe` | R0 | Returns the full schema of named tools. |
| `tool_call` | R0 | Runs a deferred tool by name. The loop runs the named tool with its own level, so the rule of that tool applies. |
| `read_result` | R0 | Reads on in a long result that the loop saved in the work folder (see [Long results](#long-results)). Its result is untrusted. |

`tool_search`, `tool_describe`, `tool_call` and `read_result` are not in the Tools menu. They are on in every chat that
has them.

Tools of MCP servers that the user added (`<server>__<tool>`, always R2) come in too: see
[MCP tools](#mcp-tools).

The Workstation tools exist only in the full profile. The chat profile has no Workstation, so its registry has none
of them.

The MCP server offers the same Workstation tools to local agents, such as Claude Code ([MCP.md](MCP.md)). The tools
are built once (`ws_tools.build`). A `ws_tools.Host` holds what differs: the forms of a file reference, the words of
the results, and the time of a wait.

### Workstation chats

A workstation chat in the full profile runs the same loop (`Turn`) with the Workstation tools only:
`list_workstation_tools`, `job_status`, `search_library`, `create_job`, `list_jobs`, `wait_for_job`, `cancel_job`,
`transcribe_audio`, `ocr_document` and `system_status`. `workstation_registry()` in `app/chat.py` builds them with `ws_tools.build(port)`.

- It has no file, web, media, plan, Python or shell tool. A call to one of them is refused: no such tool.
- The tools keep their risk levels. `create_job`, `cancel_job`, `transcribe_audio` and `ocr_document` ask first, as
  above.
- The policy, the approval cards, the grants, the taint, the step limit and the memory lease are those of an agent
  chat. A job that needs the chat model's memory starts after the answer.
- It has no folder. A folder grant is refused (HTTP 400), so a `folder:` file reference fails. A `library:`
  reference works.
- The Tools menu turns each tool on or off and ends a grant. A new workstation chat has every tool on.
- The prompt lists every field of each media tool and tells the model when to queue a job: when the user asks it
  to make something. When the request is unclear, or the user wants to edit the text first, it writes `prompt`
  blocks instead.
- The prompt also names the media tools that are not installed (`not_installed()` in `app/chat.py`), so the model
  does not need `list_workstation_tools` to learn them.
- The registry needs no `[agent]` settings and no sandbox.
- In the chat profile a workstation chat has no tools. It answers in one model request, without the loop.

### Input files

A file field of `create_job` (`"type": "file"`) takes one file reference. A `files` field takes a list of them (at
most 10). `transcribe_audio` takes one in `audio`, `ocr_document` one in `file`. A reference has one of these forms:

| Reference | The file |
|---|---|
| `library:<job id>/<file name>` | An output of a finished job, as `search_library` and `job_status` show it. Inputs (`in_*`) are not outputs. |
| `work:<path>` | A file in the chat's work folder (`outputs/chats/<id>/ws`), where `run_python` writes. |
| `folder:<path>` | A file in the granted folder. Refused when no folder is granted. |

- The `work:` and `folder:` files follow the rules of the file tools: no hidden or secret names, the real path must
  stay inside the folder, a link at the last part is refused (`O_NOFOLLOW`), and the open file's real path is checked
  again (`F_GETPATH`). A work folder that is a link is refused.
- The tool opens the file and checks its size once. The server copies that open file into the job folder with the
  checks of an upload (`prepare_job` in `app/server.py`, shared with `POST /api/jobs`): at most 200 MB, the count of
  the field, and the name `in_<field>_<n><suffix>`. Only the suffix of the file's name is kept.
- The agent path also checks the type: the suffix must be a media type that the field accepts (`image/*` takes
  `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.heic` and `.heif`; `audio/*` takes `.wav`, `.mp3`, `.flac`, `.m4a`,
  `.aac`, `.ogg`, `.opus`, `.aiff` and `.aif`; `video/*` takes `.mp4` and `.mov`), or a suffix that the field names
  (the subtitle file of `edit.subtitle` takes `.srt` and `.vtt`). A browser upload of another type is kept as `.bin`;
  the agent's is refused.
- The approval card lists the input files with the other fields. The risk stays R2: the job uses this Mac for
  minutes.
- The job stores the chat that queued it (`"chat"` in `job.json`).

### Waiting for a job

`create_job` returns at once. `wait_for_job`, `transcribe_audio` and `ocr_document` wait for a job. The answer holds the model's
memory lease. A job that fits next to the model (an image, a transcription on a 48 GB Mac) runs during the answer. A
job that does not fit (a video, for example) waits for the lease, so it starts only after the answer.

- The wait does not wait for such a job, or for a job behind one in the queue. It returns at once, and the model
  tells the user that the job runs after the answer. The answer does not give its lease up to such a job: the model
  would then wait for the whole job before its next step.
- A light job (`edit.mix`, `edit.concat`, `edit.subtitle`) needs no memory and does not wait for the model jobs, so
  the wait waits for it.
- A wait takes at most 300 s. It ends early when the job ends or the user presses Stop.
- The wait counts against the answer's 10 minutes. It stops 60 s before the answer's time ends, so the model can
  still answer. With less time left it does not wait and says so.
- A job that still runs after the wait goes on in the background. The page shows its live card.

### Cancelling a job

`cancel_job` cancels only a job that this chat queued. Another job is refused, also when the user would approve: the
user cancels it in the Jobs view. A web page or a file could tell the model to cancel the user's other work, and the
card would show only a job id. It asks like every R2 tool ("Allow in this chat" is offered until the chat is
tainted).

### Transcription

`transcribe_audio` queues a `voice.transcribe` job: the Transcribe tool, which the Create page shows too. The job runs
`app/runners/transcribe.py` with the Drama 3 environment's Python and Qwen3-ASR 0.6B, the speech check of Drama 3. So
it gets the queue, the memory claim, the progress ("part i/n") and Cancel of every job. The plan turns the sound
track into 16 kHz mono first, so a video works too.

- The output is `transcript.txt`: a `Language:` line, then one line per part of at most 30 s with its time
  (`[0:00-0:28] ...`). The model has no word times. With `language` `auto`, it names the language of each part.
- Its peak was 2.6 GB for 40 s of speech, so it runs next to the chat model.
- When the job ends in the wait, the tool returns the transcript. Else it returns the job id; the model reads the
  text later with `transcribe_audio` and `job_id`.
- When Qwen3-ASR or the Drama 3 environment is not installed, the tool is refused with a note to install them in
  Models. It downloads nothing.

### Reading text (OCR)

`ocr_document` queues a `doc.ocr` job: the Read Text (OCR) tool, which the Create page shows too. It takes `file` (a
file reference to an image or a PDF), `pages` (PDF pages such as `1-3, 7`; empty = all, at most 50), `output`
(`markdown` or `text`), `job_id` and `seconds`.

- The job has two steps. `app/runners/pages.py` (the app's Python with macOS Quartz) draws each chosen page as a PNG
  image. Then `app/runners/ocr.py` (the local LLM environment, mlx-vlm) reads each image with OvisOCR3.
- The output is `document.md` or `document.txt`. In the text of a PDF, each page starts with `<!-- page N -->`.
- OvisOCR3 needs about 6 GB at its peak, so the job uses the model lane. A short page takes about 2 s.
- When the job ends in the wait, the tool returns the text. Else it returns the job id; the model reads the text
  later with `ocr_document` and `job_id`.
- The text comes from a picture or a document, so the result taints the chat. A page can hold words that look like
  instructions.
- When OvisOCR3 or the local LLM environment is not installed, the tool is refused with a note to install them in
  Models.

A new agent chat has every tool on except `run_python` and `run_shell`. A chat saved before `update_plan` existed
keeps its own list, so the plan is off there until the user turns it on. The model writes `run_python`'s code and
`run_shell`'s commands, and they can carry text from earlier untrusted content. So the user turns them on in the
Tools menu when the chat needs them. Both ask before every call. The tools that write files are on: `write_file` and
`export_from_workspace` ask first, `edit_file` asks every time.

The tools that read or write the user's files are offered to the model only while a folder is granted. `media_info`
and `view_image` are offered without one.

The file tools never list, search or read hidden names (any path part that starts with `.`) or secret files (keys,
certificates, keychains, password databases, `credentials`, `secrets.*` and similar). They resolve every path and
keep it inside the granted folder. They open a file with `O_NOFOLLOW` and check its real path again from the open
descriptor. They read only text files. They open the granted folder itself with `O_NOFOLLOW` too, and its real path
(`F_GETPATH`) must equal the stored path. A granted folder that was moved, or replaced by a link (also a folder above
it), is refused until the user grants it again.

The folder grant is checked by the server (`PATCH /api/chats/{id}/agent`). It stores the folder's real path as the
disk spells it (`F_GETPATH`), so another letter case cannot pass a check. It refuses these folders and everything
inside them:

- `/`, the folders directly under `/`, the home folder itself and the folders above it;
- system folders and `~/Library`;
- folders of programs: `/Applications`, `~/Applications` and `/opt` (Homebrew);
- paths with a bundle part (`.app`, `.framework`, `.bundle`, `.plugin`, `.kext` and the other bundle types) or a
  `site-packages` or `dist-packages` part (Python loads code from there);
- `/private/var` and `/private/tmp` (also as `/var` and `/tmp`): every user can write there;
- other users' folders under `/Users` (also `/Users/Shared`);
- the app's own folder (its settings, outputs and the other chats), and the folders that hold it;
- paths with a hidden part.

## Writing files

`write_file`, `edit_file` and `export_from_workspace` write in the granted folder. `copy_to_workspace` and the backups
of `edit_file` write in the chat's work folder (`outputs/chats/<id>/ws`). The rules are in `app/agent/safe_fs.py`:

- **No links.** A path in the granted folder must have no link in any part, also not one to a place inside the
  folder. A folder of the path is opened with `O_NOFOLLOW` and its real path is checked again. In the work folder,
  each part is opened with `O_NOFOLLOW` from the folder before it, because the sandboxed code can make links there.
  A work folder that is a link is refused.
- **Names.** No hidden or secret name (the read rules above), no control character, no name that ends with a space
  or a dot, at most 255 bytes.
- **No code folders.** The tools never write inside a bundle (`Thing.app/Contents/x.txt`) or a `site-packages` or
  `dist-packages` folder. This also holds when the granted folder itself is inside one (a grant made before the
  server refused such folders).
- **No executable types.** These are refused, also as a folder on the way (`Thing.app/Contents/x.txt`): `.app`,
  `.command`, `.tool`, shell scripts (`.sh`, `.bash`, `.zsh`, `.csh`, `.tcsh`, `.ksh`, `.fish`), `.py`, `.pyw`,
  `.pyz`, `.pyc`, `.rb`, `.pl`, `.php`, `.lua`, `.tcl`, AppleScript and Automator files, `.terminal`, `.plist`,
  `.mobileconfig`, libraries and bundles (`.dylib`, `.so`, `.bundle`, `.framework`, `.kext`, `.plugin`...), Windows
  programs and scripts (`.exe`, `.bat`, `.ps1`, `.vbs`, `.js`, `.jse`, `.wsf`, `.hta`, `.scr`, `.reg`...), `.jar`,
  `.pkg`, `.dmg`, `.pth` (Python's `site` module runs its import lines at every start) and link files (`.webloc`,
  `.url`, `.desktop`...). Python is refused because a double-clicked `.py` file runs in Python Launcher when
  python.org's Python is installed. A file with an
  executable bit is not changed or exported.
- **Text.** `write_file` and `edit_file` write UTF-8 text of at most 1 MB, without NUL characters. `edit_file` changes
  only a file that is UTF-8 now (a change would damage text in another encoding), at most 1 MB, with one hard link.
  A file with Windows line ends keeps them when `old_text` has plain ones.
- **Atomic.** The tool writes a temp file in the same folder, then links it in under the new name (`link()` fails
  when the name exists, so nothing is overwritten) or, for a change, renames it over the old file. The rename happens
  only while the old file is still the one that was read (inode, size and time). A failed write leaves no file.
- **Modes.** A new file gets the default mode (0644 less the umask). A changed file keeps its mode. The tools never
  change a mode.
- **No overwrite.** `write_file` and `export_from_workspace` refuse a name that exists. `write_file` points the model
  to `edit_file`.

`edit_file` takes `old_text` and `new_text` (`old_text` must be found exactly once, or set `replace_all`), or
`content` for the whole new text. These steps run:

1. Before the approval card, the loop reads the file and makes the change as a unified diff. A change that cannot
   be made (text not found, found twice, no change, a refused file) goes back to the model at once. The user is not
   asked. So does a change whose diff is longer than the card shows (400 lines or 20,000 characters): the model must
   split it into smaller edits, so the user always sees the whole change.
2. The card shows the diff, with hiding characters written as `\uXXXX`. The loop keeps the SHA-256 of the diff
   before that escaping (`Context.digest`).
3. After "Allow once", the tool makes the diff again. When its SHA-256 is different (the file changed), it writes
   nothing. The digest also tells apart two diffs that the escaping shows the same, such as a zero-width space and
   the text `\u200b`.
4. It copies the old file to `backups/<date>-<time>-<name>` in the work folder and names that copy in its result.
   The page links it.
5. It writes the new text atomically.

`export_from_workspace` copies images, audio, video, PDF, CSV, TSV, XLSX, DOCX, PPTX and Parquet files up to 500 MB.
Every other file must be UTF-8 text up to 1 MB. Its `path` is a file in the work folder (also as `work:<path>`) or a
Library reference, `library:<job id>/<file name>`. A Library reference must name a listed output of a finished job,
as `job_status` and `search_library` show it. The tool opens that file with `O_NOFOLLOW`, so a link is never copied.
The approval card says where the file comes from: the work folder or the Library. So "make an image and save it in my
folder" takes three calls: `create_job`, `wait_for_job`, then `export_from_workspace`. `copy_to_workspace` copies any type up to 500 MB. Both check that
1 GB of disk stays free. `copy_to_workspace` also checks that the work folder stays under the sandbox's limit
(500 MB), so `run_python` still runs.

The page shows a written file of the granted folder with a "Show in Finder" button (`POST /api/chats/{id}/reveal`).

## Media files

`media_info` and `view_image` take a `path` and a `location`: `folder` (the granted folder, the default), `work` (the
chat's work folder) or `library` (`<job id>/<file name>`, as `search_library` shows it). The app opens the file with
the same checks as the other tools. It gives the open file to `ffprobe` or `ffmpeg` as `/dev/fd/N`, so a link swapped
in after the check cannot redirect them.

- They run with an argument list (no shell) and a time limit (20 s for ffprobe, 30 s for ffmpeg).
- They may use the `file` protocol only. `ffprobe` may use only the demuxers of the media types (MP4/MOV, Matroska,
  AVI, WAV, MP3, AAC, FLAC, Ogg, AIFF and the image formats). `ffmpeg` in `view_image` gets a fixed demuxer from the
  file's extension. So a playlist or a concat list cannot make them read other files or the network.
- They find `ffprobe` and `ffmpeg` on the `PATH`, as the rest of the app does.
- They run in a Seatbelt sandbox (`media_profile`), because code in the sandbox can write the files they parse. The
  profile denies by default. It lets them read the system folders, Homebrew's program folders, the program's own
  prefix and the one open file (`/dev/fd/N`), and write only the PNG copy of `view_image`. They have no network, no
  system services, no fork and no view of other processes. A program outside Homebrew that needs other folders
  fails with "Operation not permitted".

`view_image` reads PNG, JPEG, WebP, GIF, BMP and TIFF images up to 50 MB. It writes a PNG copy of at most 1280 pixels
on its long side into the chat's folder (`view_<hex>.png`), where the sandbox cannot write. The tool message keeps
the copy's name. The next model requests get it as an image part of that tool message. Only the newest 2 tool images
go to the model, so the context stays small.

## Host tools

The shell sandbox has no access to system services (no `mach-lookup`). So Spotlight (`mdfind`), speech (`say`),
`textutil` and `shortcuts` fail inside it. The host tools run these programs outside the sandbox instead. The code is
in `app/agent/host_tools.py`. A provider (`HostTools`, loaded in `app/agent/providers.py`) gives the tools.

- Each tool runs one program from `/usr/bin` with a fixed argument list. There is no shell.
- The program gets its own session, no input (`/dev/null`) and a small environment (`PATH`, `HOME`, `LANG`).
- Stop and the time limit kill its process group. The time limit is shorter when the answer ends sooner.
- All host tools are deferred: they are not in the chat's tools array. The model finds them with `tool_search`.
- A tool whose program is missing is not offered. The provider adds no text to the system prompt.

| Tool | Program | Time limit | Rules |
|---|---|---|---|
| `spotlight_search` | `mdfind -0 -onlyin <folder>` | 20 s | Searches only the granted folder or a folder in it. The query must not start with `-`. The tool drops results outside the folder and hidden or secret files, and never says that one matched. Paths are relative. |
| `speak` | `say -f <text file> -o <file>` | 120 s | The text goes in as a file, not as an argument. The tool picks the voice: the `voice` name, else the first voice of `language` that is not a novelty voice (such as Zarvox), else the Turkish voice for a text with Turkish-only letters (ğ, ş, ı, İ), else the system voice. A name must match a voice that `say -v ?` lists, because `say` uses the system voice for an unknown name without an error. |
| `convert_document` | `textutil -convert <format> -noload -nostore` | 60 s | Reads a copy of the file, at most 50 MB. A docx or odt file gets the zip guard of attached files first. HTML loads no resources (`-noload`). HTML without a charset is read as UTF-8. A txt result also shows its first 8,000 characters. |
| `list_shortcuts` | `shortcuts list --show-identifiers` | 15 s | Shows at most 300 names. |
| `run_shortcut` | `shortcuts run <identifier> --output-path <file> [--input-path <file>]` | 120 s | Runs the shortcut by its identifier, not by its name. The input text goes in as a file, at most 20,000 characters. The tool returns text output only. |

`speak` and `convert_document` write a new file into the chat's work folder. They never overwrite a file. The work
folder's 500 MB limit applies. `convert_document` reads the granted folder (`location` `folder`) or the work folder
(`location` `work`).

### Risk levels of the host tools

- `spotlight_search` is R1, as `search_files`. It reads names and indexed text of the files in the granted folder
  only. The grant is the consent. It writes nothing and sends nothing off the Mac.
- `speak` is R0. It reads only the text from the model and writes a new audio file into the work folder.
- `convert_document` is R1 and needs no folder, as `media_info`. It reads a file and writes a new file into the
  work folder only.
- `list_shortcuts` is R0. It reads only the names of the shortcuts. The names are untrusted: a shortcut can come
  from a link.
- `run_shortcut` is R3. A shortcut can do anything: send messages, change files, use the network. So it asks every
  time, and "Allow in this chat" is not offered.

The approval card of `run_shortcut` shows the shortcut's name, its identifier and the whole input text:

```
Shortcut: Make GIF
Identifier: 5ACAB88F-A7F9-4732-89FB-41A09F9EE79D
Input text (10 characters):
frames/a/
```

The tool reads the shortcut list again before it runs. When the card is different now (the shortcut was renamed, or
another shortcut has the name), it does not run and tells the model to call it again. An unknown name fails before
the card shows: the error gives the nearest names.

## Memory and chat search

The memory provider (`app/memory.py`, loaded by `app/agent/providers.py`) gives a chat two tools and one prompt
section. It follows the design of Hermes Agent: two small stores with a character cap, a snapshot that does not
change during a chat, and a search over past chats with SQLite FTS5. Nothing runs in the background.

### The stores

The stores are in `outputs/memory/memory.json`. Each store is a list of short entries.

| Store | Holds | Cap |
|---|---|---|
| `user` | Who the user is and what they prefer. | 800 characters |
| `notes` | Facts about the setup and lessons from earlier chats. | 1,250 characters |

An entry counts its length plus 3 characters (its `- ` mark and its line break). An entry has at most 300
characters. With these caps the section of two full stores stays under its budget of 800 tokens (characters / 3).

### The memory section

The section shows both stores, each with a usage header such as `Notes (store notes, 61% full: 763/1,250
characters)`. A chat takes it at its first turn and saves it in its folder (`outputs/chats/<id>/memory.txt`). Each
later turn uses the saved text, also after a restart. So the prompt's start does not change and the prompt cache
holds. A change made during a chat shows from the next chat. The tool result of a write says so.

A chat has the section and the `memory` tool (a core tool) when all of these are true:

- Settings > Chat memory has "Memory" on.
- The chat is not temporary (`"temporary": true` in `chat.json`).
- The chat is an Agent or a Workstation chat, or a General chat with "Memory in General chats" on (off by default).

### The `memory` tool

Arguments: `action` (`add`, `replace` or `remove`), `store` (`user` or `notes`), `text` (the new entry, for `add`
and `replace`) and `old` (a part of the entry that only that entry has, for `replace` and `remove`).

1. The tool checks the change before it asks. A refused change goes back to the model without an approval card.
2. With "Ask before saving" on (the default), the tool is R3: each call shows an approval card. The card shows the
   change as `+` and `-` lines and the store's usage before and after it.
3. The write happens only when the store is still the one the card showed. Else the call fails and nothing is saved.
4. With "Ask before saving" off, the tool is R0 and runs without a card. In a tainted chat it refuses the write: a web
   page or a file could tell the model what to save. A chat keeps the risk level of its first turn. When a chat
   started with "Ask before saving" off and the setting is on now, a write without a card is refused.

The tool refuses these changes with a message that the model can act on:

- A full store. The message gives the characters that are missing and the entries, so the model can merge or remove
  entries first. Nothing is compacted without a call.
- An exact duplicate, an empty entry, an entry over 300 characters, or an `old` part that no entry or more than one
  entry has.
- Hidden or control characters (the same set as the approval card escapes).
- Text that looks like a secret: API keys (OpenAI, Anthropic, GitHub, GitLab, AWS, Google, Slack, Hugging Face),
  private keys, JSON web tokens, 64-digit hex keys, bearer tokens, `password: ...` and similar, a URL with a password
  or with a `token=` or `key=` query, and long random strings with digits, small and capital letters.
- Text that reads like orders to a model or holds chat markup: "ignore the previous instructions", "system prompt",
  "you are now", "new instructions", tags such as `<tool_call>` or `</system>`, "do not tell the user", a request to
  send data to a URL, `curl` or `wget`, and some Turkish forms of these.

The checks match patterns. They do not catch every secret or every injection. The approval card is the main
control.

### "Saved" without a call

A model of this size can say "I saved that to memory" without a `memory` call. After an answer, the chat loop calls
`memory.check_claim()`. When the answer's text says that it saved or will remember something (English and Turkish
phrases) and no `memory` call returned `ok` in the answer, the answer gets a note: "This answer says that it saved
something to memory, but no memory call ran. Nothing was saved." The model is not asked again.

### `search_chats`

`search_chats` is a deferred tool: the model finds it with the tool search. Arguments: `query` (1 to 200
characters) and `limit` (1 to 10 chats, 5 by default).

- The index is `outputs/memory/chats.sqlite3` (`app/chat_search.py`, the `sqlite3` module of Python with FTS5). It
  holds each chat's title and the text of its user and assistant messages. It does not hold the reasoning, the tool
  results or the app's notes.
- Before each search the index reads the chats whose `chat.json` changed (size and modification time) and drops the
  deleted chats. Settings > Chat memory can build it again.
- A temporary chat is never indexed. The search never returns the chat it runs in.
- Case and accents do not matter. The index and the query fold the same way: lower case, accents removed, and the
  Turkish `İ`, `I` and `ı` read as `i`. So `istanbul` finds `İSTANBUL'da`, and `sikayet` finds `şikâyet`.
- Each query word also matches the words that start with it (`kedi` finds `kedimi`). All words must match. When no
  chat has them all, any word may match.
- The result gives each chat's title, date and at most 2 snippets of about 160 characters. It says that the text is
  data from past chats. The result taints the chat: past chats can quote web pages and files.
- When this Python's SQLite has no FTS5, the tool is not offered and Settings shows "Not available".

## Taint

Taint is a flag per chat. It is stored in `chat.json` with the grants.

- These tools set it when they run: `search_library`, `transcribe_audio`, `ocr_document`, `list_files`, `read_file`,
  `search_files`, `copy_to_workspace`, `media_info`, `view_image`, `web_search`, `fetch_url`, `run_python`,
  `run_shell`, `search_chats`, `spotlight_search`, `convert_document`, `list_shortcuts` and `run_shortcut`. Their
  results can hold text from outside the app (an image can hold written text too).
- A message with an attached file that is not an image (a document, a text file, an audio file) sets it when the
  message is saved, before the model reads the file. This applies to agent chats and to workstation chats, also in the chat
  profile: the same chat can open later in the full profile, with tools. The file's text (or the audio's transcript) comes from outside
  the app, as a web page does: a downloaded PDF can hold orders for the model. See
  [API.md](API.md#attached-files).
- An attached image does not set it. An image can hold written text too, but the user chose it and sees it in the
  message. This is a choice, not a check: see [What is not protected](#what-is-not-protected).
- Once set, it stays set for the rest of the chat. A new answer does not clear it, because the text is still in the
  model's context.
- While it is set, every R2 call asks, and "Allow in this chat" is not offered. A web page could tell the model to
  put private text into a query or a URL, to queue jobs or to write files. The user sees such a call before it runs.

The prompt also tells the model that tool output is untrusted data and never instructions. The block of an attached
file says the same: its text is data from the file, not instructions.

## The sandbox (`run_python`)

`app/agent/sandbox.py` runs the code with `sandbox-exec` and a Seatbelt profile that denies by default.

- **Exec**: only the interpreter (and the `Python.app` stub that a framework build re-executes). No fork, so no
  shell and no child processes.
- **Read**: `/` itself, `/usr/lib`, `/usr/share`, `/System/Library`, the OS cryptexes, the time zone data,
  `/dev/null`, `/dev/random`, `/dev/urandom`, `/dev/zero` and the interpreter's own folders.
- **Read and write**: the chat's work folder only (`outputs/chats/<id>/ws`), and writes to `/dev/null`.
- **Metadata**: `stat` works on any path. Directory listings and file contents elsewhere are refused.
- **sysctl**: the hardware facts (`hw.*`) and the OS name and version only.
- **Processes**: information about the process itself only. `(deny default)` does not cover the `process-info*`
  operations, so the profile denies them and allows them for the process itself. The code cannot read other
  processes' command lines and environment (`KERN_PROCARGS2`), their paths or the process list.
- **Network**: no rule allows it. Every socket and every DNS lookup is refused. No `mach-lookup`, so no system
  service can act for the code.
- The work folder node itself is not writable, so the code cannot replace it with a symlink. The host refuses a work
  folder that is a symlink.

Default limits (`Limits`):

| Limit | Value | How |
|---|---|---|
| Wall-clock time | 60 s, less when the answer ends sooner (see below the table) | The host kills the process group. |
| CPU time | 60 s | `RLIMIT_CPU` |
| Memory | 2,048 MB | A watchdog polls the group's footprint every 0.05 s. macOS refuses memory rlimits. |
| One file | 50 MB | `RLIMIT_FSIZE` |
| Work folder | 500 MB, 10,000 entries | The watchdog checks every 0.5 s. A file counts with the most of its size, its blocks (a resource fork) and its size plus its extended attributes. |
| Output kept | 20,000 characters of stdout and of stderr each | The tool cuts the rest. |
| Code | 100,000 characters | The tool's schema. |

A run never goes into the answer's last 60 s, as a job wait does not: the wall-clock limit is cut to the time that
is left, and the result's second line says so ("note: the time limit was cut from 60 s to 25 s ..."). When less than
1 s is left, the call does not run and the model gets an error.

The process runs in its own session. A timeout, Stop or a limit kills the whole group. Core files are off. The
environment has only `PATH`, `HOME` and `TMPDIR` (both in the work folder), `LANG` and the matplotlib settings.

The files that the code writes stay in the work folder for later calls in the same chat. The page shows images inline
and links other files (`GET /api/chats/{id}/ws/{path}`).

### The interpreter

- By default the sandbox runs the app's own base interpreter with `-I -S`. The code gets the standard library only
  and no `site-packages` folder.
- `[agent] python` in `config.toml` names another interpreter, usually a venv made for the sandbox (for example with
  numpy, pandas and matplotlib). It runs with `-I`: its own packages work, but no user site folder and no `PYTHON*`
  variable. The sandbox may read that environment's folders.
- The app runs that interpreter once outside the sandbox, to ask it for its folders. So no other user may be able to
  change it: the file and every folder above it, up to `/`, must belong to you or to root and must not be writable
  by the group or by others. The path is checked as written:
  for a symlink (a venv's `bin/python`), the file it names is checked, not the folders of that file.
- When that interpreter does not exist or fails that check, or `sandbox-exec` is missing, the registry has no
  `run_python`. Settings > Agent shows why.

## The shell sandbox (`run_shell`)

`app/agent/shell_sandbox.py` runs the command as `/bin/zsh -f -c <command>` with `sandbox-exec` and a Seatbelt
profile that denies by default. It uses the limits and the watchdog of `run_python`. The approval card shows the
command in a code block, the folder it runs in and the granted folder it may read.

What the command can do:

- **Run programs** from `/bin` and `/usr/bin`, and from Homebrew (`/opt/homebrew/bin` and the `Cellar` and `opt`
  folders that its links point to), for example `ffmpeg`, `jq` and Homebrew's `python3`. Pipes, background jobs and
  child processes work.
- **Read** the system folders that the programs need (`/usr/lib`, `/usr/share`, `/usr/libexec`, `/System/Library`,
  the OS cryptexes, the time zone data), Homebrew's `bin`, `Cellar`, `opt`, `lib`, `share` and `Frameworks`, and the
  work folder.
- **Read the granted folder**, when the chat has one, without changing it. The host checks it as the file tools do
  (`O_NOFOLLOW` and `F_GETPATH`): a folder that was moved or replaced by a link is refused. Hidden files (any path
  part that starts with `.`) and the secret files of the file tools are refused inside it.
- **Write** in the chat's work folder only (`outputs/chats/<id>/ws`, the current folder and `HOME`). The files stay
  for later calls in the same chat. The page shows images inline and links other files.

What it cannot do:

- **Other programs**: exec from any other folder is refused: the work folder, `/usr/sbin`, `/sbin`,
  `/usr/libexec`, the app's own virtual environment. A script in the work folder runs with `sh script.sh`, not
  `./script.sh`. The Xcode tools in `/usr/bin` (`git`, `python3`, `clang`, `make`) do not work, because they need
  Xcode's folders. Their Homebrew versions work.
- **More rights**: setuid and setgid programs (`sudo`, `su`, `ps`, `top`, `login`, `crontab`...) are refused, by mode
  and by name.
- **The home folder**: nothing of it can be read or written, except the granted folder (read-only). No `~/.ssh`,
  keychains, browser data, the app's folder or other chats.
- **Other processes**: `(deny process-info*)` and `(allow process-info* (target self))` hold for every child too. No
  child reads another process's command line or environment (`KERN_PROCARGS2`), the process list or the paths of
  other processes. `ps`, `/bin/ps` and `top` are setuid (refused), `pgrep` finds nothing, `sysctl` is in
  `/usr/sbin` (refused). Signals reach only the run's own processes.
- **Network**: no rule allows it. Every socket (TCP, UDP, Unix) and every DNS lookup is refused.
- **System services**: the only `mach-lookup` rule allows one random name that no service has (see below). The
  pasteboard, the keychain, Apple Events, Spotlight and launchd are out of reach.
- **Input**: stdin is `/dev/null`. An interactive program gets no input.

The environment has only `PATH` (Homebrew's `bin`, `/usr/bin`, `/bin`), `HOME` (the work folder), `TMPDIR` (a
`.tmp` folder in it that the host removes after each call), `TMPPREFIX` (for here-documents), `LANG` and `SHELL`.

Limits:

| Limit | Value | How |
|---|---|---|
| Wall-clock time | `timeout_s`: 120 s by default, at most 600 s; less when the answer ends sooner (as for `run_python`) | The host kills every process of the run. |
| CPU time | `timeout_s` times the number of cores, per process | `RLIMIT_CPU` |
| Processes | 64 more than the user had at the start | `RLIMIT_NPROC` |
| Memory | 2,048 MB for all processes of the run | The watchdog. |
| One file | 50 MB | `RLIMIT_FSIZE` |
| Work folder | 500 MB, 10,000 entries | The watchdog; files that a call added past the limit are removed when it ends. |
| Output kept | 20,000 characters of stdout and of stderr each | The tool cuts the rest. |
| Command | 20,000 characters | The tool's schema. |

A child can leave the process group (`setsid`). So the host finds the run's processes by their sandbox: the profile
allows `mach-lookup` of one random name, and the host asks the kernel (`sandbox_check`) which of the user's processes
have a sandbox that allows that name and refuses a second random name. Only this run's profile has that name. A
timeout, Stop or a limit kills the process group, and so does the end of the main process. Then the host hunts the
rest: it kills each process of the run the moment it finds it, and it looks again without a pause until it found
none for 0.3 s. So a background job, a child in its own session and a process that forks and exits in a loop (to
slip between two looks) all end with the call. At its first use the app checks with one sandboxed `sleep` that it
can find a run's processes. If it cannot, there is no `run_shell` tool.

When the hunt does not end within 5 s, or a process still holds the output pipes after it, the result says so. A
thread goes on hunting those processes, `run_shell` refuses new commands until it is done, and the host does not
clean the work folder of that call. When the host removes files from the work folder, it opens each folder from the
one above it with `O_NOFOLLOW`, so a folder that was replaced by a link is never followed. It clears the file flags
of each entry and its folder first, so an immutable file (`chflags uchg`) is removed too.

The result starts with `exit code: N (S s)`. Then `stdout (N characters):` and `stderr (N characters):`, each with
the text. The page uses the lengths to show the two parts. A long part is folded.

## The web tools

### `fetch_url`

1. Accepts only `http://` and `https://` URLs without a user name or password.
2. Resolves the host and refuses it unless every address is public. Loopback, private, link-local, multicast,
   reserved, CGNAT and documentation addresses are refused, also when an IPv4 address hides inside an IPv6 one.
3. Connects to an address it checked (a pinned IP). TLS still checks the real host name. So a DNS answer that changes
   after the check cannot point the request at this Mac or the local network.
4. Follows redirects by hand, at most 5, and checks the address again before each one.
5. Stops at 2 MB of body (also after decompression) and 15 s for the whole fetch, redirects included.
6. Sends no cookies, no credentials and no proxy. The User-Agent names the app and its repository.
7. Turns HTML into text with the page's links. Other text types come as they are. Binary content is refused.
8. Returns the text in parts of at most 10,000 characters. A part ends after a line break when there is one near its
   end. When the page is longer, the result says the part's range, the page's length and the `start` of the next
   part. A call with `start` greater than 0 reads from a copy of the page that this chat fetched in the last 10
   minutes, without a new download. A call with `start` 0, or without a copy, downloads the page with every check
   above. The copies are kept per chat and URL, at most 8,000,000 characters for all chats together (the oldest go
   first), and only in memory.

### `web_search`

`web_search` asks these sources one after another, never in parallel. It merges the results and drops duplicates.

1. A SearXNG instance, when `[agent] searxng_url` is set.
2. Wikipedia (the MediaWiki search API) in `[agent] wikipedia_language`.
3. Marginalia Search with the shared key `public`, when `[agent] marginalia` is true.

It does not scrape search engines. Each request takes at most 10 s. A source that fails is named in the result. The
search fails only when every source fails.

## MCP tools

The user adds MCP servers in Settings > MCP servers ([MCP.md](MCP.md#mcp-client), user guide:
[MCP servers](guide/mcp-servers.md)). `app/agent/mcp_tools.py` gives their tools to chats as a Provider
(`app/agent/extensions.py`).

- A chat gets the approved tools of each server that is on, approved and not hidden. A tool is named
  `<server>__<tool>`.
- Every MCP tool is R2, also when the server marks it read-only: tool annotations are untrusted. Its result is
  untrusted and taints the chat. So each call asks, and "Allow in this chat" is not offered.
- The approval card says what runs: "Runs the tool X of the MCP server Y, a program on this Mac with your user
  rights", or for a remote server "Sends a call of the tool X to the remote MCP server Y (host), off this Mac". A
  remote server's tools have `network` set and name the host.
- Deferred (the default): the tool is not in the tools array; the model finds it with tool search. Direct: it is in
  the tools array (at most 4 MCP tools). The prompt section "MCP servers" lists only the servers (at most 150
  tokens).
- The model gets the definition the user approved. Before each call the app lists the server's tools again; a
  changed or removed tool is refused until the user approves it again.
- An error of the tool or the server, a timeout (120 s) or a failed start (30 s) becomes the tool's error text. Only
  that server's tools fail.

## Skills

A skill is a folder with a `SKILL.md` file in the [Agent Skills format](https://agentskills.io/specification). It
teaches the agent one kind of task. The code is in `app/skills.py` (the skills, the catalog, `load_skill`, `/skill`
and the import) and `app/skills_api.py` (the endpoints of Settings > Skills, see [API.md](API.md#skills)). The
user's guide is [guide/skills.md](guide/skills.md).

### Sources

The app reads skills from three sources. When two skills have the same name, the first source wins, and the other
skill gets the error "... skill with the same name replaces it".

| Source | Folder | Default | The app can |
|---|---|---|---|
| Imported (`user`) | `outputs/skills/` | on | import, switch, delete |
| Your folder (`folder`) | set in Settings > Skills (for example `~/.agents/skills`) | off | switch; it only reads the folder |
| App (`app`) | `skills/` in the repository | on | switch |

The switches and the folder are in `outputs/skills.json`. A folder in "your folder" without a `SKILL.md` is left
out. A folder in the other sources without one is listed with an error.

### `SKILL.md` rules

The file starts with YAML frontmatter between two `---` lines. The Markdown after it is the body: the guide that the
agent reads. A skill that breaks a rule is listed with its error and is never used.

| Field | Rule |
|---|---|
| `name` | Required. 1-64 characters: `a-z`, `0-9` and single hyphens, not at the start or the end. It must equal the folder's name. |
| `description` | Required. 1-1024 characters. What the skill does and when to use it. |
| `license` | Optional text. |
| `compatibility` | Optional. 1-500 characters. |
| `metadata` | Optional. Keys and values must be texts (`version: "1.0"`, not `version: 1.0`). |
| `allowed-tools` | Optional. A text of tool names with spaces between them. |
| `disable-model-invocation` | Optional, `true` or `false` (a Claude Code field). With `true`, only `/skill <name>` loads the skill. |

- `SKILL.md` must be UTF-8 and at most 256 KB, and the frontmatter at most 16 KB. The body must not be empty.
- Another field (such as `when_to_use` of other agents) gives a warning, not an error.
- A body of more than 5,000 tokens gives a warning. `load_skill` returns at most 12,000 characters (`MAX_RESULT`).
- A text with a colon needs quotes. YAML reads `on`, `off`, `yes` and `no` as true or false, so such a `name` is an
  error.

### The catalog and `load_skill`

The skills provider (`SkillsProvider`, loaded by `app/agent/providers.py`) gives a chat two things:

- The section "Skills" of the system prompt: one lead line, then one line for each skill that is on, with its name
  and description. It may hold at most 600 tokens (`BUDGET`). When a skill does not fit, it is left out, and the
  last line says how many are left out. The three skills of the app take about 280 tokens.
- The core tool `load_skill(name, file)`. `name` is an enum of the skills that are on (without
  `disable-model-invocation` skills); `file` is optional. The tool and its schema take about 170 tokens. With no skill on, there is no tool and no section.
  A chat with tools off (the Tools switch) gets neither.

`load_skill` is R0: it runs without asking. Its result is the body in `<skill_content name="...">`. When the skill
has other files, the result also gives the skill folder's full path and a `<skill_resources>` list (scripts are
marked). The result is untrusted (`untrusted_output`), so it taints the chat like a web page: an imported skill is
text from outside the app. A skill that was turned off or deleted after the chat started gives an error.

With `file`, `load_skill` returns one file of the skill's `<skill_resources>` list in `<skill_file skill="..."
path="...">`, at most 200,000 bytes. This works in a chat without a granted folder. The file must be in the list (no
hidden or secret files), must stay in the skill folder when its links are followed, and must be text. The result is
untrusted, as the body is.

The read tools (`list_files`, `read_file`, `search_files`) also read inside a skill folder when the path is the full
path of a file there. Only the folders of skills that are on count. The rules of the granted folder apply: no hidden
or secret files, real paths only. The write tools never write there. The policy still asks for a granted folder
before any R1 tool runs, so in a chat without one, `load_skill` with `file` is the way to read a skill's files.

Skill scripts never run by themselves. To run one, the agent reads it and gives its code to `run_python` or
`run_shell`. The user approves each call and sees the code on the approval card. `allowed-tools` never approves a
call.

### Hidden skills

A skill is left out of a chat's catalog when the chat lacks what it needs:

- A tool that `allowed-tools` names. A name with arguments counts by its base (`Bash(git:*)` is `Bash`). The names of
  Claude Code map to this app's tools: `Bash` is `run_shell`, `Read` is `read_file`, `Write` is `write_file`, `Edit`
  is `edit_file`, `Grep` is `search_files`, `Glob` and `LS` are `list_files`, `WebFetch` is `fetch_url`, `WebSearch`
  is `web_search`. A tool that this app does not have hides the skill.
- The network, when `compatibility` asks for it ("Requires ... internet", "Needs network access"). A sentence with
  "no", "not", "without" or "offline" does not count.

The chat's tools are the ones that are on in its Tools menu (`skills.chat_tools()`). When the chat has `"offline":
true`, its network tools do not count. `load_skill` still accepts a hidden skill by name. Its result then starts with
a note that names what the chat lacks.

### `/skill <name>`

A message that starts with `/skill <name>` forces that skill. The server handles it (`start_answer` in
`app/chat.py`), so the page needs no change:

1. The server finds the skill among the skills that are on. When there is none, the message is refused with HTTP 400
   and the names of the skills that are on.
2. The user message is stored as typed, with a `skill` key: `{"name", "source", "command", "content"}`. `content` is
   a note that the user chose the skill, then the same text as `load_skill` gives. That text is cut at 24,000
   characters (`FORCED_MAX`), with a note that `load_skill` gives all of it.
3. For the model, the command is removed from the text, and `content` goes before the message (before its files).
4. A skill from another source than the app taints an agent or workstation chat, as an attached file does.

A `disable-model-invocation` skill works with `/skill` too.

### Import

Settings > Skills imports a skill folder or a `.zip` from this Mac. It never downloads.

1. Check: the server copies the skill to `outputs/skills/.staging/<token>/`. It refuses links, paths outside the
   skill, more than 500 files and more than 20 MB. It leaves out hidden and secret files and names them. A `.zip` may
   hold the skill at its root or in one folder; the skill takes the `name` of its `SKILL.md`. A broken `SKILL.md`
   is refused.
2. Scan: each file (at most 2 MB) is searched for dangerous patterns: a download piped into a shell or an
   interpreter, base64 decoding and long base64 blobs, "paste in Terminal", `sudo`, turning off macOS protections,
   `osascript`, keys and the keychain, `.env`, `rm -rf`, network shells, `eval`, `chmod +x`, login items and timers,
   text that tries to change the agent's rules or hide something from the user, and hidden or bidi characters. A
   binary file is named, because the scan cannot read it.
3. The page shows the files, the scripts, the findings and the `SKILL.md`. The user confirms (a dialog when there are
   findings or scripts).
4. Confirm: the checked copy moves to `outputs/skills/<name>/` and is on. An unconfirmed copy is removed after an
   hour.

The scan only helps the user decide. It cannot prove that a skill is safe.

### The app's skills

| Skill | Needs | What it does |
|---|---|---|
| `short-film` | `list_workstation_tools`, `create_job`, `job_status` | Plans 3-6 shots, then queues images, Klein Edit for the same character, voice, clips (`video.i2v`, `video.a2v`), the join (when a join tool is installed), music and `edit.mix`, one stage per answer. |
| `podcast-cleanup` | `run_shell`, `media_info` | Shortens silences longer than 0.5 s with ffmpeg `silenceremove`, sets the loudness to -16 LUFS with a two-pass `loudnorm`, writes show notes from `transcribe_audio`. |
| `subtitle-video` | `transcribe_audio` | Transcribes, writes an SRT (cues of at most 2 lines of 42 characters and 7 s), and adds a soft `mov_text` track with ffmpeg in `run_shell`. It says which step a missing tool stops. |

Each body is under 1,600 tokens.

## What is not protected

- Seatbelt (`sandbox-exec`) is deprecated by Apple. It works on current macOS, but Apple may remove it.
- The memory watchdog polls. Between two polls the code can use more memory than the limit.
- The code can `stat` any path, so it can learn which files exist and their sizes and dates.
- The code can read the Mac's name (`kern.hostname`), which often holds the user's name: `os.uname()` needs it,
  and `ctypes` and `platform` call that at import.
- The folders of a symlinked interpreter's target are not checked. A Homebrew `Cellar` folder, for example, is
  writable by the `admin` group.
- A DNS lookup and the HTML parsing of `fetch_url` cannot be interrupted. Stop takes effect after they end.
- The SearXNG URL is trusted. Its address is not checked, so it can be on this Mac or the local network.
- The granted folder is not mounted in the `run_python` sandbox: it cannot read the user's files. Only the file tools
  can (as text for the model, or as a copy in the work folder with `copy_to_workspace`), and `run_shell`, which can
  read the granted folder (not its hidden and secret files).
- In the granted folder, `run_shell` can list the names of hidden and secret files (`ls -a`), but not read them. The
  file tools do not show those names.
- `run_shell` can start every program in Homebrew's folders. Those folders often belong to the `admin` group, so
  another admin user could put a program there. The program still runs in the sandbox.
- When the app is killed hard (not closed) while `run_shell` runs, nothing stops the command's background
  processes. They stay in the sandbox: no network, writes to the work folder only.
- The process limit counts all of the user's processes (`RLIMIT_NPROC`). When the user starts many programs during
  a call, the call can start fewer processes.
- `ffprobe` and `ffmpeg` of the media tools run in a sandbox, but the media jobs (and the transcription's ffmpeg
  step) parse their input files outside it. A file made to attack a decoder could reach them. A very large image
  (a "decompression bomb") can still use much memory in `view_image` for up to 30 s.
- macOS does not check the creation of System V IPC objects in a sandbox: code in `run_shell` (for example with
  `perl`) can create a shared memory segment, a message queue or a semaphore set, and it stays after the run. The
  sandbox refuses their use, so no data passes from one run to another; the empty objects stay until the Mac
  restarts or the user removes them (`ipcs`, `ipcrm`).
- Code in the sandbox can set extended attributes and file flags in the work folder, because `cp`, `tar` and
  Python's `shutil.copy2` set them and fail without them. The disk limit counts a file's blocks and its extended
  attributes, so a resource fork or a large attribute cannot pass it. The host clears the flags (`uchg`) before it
  removes a file of the work folder, and when it deletes a chat.
- A change by `edit_file` is a new file renamed over the old one. The mode stays, but extended attributes, ACLs and
  the owner of the old file do not carry over.
- The backups of `edit_file` are in the work folder, where later `run_python` or `run_shell` code can change or
  delete them.
- `view_image` does not read HEIC. Convert the image first (for example with `run_python` or Preview).
- An image attached to a message does not taint the chat, also when it shows written orders.
- The host tools run outside the sandbox. `textutil` parses the documents of `convert_document` there, as it parses
  attached files. `-noload` stops HTML from loading resources, but a file made to attack `textutil` could reach it.
- When Stop or the time limit kills `shortcuts run`, the shortcut itself can go on in the Shortcuts service. A
  shortcut that waits for an answer from the user (a dialog) runs into the 120 s limit.
- Spotlight finds only indexed files. A new file can take a moment, and a folder that Spotlight skips gives no
  results. `search_files` reads the text files directly.
- The readers of attached files run outside the sandboxes: PDFKit in a child process of the app, `textutil`, and the
  standard library's zip and XML parsers. A file made to attack them could reach them. The limits (file size, zip
  size and part count, no XML `DOCTYPE`, 60 s per reader) make most such files fail.
- An MCP server of the "local program" kind runs outside the sandboxes, with the user's rights. The approval of
  its command line is the only check. It can read and change the user's files and use the network.
- The values of an MCP server's secrets go to `/usr/bin/security` as arguments. While that program runs (a few
  milliseconds), another process of this user can read them in the process list.
- A local program can send any `Origin` header. The same-origin check stops other web pages. Programs on this Mac
  need the session cookie, which only the token file of this user gives (docs/API.md). A program that runs as this
  user can read that file.
- In `--browser` mode the browser sends the session cookie to every server on 127.0.0.1, at any port, that its pages
  open. Use the app window when other local servers run.

## How the chat loop uses the package

`app/chat.py` builds a registry with `build_registry(port, settings)` at the first use and keeps it. A workstation
chat uses `workstation_registry()` instead (see [Workstation chats](#workstation-chats)). It keeps one
registry per media flag, so at most one per profile: with the media tools (the full profile; the port is the server's
`Workstation` object from `app/server.py`) and without them (the chat profile; the port is `None`). One answer
(`Turn`) runs these steps:

1. Take the chat's frozen prompt: its tools array and system text (see [The prompt and the prompt
   cache](#the-prompt-and-the-prompt-cache)). When the tools that are on, the folder or the installed media tools
   changed since the model last heard of them, add the app's note (a user message, see [Notes for later
   changes](#notes-for-later-changes)).
2. Build the model input. The budget counts the system prompt, the tool schemas and the tool messages. Old tool
   results are shortened first, then old turns are dropped, until the input fills 60% of the budget (`TRIM_TO`).
3. Run one model request with the schemas. With no calls, the answer ends. When the content has no call, the worker
   looks for calls in the reasoning (Qwen sometimes writes its call while it thinks; see [Guards for small
   models](#guards-for-small-models)).
4. For each call: unwrap `tool_call` (the call is then the tool it names), check Stop and the time, check the format,
   check for a repeat, then call `decide(tool, grants, tainted, enabled)`. The second identical call (same tool, same arguments) in one answer does
   not run when the first one returned `ok`: its result says that the call already ran and its result is above. The
   third one ends the answer with a note. The rule skips a tool with `repeatable=True`: its result can change between
   two identical calls (jobs, files, web pages). After a tool with `writes=True` or `side_effects=True` ran (it
   changes the files or the jobs that other tools read), the rule forgets the earlier calls. That call itself still
   counts, so the same job is not queued twice.
5. On "ask", make the tool's preview (`edit_file`'s diff); a call that cannot run goes back to the model unasked.
   Then show the approval card and wait, at most 10 minutes. No answer counts as a denial. "Allow in this chat"
   adds the tool to the chat's grants. An answer that the server accepted (HTTP 200) counts, also when it came in the
   moment the wait timed out. The card shows `describe` and the arguments with bidi, zero-width, tag and control
   characters written as `\uXXXX`, and for `fetch_url` the host as the network sees it (IDNA, `xn--...`) on its own
   line.
6. Run the call with `registry.call(name, args, Context(chat_id, folder, abort, work_dir, deadline, preview))`.
   `deadline` is when the answer must end, so a tool that waits stops in time. `preview` is the diff the user
   approved. Then update the taint flag with `taints(tool, tainted)`. The images of a result (`view_image`) go into
   its tool message.
7. Add a tool message with the result and go to the next step. A result over 12,000 characters is saved whole in the
   work folder first (see [Long results](#long-results)). For `update_plan`, also send the checked items to the page
   (a `plan` event) and keep them in the tool message.
8. An answer has at most 16 steps (`MAX_STEPS`) and 10 minutes. The time a step waits for memory (a media job runs)
   does not count against the 10 minutes, and Stop ends that wait.
9. The last step cannot call tools. Its request keeps the same tool schemas, so the prompt's start does not change
   and the prompt cache holds. It adds a user message from the app (`NUDGE`: no more tools, give the final answer)
   and sends `tool_choice: "none"`, so the worker bans the call marker token. The answer then ends with the model's
   summary and the step-limit note. The saved final message keeps that user message as `nudge`, so the next turn's
   prompt still starts with the text that the cache holds.
10. After the answer ends, `memory.check_claim()` adds a note when the answer says that it saved something to
    memory and no `memory` call ran (see ["Saved" without a call](#saved-without-a-call)).

The worker bans emoji in every request with a logits processor (`ByteBan` and `NoEmoji` in `app/llm_worker.py`).
The prompt rule alone did not stop them, also not in code. The banned set is built once from the tokenizer's
vocabulary. Byte-level BPE can split one emoji over two tokens, so the ban follows the unfinished character at the
end of the text so far: a small state machine whose tables are arrays, so a step never waits for the token before
it. The banned code points are the Misc Symbols and Dingbats blocks (but the music signs, as in "C♯"), the emoji
among the technical and geometric symbols, the keycap mark, the emoji variation selector, the flag tags and
U+1F000 to U+1FFFF. Arrows, box drawing, math signs and (c) stay allowed. Inside a tool-call block (from the call
marker token to the end marker token) nothing is banned: the arguments are data, such as the text of a file that
`write_file` writes. A request with `"allow_emoji": true` (`llm.generate(allow_emoji=True)`) has no ban.

### The prompt and the prompt cache

The worker keeps a chat's prompt cache only while each new prompt extends the one before: Qwen3.8's linear-attention
layers cannot rewind, so any change reads the whole chat again. The Qwen3.8 chat template puts the tools array before
the system text. So a change to either one reads the whole chat again: about 8 s for 4,000 tokens on an M5 Pro, two
to four times more on a smaller Mac.

So a chat fixes its prompt at its first turn (`freeze` in `app/chat.py`) and keeps it in `chat.json` under `prompt`
(see `docs/API.md`). Every later request sends exactly the same tools array and system text. The chat fixes:

- the tools array: the names, and the schemas as they were then;
- the system text: the text of the mode, then the providers' sections in order;
- the date, to the day;
- whether the Mac had a network (see [Offline](#offline));
- the providers' tools (see [Providers](#providers));
- the tools that were on, the folder and the media tools that were not installed, as the start of the notes.

These no longer change the prompt of a chat that has started: the Tools menu (tools on or off, the folder), a tool
that the app gets after an update (`known` in the agent state), a media tool that gets installed, the date, the
`[agent]` settings after a restart (the description of `run_python`), the network, and the providers' tools and
sections. The mode is fixed after the first message in any case. A chat saved before this has no `prompt`: it gets
one at its next turn, which reads the chat once more.

The thinking level is not fixed. The template writes a line for the reasoning effort before the tools array, so a
message with another level (for example `off`, then `low`) reads the whole chat again. The levels `off` and `medium`
have no such line, so a change between those two keeps the cache.

#### Prompt layout

The system turn of a request has these parts, in this order:

1. The template's reasoning-effort line (it depends on the thinking level).
2. The tools array: the core tools, then `tool_search`, `tool_describe` and `tool_call` when the chat has deferred
   tools.
3. The system text: the text of the mode (`agent_prompt`, `workstation_prompt` or `general_prompt`). An agent chat's
   text names the tools of the array, then lists the deferred tools, one line per group ("- Media jobs: list_jobs,
   wait_for_job, ..."). A group whose tools need a folder (none is granted) or the network (offline) says so. Then
   come the rules, the step limit, the internet line, the folder line and two short example runs
   (`AGENT_EXAMPLES`, about 250 tokens).
4. The providers' sections, each as "Title:" and its text.

#### Core and deferred tools

Each mode names its core tools (`CORE` in `app/chat.py`). A core tool goes in the tools array when it is on and can
run: a file tool needs a granted folder, a web tool needs the network. Each provider names its own core tools.
Every other tool that is on is deferred: the system text lists its name, and the model reaches it in one of two ways:

- `tool_search` (BM25 over the name, the group, the description and the parameter names; an exact name comes first)
  or `tool_describe` gives the schema. Then the model calls the tool by its name, or with `tool_call`.
- The loop knows every tool of the chat, not only the array. A direct call to a deferred tool runs. The worker types
  the arguments by the schemas of the array, so a deferred tool's numbers and switches come as text; the loop turns
  them into the type of the schema (`deferred.coerce`).

`tool_call` is unwrapped before anything else: the policy, the approval card, the taint flag and the repeat rule see
the real tool. The tool message answers the model's `tool_call` by its id. The `tool_call` event has `"via":
"tool_call"`.

| Mode | Core tools |
|---|---|
| Agent | `update_plan`, `web_search`, `fetch_url`, `list_workstation_tools`, `create_job`, `job_status`, `wait_for_job`, `search_library`, `list_files`, `read_file`, `write_file`, `edit_file`, `media_info`, `run_python` |
| Workstation | all its tools but `ocr_document` (9). `ocr_document` is deferred, because most chats never read a scan; the bridge tools reach it (memory's deferred `search_chats` puts them in the array anyway). The prompt names `ocr_document` and says that it returns the text of a picture. |
| General | none: a general chat has no tools |

The agent's deferred tools are `list_jobs`, `cancel_job`, `transcribe_audio`, `ocr_document`,
`system_status`, `search_files`, `copy_to_workspace`, `export_from_workspace`, `view_image`, `run_shell` and
`read_result` (the last one is not in the listing: the note of a cut result names it).

The prompt of each mode (the template's system turn with the tools array, thinking off; real tokens of the Qwen3.8
tokenizer, and characters / 3):

| Chat | Before | After |
|---|---|---|
| Agent, no folder, defaults | 14 tools, 3,088 tokens (3,621) | 10 tools, 2,520 tokens (2,963) |
| Agent, a folder, `run_python` and `run_shell` on | 23 tools, 4,934 tokens (5,845) | 15 tools, 3,442 tokens (4,079) |
| Agent, offline, no folder | - | 8 tools, 2,222 tokens (2,597) |
| Workstation | 9 tools, 4,921 tokens (5,368) | the same, before Read Text. With `ocr_document` deferred and the default providers (memory and skills), the bridge is already in the array (memory defers `search_chats`), so `ocr_document` adds 45 tokens; as a core tool it would add 370 (system text and schemas only, measured 2026-11-05) |
| General | 78 tokens (105) | the same |

The eval showed that the agent spent steps to find `wait_for_job` and `search_library`, so they became core
after this measure: each agent row above is about 350 tokens (characters / 3) larger now.

#### Notes for later changes

A change after the first turn reaches the model as the app's note: a user message at the end of the chat, before the
next model step. It starts with "[The app: this chat's settings changed." and says which tools are now on or off, the
folder that the user granted or took back, and the media tools that are now installed or no longer installed. The
step's saved message keeps the note as `notice` and the state it told as `told`, so the next turns send the note in
the same place and do not repeat it. A tool that is off is refused by the policy at once; the note only tells the
model why.

#### Offline

At a chat's first turn, `online_check` (`network_up`) looks at the Mac's routing table: it connects a UDP socket to
a documentation address, which sends no packet. Without a route the chat is offline: the web tools are deferred, their
group in the listing says "(need the network)", `tool_search` and `tool_describe` add "Needs the network" to their
description, and the system text says that the Mac had no network when the chat started. The check runs once per
chat. A web call still runs when the network comes back.

#### Providers

A provider (`app/agent/extensions.py`) adds tools and one short section. `freeze` asks the providers once, at the
chat's first turn (`extensions.collect(providers.load(), chat)`). The chat keeps the names of the providers' tools
(`prompt.providers`). While the app runs, the tools stay in memory per chat. After a restart the loop asks the
providers again and keeps only the tools with those names: a tool that is gone gets "There is no tool". So an MCP
server's tools can change between chats, never inside one. A provider tool whose name the app already uses is left
out, with a line in `prompt.problems`. A general chat gets the sections but no tools. A provider sees the chat
(`chat["mode"]`) and can leave its section out.

#### Long results

The model gets at most 12,000 characters of a result. In an agent chat a longer result is saved whole as
`results/<call id>.txt` in the chat's work folder (at most 4 M characters). The model gets the start of the result and
a note with the file name and the offset to read on with `read_result`. The page links the file. Every part of the
path is opened with `O_NOFOLLOW`, because the sandboxed code writes in the work folder too: a link that it made
cannot send the write or the read out of the folder. `run_python` and `run_shell` can read the file as well.

#### Guards for small models

The loop and the worker catch the mistakes that a 27B model makes most:

| Mistake | What happens |
|---|---|
| An unknown tool name | The tool message names the nearest names and the tools in the array. |
| Invalid arguments | The tool message gives the error and the tool's parameters as compact JSON (at most 1,500 characters). |
| A malformed call | The tool message gives the error and the tool's parameters. When one parameter is to blame, the error names it and the place in it (for example JSON that is not valid). The next request shows the model the call as it wrote it, not an empty call. The first malformed call of an answer gets one extra step: that step does not count against the step limit (one automatic retry). |
| A bare size in JSON | Qwen often writes `"size": 1344x768` without quotes in `create_job`'s `fields`. That is never valid JSON. When a call does not parse, the worker puts such a size in quotes in each object or array parameter, outside JSON strings, and parses the call again. Text parameters stay as written. |
| A call in the reasoning | When the answer has no call, the worker takes the complete call blocks at the end of the reasoning. When the answer has no text either, it takes every complete call block in the reasoning, each one once. When the reasoning ends inside a call (the thinking budget cut it), that call comes back as a malformed call. A call in the reasoning of an answer with text is a draft: it does not run. |
| The same call again | The repeat rule (step 4 above): the second identical call does not run, the third one ends the answer. |
| The same failing call again | A call that failed (an error or a refusal) runs again once, and its result adds that it failed the same way before. The third identical failure does not run and ends the answer (`finish` `repeat`, a note). For a malformed call, "identical" means the same text. An `ok` result of any call starts the count again. |
| The same text again and again | When a step's reasoning or content ends with one block repeated 4 times or more, 400 characters or more in all, the loop cancels the step and ends the answer (`finish` `repeat`, a note). The saved message keeps the first block and a marker instead of the repeats, so the model never reads the loop again. |

The repeat rule works on calls; the text guard works on the text of one step. Neither one needs the other. A retry
does not run the step again from the same prompt: that would read the whole chat again. The model writes the call
again in the next step, after the error.

A full context also changes the prompt's start: old turns must go. A trim to just under the budget would drop one
turn on each new turn, and each turn would read the whole chat again. So a trim goes down to 60% of the budget
(`TRIM_TO`), and the chat stores where the input now starts (`window` in `chat.json`). The next turns start at the
same message and extend the same prompt, until the context is full again.

These steps also read the whole chat again (mlx-vlm 0.7.11):

- The step right after a new `view_image` result. mlx-vlm reuses a cached prefix only when the new part of the
  prompt holds no image.
- A step whose prompt holds an image, when the model sampled a token split that a fresh tokenization does not give.
  Without images the worker gives mlx-vlm the cached token ids plus the ids of the new text (`extend_ids`), so the
  cache always fits. With images the image tokens come from the processor, so the worker lets mlx-vlm tokenize the
  whole prompt, and the cache fits only when that tokenization starts with the cached ids. The worker does not
  build the ids itself here: the Qwen mRoPE positions after an image depend on the processor's image grid, and a
  wrong position would give wrong answers without an error.
- The step after a call that the worker took from the reasoning (Qwen wrote it while it thought). The saved step has
  the call as a normal tool call after the reasoning, so the new prompt does not start with the text the model
  wrote.
- The step after a loop that the text guard stopped, and any step after a cancelled one: the worker drops the cache of
  a cancelled request.

Only the newest 2 tool images go to the model (`MAX_TOOL_IMAGES`). An older one is dropped from its tool message,
which gets a note instead. This changes an earlier message, but it happens only in the request that adds a new
image, which reads the chat again anyway. So the drop costs no extra read.

All steps run in one `llm.session()`. Its time limit counts from the moment it first holds the memory. `create_job`
never waits for its job, because the job waits for that session. `wait_for_job` and `transcribe_audio` wait only for a
job that can run next to the model (see [Waiting for a job](#waiting-for-a-job)). When the app closes, every running answer gets
Stop (a waiting approval ends as stopped) and a few seconds to save what it has. `docs/API.md` lists all rules of the loop.

Each model request of an answer uses the thinking level of the message (`app/effort.py`; docs/API.md, "Thinking
levels"). The budget applies to each step: the first step gets the full thinking budget to plan the work, each later
step gets 40% of it. The template effort stays the same in every step, so the prompt cache stays valid. At `max` no
step has a budget; a long thought can then reach the 10-minute limit of the answer.

In an agent chat, a Markdown link shows its host after its label ("label (host)"), and a long bare URL is shortened
with the whole URL in its tooltip. A web page can tell the model to write a link that carries private text in its
address: the host shows where one click sends it. A link with a query shows the query too ("label
(host/path?query)", cut at 80 characters), so the text that it sends is visible before the click.

## Settings

The `[agent]` section of `config.toml` sets the sandbox interpreter of `run_python` and the sources of `web_search`.
Settings > Agent edits it (in both profiles). The app reads it once, at start, when it builds the agent tools. A
change takes effect after a restart; the Settings screen shows the restart note until then.

| Key | Default | Meaning |
|---|---|---|
| `python` | `""` | The interpreter `run_python` runs in the sandbox. Empty = the app's own base Python with `-I -S`: the standard library only. |
| `searxng_url` | `""` | The base URL of a SearXNG instance you run yourself, with the JSON format on. `web_search` asks it first. Empty = not used. |
| `wikipedia_language` | `"en"` | The Wikipedia that `web_search` asks. |
| `marginalia` | `true` | `web_search` also asks Marginalia Search with the shared key `"public"`. That key is often rate limited. |

### Rules

The Settings screen (`PUT /api/settings`, see [API.md](API.md#settings)) refuses a value that breaks these rules:

- `python`: `""`, or an absolute path ("~" is expanded) to an executable file that is there now. No other user may
  be able to change it (see [The interpreter](#the-interpreter)).
- `searxng_url`: `""`, or an `http://` or `https://` URL with a host. It cannot hold a user name or password, a query
  (`?`) or a fragment (`#`).
- `wikipedia_language`: a language code: two or three lowercase letters, then an optional `-` and lowercase letters
  (`en`, `de`, `pt-br`). The screen offers en, tr, de, fr, es, it, ja and zh. Another code works when you set it in
  the file by hand.
- `marginalia`: `true` or `false`.
- A text value cannot hold a double quote, a backslash or a line break, and is at most 1024 characters.

At start the app checks the same rules, except that `python` may be a relative path (counted from the app folder) and
need not exist: a bad value stops the start with a message. When the `python` file is missing, or other users can
change it, the agent has no `run_python` tool.

### A sandbox environment with packages

The default sandbox Python has the standard library only. For numpy, pandas or matplotlib, make a separate
environment for the sandbox. Do these steps in Terminal, with [uv](https://docs.astral.sh/uv/):

1. Make the environment:

   ```sh
   uv venv --python 3.12 ~/mlx-media/venv-sandbox
   ```

2. Install the packages:

   ```sh
   uv pip install --python ~/mlx-media/venv-sandbox/bin/python numpy==2.5.4 pandas==3.1.0 matplotlib==3.11.2
   ```

3. In Settings > Agent, enter `~/mlx-media/venv-sandbox/bin/python` as the sandbox Python and press Save.
4. Restart the app.

The sandbox runs this interpreter with `-I`: its own packages work, but the user site folder and the `PYTHON*`
variables do not. The sandbox may read the environment's folder.
