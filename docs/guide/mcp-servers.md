# MCP servers

An MCP server gives the agent more tools. For example, a server can read a git repository, search your notes or
talk to a web service. You add MCP servers in **Settings > MCP servers**. The agent of an Agent chat can then use
their tools.

There are two kinds of server:

| Kind | What it is | Network |
|---|---|---|
| Local program | A program on this Mac. The app starts it and talks to it on stdin and stdout. | Only when the program itself uses the network. |
| Remote server | A service at an `https://` URL. | Yes. Every call sends its arguments off this Mac. |

A local program runs with your user rights. It can read and change your files and use the network. Add only
programs that you trust.

## Add a local program

1. Open **Settings** and go to **MCP servers**.
2. Press **Add a server**.
3. Enter a **Name**: lowercase letters and digits, with `-` or `_` between them (for example `git`). The agent's
   tools of this server start with this name and two underscores: `git__log`.
4. Keep **Local program**.
5. Enter the **Program**. Use the absolute path, for example `/opt/homebrew/bin/uvx`.
6. Enter the **Arguments**, one a line, without quotes.
7. Optional: enter a **Working folder**. Empty means the app's folder.
8. Optional: add **Environment variables**, for example an API key. Each value goes to the macOS Keychain, never to
   a file.
9. Press **Add**. The server is off and not approved.

The server gets only its own environment variables and `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`. It
does not get the rest of the app's environment.

## Add a remote server

1. Press **Add a server** and choose **Remote server**.
2. Enter the **URL**. It must start with `https://`. Plain `http://` works only for a server on this Mac
   (`127.0.0.1`).
3. Optional: enter a **Bearer token**. It goes to the macOS Keychain and in the `Authorization` header of each
   request.
4. Press **Add**.

A remote server shows the label **Remote: sends data off this Mac**. It needs the network. The app does not do the
OAuth sign-in of a remote server: use a server that takes a bearer token, or none.

## Approve the command

Before the app runs a server for the first time, you approve it.

1. Read the whole **Command** line (or the **URL**). The card shows the program with its full path.
2. Read the yellow notes. The card warns about `sudo`, delete options, and programs that download a package (`npx`,
   `uvx`, `pipx`, `bunx`, `pnpx`, `docker`, `podman`).
3. Press **Approve this command** (or **Approve this URL**).

When you change the program, an argument, the working folder, the names of the environment variables or the URL,
the approval stops. Approve the new command again. A new value of a secret does not need a new approval.

## Test the connection and approve the tools

1. Press **Test connection**. The app starts the server and lists its tools.
2. Read each tool: its name, its description and the hints from the server (read-only, destructive). The hints come
   from the server, so the app does not trust them.
3. Press **Approve** for each tool that the agent may use, or **Approve all**.
4. Turn on the switch of the server. New agent chats then get its approved tools.

The app keeps a fingerprint (SHA-256) of each approved tool. When the server changes a tool later, the tool shows
**Changed** and the agent cannot use it. The card shows the description that you approved. Read the new one, then
press **Approve** again, or **Revoke**.

A tool that the server no longer offers shows **Not offered now**.

## Choose how the agent sees the tools

| Choice | What it does |
|---|---|
| Deferred (default) | The tools are not in the agent's tool list. The agent finds them with tool search. The prompt stays small. |
| Direct | The tools are in the tool list of every agent chat. At most 4 MCP tools of all servers are direct; the others stay deferred. |
| Hidden | The agent does not get the tools. |

Use **Direct** only for a tool that you use in most chats.

## When the agent calls an MCP tool

- The app starts the server at the first turn of an agent chat. A server that does not start in 30 seconds fails.
  Only its own tools fail; the chat goes on.
- The app asks you before every call of an MCP tool. The approval card shows the tool, its arguments and the server.
- The result is untrusted: after it, the chat counts as tainted. Every later call that sends data or has a cost asks
  again (see [Agent mode](agent-mode.md#untrusted-content)).
- A call may take at most 120 seconds.
- When the server gives an error, the agent reads the error text and can tell you.

## Read the last error

When a server fails, its card shows **Failed** and a **Last error** box. The box shows the end of the server's
output (stderr). A secret value of the server shows as `[secret]`. The full output is in `outputs/mcp/<name>.log`.

After a failure, a call within 30 seconds fails at once with the same error. Press **Test connection** to try again
at once.

## Turn off or remove a server

- Turn off the switch: the app stops the server. Chats do not get its tools.
- Press **Remove**: the app stops the server, forgets its approvals and deletes its secrets from the Keychain.

## Where the app keeps what

| What | Where |
|---|---|
| The list of servers, the approvals and the approved tool definitions | `outputs/mcp_servers.json` (only you can read it) |
| Environment values and bearer tokens | The macOS Keychain, service `mlx-media-workstation-mcp`, account = the server's name |
| The output (stderr) of a local server | `outputs/mcp/<name>.log`, made new at each start |
| The process groups of the running servers | `outputs/mcp/pids.json` |

When you close the app, it stops every server. When the app stopped without closing them (a crash), it stops them at
the next start.
