# Agent mode

In an agent chat the local model can call tools. It can read the Workstation's state, queue media jobs, read files in
one folder, search the web, read web pages and run Python in a sandbox. The code is in `app/agent/`. The chat loop
that uses it is in `app/chat.py`. The endpoints and events are in `docs/API.md` under "Agent mode".

## Tools and risk levels

Each tool has a risk level. `app/agent/policy.py` decides from that level whether a call runs, asks or is refused.

| Level | Meaning | Rule |
|---|---|---|
| R0 | Reads app state only. | Runs. |
| R1 | Reads the user's files in the granted folder. | Runs when the chat has a granted folder. Refused without one. The grant is the consent. |
| R2 | Sends text off this Mac, or queues minutes of compute. | Asks on the first use in a chat. The user can answer "Allow in this chat". When the chat is tainted, it asks every time, also with that grant. |
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
| `fetch_url` | R2 | Reads one public web page as text. Sends the URL off this Mac. |
| `run_python` | R3 | Runs Python code in the sandbox. Present only when the sandbox works on this Mac. |

The Workstation tools exist only in the full profile. The chat profile has no Workstation, so its registry has none
of them.

A new agent chat has every tool on except `run_python`. The model writes that code, and the code can carry text from
earlier untrusted content. So the user turns it on in the Tools menu when the chat needs it.

The file tools never list, search or read hidden names (any path part that starts with `.`) or secret files (keys,
certificates, keychains, password databases, `credentials`, `secrets.*` and similar). They resolve every path and
keep it inside the granted folder. They open a file with `O_NOFOLLOW` and check its real path again from the open
descriptor. They read only text files.

The folder grant is checked by the server (`PATCH /api/chats/{id}/agent`). It refuses `/`, the folders directly under
`/`, the home folder itself, system folders, `~/Library` and paths with a hidden part.

## Taint

Taint is a flag per chat. It is stored in `chat.json` with the grants.

- These tools set it when they run: `search_library`, `list_files`, `read_file`, `search_files`, `web_search`,
  `fetch_url` and `run_python`. Their results can hold text from outside the app.
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
- When that interpreter does not exist, or `sandbox-exec` is missing, the registry has no `run_python`.

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
- The code can read other processes' command lines (`KERN_PROCARGS2`). Do not put secrets in command lines.
- A DNS lookup and the HTML parsing of `fetch_url` cannot be interrupted. Stop takes effect after they end.
- The SearXNG URL is trusted. Its address is not checked, so it can be on this Mac or the local network.
- The granted folder is not mounted in the sandbox. `run_python` cannot read the user's files. Only the file tools
  can, and only as text for the model.
- A local program can send any `Origin` header. The same-origin check on approvals and grants stops other web pages,
  not programs on this Mac.

## How the chat loop uses the package

`app/chat.py` builds one registry with `build_registry(port, settings)` and keeps it. The port is the server's
`Workstation` object (`app/server.py`), or `None` in the chat profile. One answer (`Turn`) runs these steps:

1. Take the schemas of the tools the chat offers. The R1 tools are offered only when a folder is granted.
2. Build the model input. The budget counts the system prompt, the tool schemas and the tool messages. Old tool
   results are shortened first, then old turns are dropped.
3. Run one model request with the schemas. With no calls, the answer ends.
4. For each call: check Stop and the time, check the format, check for a repeat, then call
   `decide(tool, grants, tainted, enabled)`.
5. On "ask", show the approval card and wait, at most 10 minutes. No answer counts as a denial. "Allow in this chat"
   adds the tool to the chat's grants.
6. Run the call with `registry.call(name, args, Context(chat_id, folder, abort, work_dir))`. Then update the taint
   flag with `taints(tool, tainted)`.
7. Add a tool message with the result and go to the next step. An answer has at most 8 steps and 10 minutes.

All steps run in one `llm.session()`. `create_job` never waits for its job, because the job waits for that session.
`docs/API.md` lists all rules of the loop.

## Settings

The `[agent]` section of `config.toml`. The server reads it once at start, so a change needs a restart.

| Key | Default | Meaning |
|---|---|---|
| `python` | `""` | The interpreter for `run_python`. Empty: the app's base interpreter, standard library only. |
| `searxng_url` | `""` | The base URL of your SearXNG instance (JSON format on). Empty: not used. |
| `wikipedia_language` | `"en"` | The Wikipedia that `web_search` asks (`en`, `de`, `tr`...). |
| `marginalia` | `true` | Whether `web_search` also asks Marginalia Search. |

A bad value stops the server at start with the key and the reason.
