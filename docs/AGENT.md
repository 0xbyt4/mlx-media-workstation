# Agent mode

In an agent chat the local model can call tools. It can read the Workstation's state, queue, wait for and cancel media
jobs, transcribe speech, read and write files in one folder, read media facts, look at images, search the web, read
web pages, and run Python and shell commands in a sandbox. The code is in `app/agent/`. The chat loop
that uses it is in `app/chat.py`. The endpoints and events are in `docs/API.md` under "Agent mode".

## Tools and risk levels

Each tool has a risk level. `app/agent/policy.py` decides from that level whether a call runs, asks or is refused.

| Level | Meaning | Rule |
|---|---|---|
| R0 | Reads app state only. | Runs. |
| R1 | Reads the user's files in the granted folder, or copies one into the chat's work folder. | Runs when the chat has a granted folder. Refused without one. The grant is the consent. `media_info` and `view_image` also run without a folder: they read the chat's work folder and the Library too. |
| R2 | Sends text off this Mac, queues minutes of compute, cancels a job, or writes a new file in the granted folder. | Asks on the first use in a chat. The user can answer "Allow in this chat", except for a tool whose result taints the chat (web_search, fetch_url, transcribe_audio): after its first result it asks every time, so a chat-wide grant would do nothing. When the chat is tainted, every R2 call asks, also with that grant. |
| R3 | Runs code, changes an existing file, or has effects the app cannot check. | Asks every time. "Allow in this chat" is not offered. |

A tool that is off in the chat's Tools menu is refused. An unknown tool name is refused. The model gets the reason as
the tool's result, so it can answer the user.

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
| `system_status` | R0 | Shows the Mac's memory, the running job and its progress, the queue, the chat model and whether each media tool fits now (`GET /api/status`). |
| `list_files` | R1 | Lists a folder inside the granted folder (at most 500 lines). |
| `read_file` | R1 | Reads a text file inside the granted folder (the first 1 MB), by line range (`start_line`, `end_line`). The result gives the line count and where to read on. It stops before the 12,000-character cap of a result. |
| `search_files` | R1 | Searches the text files of the granted folder (at most 5,000 files and 15 s). |
| `write_file` | R2 | Creates a new text file in the granted folder. Never overwrites. |
| `edit_file` | R3 | Changes an existing text file in the granted folder: an exact replacement or the whole text. The approval card shows the diff. A copy of the old file goes to the work folder first. |
| `copy_to_workspace` | R1 | Copies a file of any type (at most 500 MB) from the granted folder into the chat's work folder, for `run_python`. |
| `export_from_workspace` | R2 | Copies a file from the work folder into the granted folder, under a new name. |
| `media_info` | R1 | Duration, size, resolution, frame rate, codecs, channels and sample rate of an image, audio or video file (ffprobe). |
| `view_image` | R1 | Lets the model see an image: a copy of at most 1280 pixels on its long side goes into the next model request. |
| `web_search` | R2 | Searches the web. Sends the query off this Mac. |
| `fetch_url` | R2 | Reads one public web page as text, in parts of 10,000 characters. Sends the URL off this Mac. |
| `run_python` | R3 | Runs Python code in the sandbox. Present only when the sandbox works on this Mac. |
| `run_shell` | R3 | Runs a shell command (zsh) in the sandbox. Present only when the sandbox works on this Mac. |

The Workstation tools exist only in the full profile. The chat profile has no Workstation, so its registry has none
of them.

The MCP server offers the same Workstation tools to local agents, such as Claude Code ([MCP.md](MCP.md)). The tools
are built once (`ws_tools.build`). A `ws_tools.Host` holds what differs: the forms of a file reference, the words of
the results, and the time of a wait.

### Workstation chats

A workstation chat in the full profile runs the same loop (`Turn`) with the Workstation tools only:
`list_workstation_tools`, `job_status`, `search_library`, `create_job`, `list_jobs`, `wait_for_job`, `cancel_job`,
`transcribe_audio` and `system_status`. `workstation_registry()` in `app/chat.py` builds them with `ws_tools.build(port)`.

- It has no file, web, media, plan, Python or shell tool. A call to one of them is refused: no such tool.
- The tools keep their risk levels. `create_job`, `cancel_job` and `transcribe_audio` ask first, as above.
- The policy, the approval cards, the grants, the taint, the step limit and the memory lease are those of an agent
  chat. A job that needs the chat model's memory starts after the answer.
- It has no folder. A folder grant is refused (HTTP 400), so a `folder:` file reference fails. A `library:`
  reference works.
- The Tools menu turns each tool on or off and ends a grant. A new workstation chat has every tool on.
- The prompt lists every field of each media tool and tells the model when to queue a job: when the user asks it
  to make something. When the request is unclear, or the user wants to edit the text first, it writes `prompt`
  blocks instead.
- The registry needs no `[agent]` settings and no sandbox.
- In the chat profile a workstation chat has no tools. It answers in one model request, without the loop.

### Input files

A file field of `create_job` (`"type": "file"`) takes one file reference. A `files` field takes a list of them (at
most 10). `transcribe_audio` takes one in `audio`. A reference has one of these forms:

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
  `.png`, `.jpg`, `.jpeg` and `.webp`; `audio/*` takes `.wav`, `.mp3`, `.flac`, `.m4a`, `.aac`, `.ogg`, `.opus`,
  `.aiff` and `.aif`; `video/*` takes `.mp4`
  and `.mov`). A browser upload of another type is kept as `.bin`; the agent's is refused.
- The approval card lists the input files with the other fields. The risk stays R2: the job uses this Mac for
  minutes.
- The job stores the chat that queued it (`"chat"` in `job.json`).

### Waiting for a job

`create_job` returns at once. `wait_for_job` and `transcribe_audio` wait for a job. The answer holds the model's
memory lease. A job that fits next to the model (an image, a transcription on a 48 GB Mac) runs during the answer. A
job that does not fit (a video, for example) waits for the lease, so it starts only after the answer.

- The wait does not wait for such a job, or for a job behind one in the queue. It returns at once, and the model
  tells the user that the job runs after the answer. The answer does not give its lease up to such a job: the model
  would then wait for the whole job before its next step.
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
Every other file must be UTF-8 text up to 1 MB. `copy_to_workspace` copies any type up to 500 MB. Both check that
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

## Taint

Taint is a flag per chat. It is stored in `chat.json` with the grants.

- These tools set it when they run: `search_library`, `transcribe_audio`, `list_files`, `read_file`,
  `search_files`, `copy_to_workspace`, `media_info`, `view_image`, `web_search`, `fetch_url`, `run_python` and
  `run_shell`. Their results can hold text from outside the app (an image can hold written text too).
- A message with an attached file that is not an image (a document, a text file, an audio file) sets it when the
  message is saved, before the model reads the file. This applies to agent chats and to workstation chats with the
  Workstation tools. The file's text (or the audio's transcript) comes from outside
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
- The readers of attached files run outside the sandboxes: PDFKit in a child process of the app, `textutil`, and the
  standard library's zip and XML parsers. A file made to attack them could reach them. The limits (file size, zip
  size and part count, no XML `DOCTYPE`, 60 s per reader) make most such files fail.
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

1. Take the schemas of the tools the chat offers. The tools that need a folder are offered only when one is granted.
2. Build the model input. The budget counts the system prompt, the tool schemas and the tool messages. Old tool
   results are shortened first, then old turns are dropped, until the input fills 60% of the budget (`TRIM_TO`).
3. Run one model request with the schemas. With no calls, the answer ends. When the content has no call but the
   reasoning ends with complete call blocks (Qwen sometimes writes its call while it thinks), the worker takes the
   calls from there.
4. For each call: check Stop and the time, check the format, check for a repeat, then call
   `decide(tool, grants, tainted, enabled)`. The second identical call (same tool, same arguments) in one answer does
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
7. Add a tool message with the result and go to the next step. For `update_plan`, also send the checked items to the
   page (a `plan` event) and keep them in the tool message.
8. An answer has at most 12 steps (`MAX_STEPS`) and 10 minutes. The time a step waits for memory (a media job runs)
   does not count against the 10 minutes, and Stop ends that wait.
9. The last step cannot call tools. Its request keeps the same tool schemas, so the prompt's start does not change
   and the prompt cache holds. It adds a user message from the app (`NUDGE`: no more tools, give the final answer)
   and sends `tool_choice: "none"`, so the worker bans the call marker token. The answer then ends with the model's
   summary and the step-limit note. The saved final message keeps that user message as `nudge`, so the next turn's
   prompt still starts with the text that the cache holds.

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
layers cannot rewind, so any change reads the whole chat again. So the agent prompt does not change between turns.
It holds the offered tools, the tools that are off (by name, with the rule to ask the user to turn one on in the
Tools menu), the step limit, the folder and two short fixed example runs (`AGENT_EXAMPLES`, about 250 tokens). It
changes only when the user changes the tools or the folder, and when the date changes: the prompt has today's date,
so the first answer after midnight reads the chat again.

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
