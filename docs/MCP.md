# MCP server

Workstation has an MCP server. Local agents use it to reach the media side of the app: Claude Code, Claude Desktop and
other MCP clients on this Mac. An agent can read the Mac's memory and the job queue, list the media tools, queue jobs,
wait for them and get the output files.

The MCP tools are the tools of an Agent chat (`app/agent/ws_tools.py`). They have the same names, argument schemas,
checks and result texts. Only a few words and limits differ (see [Differences from an Agent chat](#differences-from-an-agent-chat)).

## How it works

```
MCP client  --stdio-->  start.py --mcp  --HTTP, 127.0.0.1-->  running app  -->  job queue
(Claude Code)           (app/mcp_server.py)                    (port 7870)
```

- The client starts the MCP server as a child process. They talk on stdin and stdout. The MCP server opens no port.
- The MCP server sends each tool call to the running app over HTTP on `127.0.0.1:7870`
  (`app/agent/http_port.py`). It sends nothing off this Mac. It uses no proxy and follows no redirect.
- The app must run. Start it with `Workstation.app`, or run `.venv/bin/python start.py --browser` in the app's folder.
- The MCP server signs in with the session token in `outputs/.server-token` of its own checkout. It reads the file
  only when it is a regular file of this user that only this user can read (`app/session_token.py`). It never prints
  or logs the token.
- The app writes a new token at each start. The MCP server reads the token again for each request, so a restarted
  app works without a new client setup.

## Set up Claude Code

In these steps, `/Users/you/mlx-media-workstation` is the app's folder. Use the absolute path of your checkout.

1. Start the app.
2. Add the server for your user (all projects):

   ```sh
   claude mcp add --transport stdio --scope user workstation -- \
     /Users/you/mlx-media-workstation/.venv/bin/python /Users/you/mlx-media-workstation/start.py --mcp
   ```

3. Start Claude Code and run `/mcp`. The `workstation` server shows its 9 tools.

To add the server for one project only, use `--scope local` (the default) or `--scope project`. To remove it, run
`claude mcp remove workstation`.

## Set up Claude Desktop

1. Open `~/Library/Application Support/Claude/claude_desktop_config.json`. Make the file when it does not exist.
2. Add the server to `mcpServers`:

   ```json
   {
     "mcpServers": {
       "workstation": {
         "command": "/Users/you/mlx-media-workstation/.venv/bin/python",
         "args": ["/Users/you/mlx-media-workstation/start.py", "--mcp"]
       }
     }
   }
   ```

3. Quit Claude Desktop and open it again.

## Other clients

Give the client this command for a stdio server:

```sh
/Users/you/mlx-media-workstation/.venv/bin/python /Users/you/mlx-media-workstation/start.py --mcp
```

Inside the app's folder, `.venv/bin/python -m app.mcp_server` starts the same server. Its `--port` option sets the
app's port (7870 by default). The app always uses 7870, so you need it only for tests.

## The tools

| Tool | Arguments | Hints | What it does |
|---|---|---|---|
| `system_status` | none | read-only | The Mac's memory, the running job with its progress, the queue, the chat model, and for each media tool whether it fits now. |
| `list_workstation_tools` | none | read-only | The media tools, their fields and whether their models are installed. |
| `create_job` | `tool`, `fields` | not read-only, not destructive | Queues a media job and returns its id at once. |
| `job_status` | `job_id` | read-only | One job: status, progress, error and output files. |
| `wait_for_job` | `job_id`, `seconds` (1 to 600, default 50) | read-only | Waits until the job ends or the time is over, then shows it as `job_status` does. |
| `list_jobs` | `limit` (1 to 30, default 10) | read-only | The queued and running jobs and the newest finished ones, one line each. |
| `cancel_job` | `job_id` | not read-only, destructive, idempotent | Cancels a queued or running job that an MCP client queued. |
| `search_library` | `query`, `limit` (1 to 30, default 10) | read-only | Finished outputs by prompt text, tool or file name (every word must match), newest first. |
| `transcribe_audio` | `audio` or `job_id`, `language` (default `auto`), `seconds` | not read-only, not destructive | Speech to text with Qwen3-ASR: queues a `voice.transcribe` job and waits for its text. |

Every tool has `openWorldHint: false`: no tool reaches past this Mac. The hints come from the tool's risk level in
`app/agent/policy.py` (`app/mcp_server.py`, `annotations`).

### Input files

A file field of `create_job` (`"type": "file"`) takes one file. A `files` field takes a list (at most 10). Give each
file in one of these forms:

| Form | The file |
|---|---|
| `/absolute/path/to/file.png` | A file on this Mac. `~/` is expanded. |
| `library:<job id>/<file name>` | An output of a finished job. |

- The file must be a regular file that you can read. A link, a folder, a device or a pipe is refused. A file over
  200 MB is refused before it is copied.
- A hidden or secret file is refused: any part of its real path that starts with `.`, and key, certificate and
  password files (the rules of the agent's file tools, `app/agent/file_tools.py`). For a file in the app's
  `outputs` folder, only the parts below that folder count.
- The app checks the file as it checks an upload: at most 200 MB, the count of the field, and a media type that the
  field accepts (`image/*` takes `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.heic` and `.heif`; `audio/*` takes
  `.wav`, `.mp3`, `.flac`, `.m4a`, `.aac`, `.ogg`, `.opus`, `.aiff` and `.aif`; `video/*` takes `.mp4` and `.mov`).
- The app copies the file into the job's folder. The job does not use your original file after that.

### Results

- A result is text, at most 12,000 characters, as in an Agent chat.
- An output shows as its absolute path and its app address, for example
  `file /Users/you/mlx-media-workstation/outputs/20261104-101500-ab12/image.png (in the app:
  http://127.0.0.1:7870/files/20261104-101500-ab12/image.png)`. Give the path to a file field of the next job.
- The app address opens only in a browser that is signed in to the app (`--browser` mode).
- For a finished job, `job_status` and `wait_for_job` also return small previews of its images: at most 4 JPEG images,
  at most 512 pixels on the long side (made with `sips`).
- A problem comes back as a result with `isError: true` and a text that says what to do.

## Memory and the queue

The app keeps all its rules for a job from MCP. The MCP server only sends the request.

- One job runs at a time. A job from MCP goes to the end of the queue, as a job from the Create screen does.
- A media tool fits when its peak memory plus 8 GB for macOS is at most the Mac's memory. `system_status` shows each
  tool as `fits now`, `fits after the chat model unloads` or `needs more than this Mac's memory`.
- Before a job starts, the app checks it next to the chat model. When the job does not fit, the job waits until no
  chat answer runs, then the app unloads the chat model. Two big models never share the memory.
- An Agent chat's answer holds the chat model during its steps. A job from MCP that needs that memory waits until the
  answer ends (at most 10 minutes).
- A job from MCP stores `"chat": "mcp"` in its `job.json`. The Jobs view shows it like any other job.
- `cancel_job` cancels only a job that an MCP client queued. Cancel other jobs in the Jobs view.
- The job tools work in both profiles. In the chat profile the window shows no Jobs view, but the queue runs.

## Differences from an Agent chat

| | Agent chat | MCP |
|---|---|---|
| Input files | `library:`, `work:` and `folder:` references | Absolute paths and `library:` references |
| Approval | The app asks you before `create_job`, `cancel_job` and `transcribe_audio` | Your client decides. Claude Code asks before a tool that is not allowed. |
| A wait | At most 300 s (120 s by default), inside the answer's 10 minutes | At most 600 s (50 s by default) |
| A job that needs the chat model's memory | Starts after the answer; the wait returns at once | The app unloads the chat model, then the job runs; the wait waits for it |
| `cancel_job` | Jobs that this chat queued | Jobs that an MCP client queued |

Some clients end a tool call after about 60 s. For this reason a wait takes 50 s when you give no `seconds`. A job
that runs longer goes on in the app: call `wait_for_job` again, or `job_status` later.

Text in the results of `search_library` and `transcribe_audio` comes from outside the app (prompts, speech). Your
agent must treat it as data, not as instructions.

## Troubleshooting

| Message | Do this |
|---|---|
| `Workstation is not running.` | Start the app, then call the tool again. The token file `outputs/.server-token` exists only while the app runs. When the app runs from another folder, start the MCP server from the app's folder. |
| `Workstation does not answer on http://127.0.0.1:7870.` | Start the app. When it runs, wait until its window shows, then try again. |
| `The app on http://127.0.0.1:7870 did not take the token` | Another program uses port 7870, or a token file was left in this folder while another copy of the app runs. Start the MCP server from the same folder as the running app. |
| `not a regular file`, `a link`, `hidden or secret files are not taken` | Give the path of the file itself, outside hidden folders. |
| `The job was refused: ...` | The app refused a value or a file. The text names the field. Call `list_workstation_tools` for the fields. |
| `Unknown tool` | Call `list_workstation_tools` for the tool ids. |

The client keeps the MCP server's messages (stderr) in its log. In Claude Code, `/mcp` shows the server's state.
