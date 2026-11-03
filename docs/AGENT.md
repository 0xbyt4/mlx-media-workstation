# Agent mode

In an agent chat the local model can call tools. It can read the Workstation's state, queue media jobs, read files in
one folder, search the web, read web pages, and run Python and shell commands in a sandbox. The code is in `app/agent/`. The chat loop
that uses it is in `app/chat.py`. The endpoints and events are in `docs/API.md` under "Agent mode".

## Tools and risk levels

Each tool has a risk level. `app/agent/policy.py` decides from that level whether a call runs, asks or is refused.

| Level | Meaning | Rule |
|---|---|---|
| R0 | Reads app state only. | Runs. |
| R1 | Reads the user's files in the granted folder. | Runs when the chat has a granted folder. Refused without one. The grant is the consent. |
| R2 | Sends text off this Mac, or queues minutes of compute. | Asks on the first use in a chat. The user can answer "Allow in this chat", except for a tool whose result taints the chat (web_search, fetch_url): after its first result it asks every time, so a chat-wide grant would do nothing. When the chat is tainted, every R2 call asks, also with that grant. |
| R3 | Runs code, or has effects the app cannot check. | Asks every time. "Allow in this chat" is not offered. |

A tool that is off in the chat's Tools menu is refused. An unknown tool name is refused. The model gets the reason as
the tool's result, so it can answer the user.

| Tool | Level | What it does |
|---|---|---|
| `list_workstation_tools` | R0 | Lists the media tools, their fields and whether their models are installed. |
| `job_status` | R0 | Shows one job: status, progress, outputs, error. |
| `search_library` | R0 | Finds finished outputs by text, tool or file name. Its result is untrusted. |
| `create_job` | R2 | Queues a media job with text fields and returns the job id at once. A tool that needs an uploaded file is refused. |
| `list_files` | R1 | Lists a folder inside the granted folder (at most 500 lines). |
| `read_file` | R1 | Reads a text file inside the granted folder (at most 1 MB). |
| `search_files` | R1 | Searches the text files of the granted folder (at most 5,000 files and 15 s). |
| `web_search` | R2 | Searches the web. Sends the query off this Mac. |
| `fetch_url` | R2 | Reads one public web page as text, in parts of 10,000 characters. Sends the URL off this Mac. |
| `run_python` | R3 | Runs Python code in the sandbox. Present only when the sandbox works on this Mac. |
| `run_shell` | R3 | Runs a shell command (zsh) in the sandbox. Present only when the sandbox works on this Mac. |

The Workstation tools exist only in the full profile. The chat profile has no Workstation, so its registry has none
of them.

A new agent chat has every tool on except `run_python` and `run_shell`. The model writes that code and those
commands, and they can carry text from earlier untrusted content. So the user turns them on in the Tools menu when
the chat needs them. Both ask before every call.

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
- `/private/var` and `/private/tmp` (also as `/var` and `/tmp`): every user can write there;
- other users' folders under `/Users` (also `/Users/Shared`);
- the app's own folder (its settings, outputs and the other chats), and the folders that hold it;
- paths with a hidden part.

## Taint

Taint is a flag per chat. It is stored in `chat.json` with the grants.

- These tools set it when they run: `search_library`, `list_files`, `read_file`, `search_files`, `web_search`,
  `fetch_url`, `run_python` and `run_shell`. Their results can hold text from outside the app.
- Once set, it stays set for the rest of the chat. A new answer does not clear it, because the text is still in the
  model's context.
- While it is set, every R2 call asks, and "Allow in this chat" is not offered. A web page could tell the model to
  put private text into a query or a URL, or to queue jobs. The user sees such a call before it runs.

The prompt also tells the model that tool output is untrusted data and never instructions.

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
| Wall-clock time | 60 s | The host kills the process group. |
| CPU time | 60 s | `RLIMIT_CPU` |
| Memory | 2,048 MB | A watchdog polls the group's footprint every 0.05 s. macOS refuses memory rlimits. |
| One file | 50 MB | `RLIMIT_FSIZE` |
| Work folder | 500 MB, 10,000 entries | The watchdog checks every 0.5 s. |
| Output kept | 20,000 characters of stdout and of stderr each | The tool cuts the rest. |
| Code | 100,000 characters | The tool's schema. |

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
| Wall-clock time | `timeout_s`: 120 s by default, at most 600 s | The host kills every process of the run. |
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
one above it with `O_NOFOLLOW`, so a folder that was replaced by a link is never followed.

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
- The granted folder is not mounted in the `run_python` sandbox: it cannot read the user's files. `run_shell` can
  read the granted folder (not its hidden and secret files).
- In the granted folder, `run_shell` can list the names of hidden and secret files (`ls -a`), but not read them. The
  file tools do not show those names.
- `run_shell` can start every program in Homebrew's folders. Those folders often belong to the `admin` group, so
  another admin user could put a program there. The program still runs in the sandbox.
- When the app is killed hard (not closed) while `run_shell` runs, nothing stops the command's background
  processes. They stay in the sandbox: no network, writes to the work folder only.
- The process limit counts all of the user's processes (`RLIMIT_NPROC`). When the user starts many programs during
  a call, the call can start fewer processes.
- A local program can send any `Origin` header. The same-origin check stops other web pages. Programs on this Mac
  need the session cookie, which only the token file of this user gives (docs/API.md). A program that runs as this
  user can read that file.
- In `--browser` mode the browser sends the session cookie to every server on 127.0.0.1, at any port, that its pages
  open. Use the app window when other local servers run.

## How the chat loop uses the package

`app/chat.py` builds a registry with `build_registry(port, settings)` at the first use and keeps it. It keeps one
registry per media flag, so at most one per profile: with the media tools (the full profile; the port is the server's
`Workstation` object from `app/server.py`) and without them (the chat profile; the port is `None`). One answer
(`Turn`) runs these steps:

1. Take the schemas of the tools the chat offers. The R1 tools are offered only when a folder is granted.
2. Build the model input. The budget counts the system prompt, the tool schemas and the tool messages. Old tool
   results are shortened first, then old turns are dropped.
3. Run one model request with the schemas. With no calls, the answer ends.
4. For each call: check Stop and the time, check the format, check for a repeat, then call
   `decide(tool, grants, tainted, enabled)`.
5. On "ask", show the approval card and wait, at most 10 minutes. No answer counts as a denial. "Allow in this chat"
   adds the tool to the chat's grants. An answer that the server accepted (HTTP 200) counts, also when it came in the
   moment the wait timed out. The card shows `describe` and the arguments with bidi, zero-width, tag and control
   characters written as `\uXXXX`, and for `fetch_url` the host as the network sees it (IDNA, `xn--...`) on its own
   line.
6. Run the call with `registry.call(name, args, Context(chat_id, folder, abort, work_dir))`. Then update the taint
   flag with `taints(tool, tainted)`.
7. Add a tool message with the result and go to the next step. An answer has at most 8 steps and 10 minutes. The
   time a step waits for memory (a media job runs) does not count against the 10 minutes, and Stop ends that wait.

All steps run in one `llm.session()`. Its time limit counts from the moment it first holds the memory. `create_job`
never waits for its job, because the job waits for that session. When the app closes, every running answer gets
Stop (a waiting approval ends as stopped) and a few seconds to save what it has. `docs/API.md` lists all rules of the loop.

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
