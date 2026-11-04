"""The MCP client: tools from MCP servers that the user adds in Settings > MCP servers (docs/MCP.md, "MCP client").

A server is a program on this Mac that the app starts and talks to on stdin and stdout ("stdio"), or a service at an
https URL ("remote": it sends data off this Mac, so it is labelled and off until the user turns it on).

- The list of servers is in outputs/mcp_servers.json (0600). It holds no secret: the values of a server's environment
  variables and a remote server's bearer token are in the macOS Keychain (service KEYCHAIN_SERVICE, account = the
  server's name), written and read with /usr/bin/security. No secret goes to a log or an API answer.
- Before the first start of a stdio server, the user approves its exact command line (program, arguments, working
  folder, names of the environment variables). The approval is a hash of that launch spec (approval()), so a changed
  command needs a new approval. A remote server's URL is approved the same way.
- The user approves each tool too: the app stores the SHA-256 of the tool's definition (definition_hash) with the
  definition. When the server lists a different definition later, the tool is off until the user approves it again
  (a "rug pull" guard). The agent sees only the approved definitions, never the live ones.
- One asyncio loop in its own thread holds every connection (Manager). A server starts when a chat needs it, with a
  time limit (START_TIMEOUT). A server that fails, hangs or exits fails only its own tools, with an error text.
- Each stdio server runs in its own process group. Stopping it closes its stdin, then sends SIGTERM and SIGKILL to the
  group. outputs/mcp/pids.json records the running groups, so a group left by a crash is stopped at the next start
  (reap_leftovers). The server's stderr goes to outputs/mcp/<name>.log.
"""

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import anyio.to_thread
import httpx2
import mcp_types as types
from anyio.streams.text import TextReceiveStream
from mcp import Client
from mcp.client.stdio import get_default_environment
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from app.agent.registry import HIDDEN

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "outputs" / "mcp_servers.json"
LOGS = ROOT / "outputs" / "mcp"  # <name>.log (a stdio server's stderr) and pids.json
KEYCHAIN_SERVICE = "mlx-media-workstation-mcp"
SECURITY = "/usr/bin/security"

START_TIMEOUT = 30.0  # seconds from the start of the program to its first tool list
CALL_TIMEOUT = 120.0  # seconds of one tool call
RETRY_AFTER = (
    30.0  # seconds after a failed start before a call tries to start the server again
)
STOP_WAIT = 2.0  # seconds a stopping server gets after its stdin closes, and again after SIGTERM
MAX_SERVERS = 20
MAX_TOOLS = 100  # tools of one server that the app keeps
MAX_LINE = 8 * 1024 * 1024  # characters of one message from a stdio server
MAX_RAW = 100_000  # characters of a tool result before the registry's cap (app/agent/registry.py MAX_RESULT)
TAIL = 1200  # characters of the server's stderr in its last error
KINDS = ("stdio", "remote")
EXPOSURES = ("deferred", "direct", "hidden")
# a server's name: the prefix of its tools and the Keychain account. No "__", so "<server>__<tool>" splits one way.
NAME = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
MAX_NAME = 24
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
TOKEN = "token"  # the Keychain key of a remote server's bearer token
MAX_ARGS = 64
MAX_TEXT = 4096  # characters of a command, an argument, a folder or a URL
MAX_SECRET = 8192
# programs that download and run a package, or run with more rights: the Settings card says so before the approval
FETCHERS = ("npx", "uvx", "pipx", "bunx", "pnpx", "docker", "podman")
ELEVATE = ("sudo", "su", "doas")


class ConfigError(ValueError):
    """A server entry that cannot be saved: `fields` names the field and says why."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


class StartError(Exception):
    """The server is not available (not approved, not started in time, or it exited)."""


# ---------- the config file ----------

lock = threading.Lock()  # the config file and the Keychain


def load() -> dict[str, Any]:
    """The config: {"servers": [entry, ...]}. A missing or unreadable file reads as no servers."""
    try:
        data = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {"servers": []}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        return {"servers": []}
    return {
        "servers": [
            s for s in servers if isinstance(s, dict) and valid_name(s.get("name"))
        ]
    }


def save(data: dict[str, Any]) -> None:
    """Write the config atomically, readable by this user only."""
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=CONFIG.parent, prefix=CONFIG.name + ".", suffix=".tmp"
    )  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, CONFIG)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def find(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((s for s in data["servers"] if s.get("name") == name), None)


def server(name: str) -> dict[str, Any] | None:
    return find(load(), name)


def valid_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and len(name) <= MAX_NAME
        and NAME.fullmatch(name) is not None
    )


# the approval card shows the command line as text: a control, invisible or direction character (a line break, a
# zero-width space, U+202E) could make it read as another command than the one the hash approves
HIDDEN_TEXT = (
    "Must not contain line breaks, control characters or invisible characters (such as zero-width or "
    "right-to-left marks)."
)


def _text(
    value: Any, field_name: str, errors: dict[str, str], *, required: bool = False
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[field_name] = "Must be text."
        return ""
    value = value.strip()
    if required and not value:
        errors[field_name] = "Required."
    elif len(value) > MAX_TEXT:
        errors[field_name] = f"At most {MAX_TEXT} characters."
    elif HIDDEN.search(value):
        errors[field_name] = HIDDEN_TEXT
    return value


def check_url(url: str) -> str | None:
    """Why a remote server's URL is refused, or None: https only (http only to this Mac), no user name or password
    in it."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return "Not a valid URL."
    if parts.scheme not in ("https", "http") or not host:
        return "Use an https:// URL."
    if parts.scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        return (
            "Use https://. Plain http:// is only for a server on this Mac (127.0.0.1)."
        )
    if parts.username is not None or parts.password is not None:
        return (
            "Do not put a user name or password in the URL. Use the bearer token field."
        )
    if any(c.isspace() for c in url):
        return "The URL must not contain spaces."
    return None


def checked(raw: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    """A server entry from the Settings form, checked. `old` is the saved entry when this is an edit: the name and
    the kind stay, the approvals and tool approvals carry over (the approval hash decides if they still count)."""
    errors: dict[str, str] = {}
    name = old["name"] if old else raw.get("name")
    if not valid_name(name):
        errors["name"] = (
            f"Use 1 to {MAX_NAME} lowercase letters and digits, with single - or _ between them (for example git or "
            "my-notes)."
        )
    kind = old["kind"] if old else raw.get("kind")
    if kind not in KINDS:
        errors["kind"] = "Choose a local program or a remote server."
    exposure = raw.get("exposure", (old or {}).get("exposure", "deferred"))
    if exposure not in EXPOSURES:
        errors["exposure"] = "Choose deferred, direct or hidden."
    entry: dict[str, Any] = {"name": name, "kind": kind, "exposure": exposure}
    if kind == "stdio":
        entry["command"] = _text(raw.get("command"), "command", errors, required=True)
        args = raw.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors["args"] = "Must be a list of texts."
            args = []
        elif len(args) > MAX_ARGS:
            errors["args"] = f"At most {MAX_ARGS} arguments."
        elif any(len(a) > MAX_TEXT for a in args):
            errors["args"] = f"Each argument: at most {MAX_TEXT} characters."
        elif any(HIDDEN.search(a) for a in args):
            errors["args"] = HIDDEN_TEXT
        entry["args"] = list(args)
        cwd = _text(raw.get("cwd"), "cwd", errors)
        if cwd and not os.path.isabs(os.path.expanduser(cwd)):
            errors["cwd"] = "Use an absolute path, or leave it empty."
        entry["cwd"] = cwd
        env = raw.get("env") or []
        if not isinstance(env, list) or not all(
            isinstance(e, str) and ENV_NAME.fullmatch(e) for e in env
        ):
            errors["env"] = (
                "Each name: letters, digits and _, not starting with a digit (for example API_KEY)."
            )
            env = []
        entry["env"] = sorted(set(env))
    elif kind == "remote":
        url = _text(raw.get("url"), "url", errors, required=True)
        if url and "url" not in errors:
            problem = check_url(url)
            if problem:
                errors["url"] = problem
        entry["url"] = url
    if errors:
        raise ConfigError(errors)
    entry["enabled"] = bool((old or {}).get("enabled", False))
    entry["approved"] = (old or {}).get("approved")
    entry["tools"] = (old or {}).get("tools") or {}
    entry["secrets"] = (old or {}).get("secrets") or []
    return entry


def secret_names(entry: dict[str, Any]) -> list[str]:
    """The secrets this server may have: its environment variables, or the bearer token of a remote server."""
    return list(entry.get("env", [])) if entry["kind"] == "stdio" else [TOKEN]


# ---------- the Keychain ----------


class Keychain:
    """One generic password per server in the login keychain: a JSON object {secret name: value}, ASCII only. The
    values go to /usr/bin/security as arguments (no shell); they never go to a file or a log."""

    def run(
        self, args: list[str], timeout: float = 10.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SECURITY, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def get(self, name: str) -> dict[str, str]:
        r = self.run(
            ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"]
        )
        if r.returncode != 0:
            return {}  # 44: no such item
        try:
            data = json.loads(r.stdout.strip())
        except ValueError:
            return {}
        return (
            {k: v for k, v in data.items() if isinstance(v, str)}
            if isinstance(data, dict)
            else {}
        )

    def set(self, name: str, values: dict[str, str]) -> None:
        if not values:
            self.delete(name)
            return
        blob = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
        r = self.run(
            ["add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-l",
             f"MLX Media Workstation MCP: {name}", "-w", blob]
        )  # fmt: skip
        if r.returncode != 0:
            # stderr names the item, never the value
            raise OSError(
                f"The Keychain refused the secret (security exit {r.returncode})."
            )

    def delete(self, name: str) -> None:
        self.run(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name])


keychain = Keychain()


def update_secrets(entry: dict[str, Any], given: dict[str, str | None]) -> None:
    """Store the secrets the form sent: a text sets the value, None or "" removes it, a name not sent keeps its
    value. A value for a name the server no longer has is removed. entry["secrets"] lists the names that have a value
    (never the values)."""
    allowed = secret_names(entry)
    for key, value in given.items():
        if key not in allowed:
            raise ConfigError(
                {f"secrets.{key}": "Not an environment variable of this server."}
            )
        if value is not None and (
            len(value) > MAX_SECRET or not value.isascii() or "\x00" in value
        ):
            raise ConfigError(
                {f"secrets.{key}": f"Use at most {MAX_SECRET} ASCII characters."}
            )
    stale = set(entry.get("secrets", [])) - set(allowed)
    if not given and not stale:
        return
    values = keychain.get(entry["name"]) if entry.get("secrets") else {}
    for key, value in given.items():
        if value:
            values[key] = value
        else:
            values.pop(key, None)
    values = {k: v for k, v in values.items() if k in allowed}
    keychain.set(entry["name"], values)
    entry["secrets"] = sorted(values)


# ---------- the launch spec and its approval ----------


def base_env() -> dict[str, str]:
    """The environment a stdio server gets before its own variables: HOME, LOGNAME, PATH, SHELL, TERM and USER of the
    app (the MCP SDK's safe list), nothing else of the app's environment."""
    return get_default_environment()


def resolve(command: str) -> str | None:
    """The program's absolute path: a path as given (with ~ expanded), a bare name looked up in PATH."""
    expanded = os.path.expanduser(command)
    if os.sep in expanded:
        return expanded if os.path.isabs(expanded) else None
    return shutil.which(expanded, path=base_env().get("PATH"))


def argv(entry: dict[str, Any]) -> list[str]:
    """The exact command line a stdio server runs with (the program resolved)."""
    return [resolve(entry["command"]) or entry["command"], *entry["args"]]


def command_line(entry: dict[str, Any]) -> str:
    """The command line as the approval card shows it (shell quoting, so each argument is clear)."""
    return shlex.join(argv(entry))


def approval(entry: dict[str, Any]) -> str:
    """The SHA-256 of what the user approves: the resolved command line, the working folder and the names of the
    environment variables (stdio), or the URL (remote). Secret values are not part of it."""
    if entry["kind"] == "stdio":
        spec: list[Any] = [
            "stdio",
            argv(entry),
            os.path.expanduser(entry["cwd"]),
            entry["env"],
        ]
    else:
        spec = ["remote", entry["url"]]
    return hashlib.sha256(json.dumps(spec, ensure_ascii=False).encode()).hexdigest()


def approved(entry: dict[str, Any]) -> bool:
    """Whether the user approved this exact launch. A program that is not found never counts: its bare name would
    run whatever program of that name appears in PATH later, under the old approval."""
    if entry["kind"] == "stdio" and resolve(entry["command"]) is None:
        return False
    return entry.get("approved") == approval(entry)


def warnings(entry: dict[str, Any]) -> list[str]:
    """What the approval card points out before the user approves a command."""
    if entry["kind"] == "remote":
        return ["It sends the agent's tool calls and their arguments to " + (urlsplit(entry["url"]).hostname or "")
                + ", off this Mac."]  # fmt: skip
    out = []
    program = os.path.basename(entry["command"])
    path = resolve(entry["command"])
    if path is None:
        out.append(
            f"The program {entry['command']} was not found. Use its absolute path."
        )
    if program in ELEVATE:
        out.append(
            f"{program} runs the server with more rights than yours. Do not approve this."
        )
    if program in FETCHERS:
        out.append(
            f"{program} can download a package from the internet and run it. The first start needs the network."
        )
    if program == "rm" or any(a in ("-rf", "-fr", "--force") for a in entry["args"]):
        out.append("The command line has a delete or force option. Read it again.")
    return out


# ---------- tool definitions ----------


def definition(tool: types.Tool) -> dict[str, Any]:
    """The parts of a listed tool that the model sees or that say what it does, as plain JSON."""
    raw = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
    keep = (
        "name",
        "title",
        "description",
        "inputSchema",
        "outputSchema",
        "annotations",
    )
    return {k: raw[k] for k in keep if k in raw}


def definition_hash(d: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            d, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def tool_state(
    entry: dict[str, Any], name: str, live: dict[str, dict[str, Any]] | None
) -> str:
    """ "approved", "new" (never approved), "changed" (the server lists another definition now) or "gone" (approved,
    but the server no longer lists it). With no live list (the server has not run since the app started), an
    approved tool counts as approved."""
    saved = (entry.get("tools") or {}).get(name)
    if live is None:
        return "approved" if saved else "new"
    if name not in live:
        return "gone"
    if not saved:
        return "new"
    return "approved" if saved["hash"] == definition_hash(live[name]) else "changed"


# ---------- the stdio transport ----------


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _end_group(proc: Any) -> None:
    """Wait for the server to exit after its stdin closed; then SIGTERM, then SIGKILL its whole process group. Returns
    when the leader exited and the group is gone (or after the last wait)."""
    pgid = proc.pid
    for sig in (None, signal.SIGTERM, signal.SIGKILL):
        if sig is not None:
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        end = time.monotonic() + STOP_WAIT
        while time.monotonic() < end:
            if proc.returncode is not None and not group_alive(pgid):
                return
            await anyio.sleep(0.05)


@asynccontextmanager
async def stdio_transport(
    command: list[str],
    env: dict[str, str],
    cwd: str | None,
    errlog: Any,
    started: Callable[[int], None],
    exited: Callable[[], None],
) -> AsyncIterator[tuple[Any, Any]]:
    """The MCP SDK's stdio transport, with the process group in our hands: started(pid) gets the group id (the
    server is its leader), exited() runs when the server closes its stdout. Leaving the context stops the group."""
    proc = await anyio.open_process(
        command, env=env, cwd=cwd, stderr=errlog, start_new_session=True
    )
    started(proc.pid)
    read_w, read_r = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_w, write_r = anyio.create_memory_object_stream[SessionMessage](0)

    async def reader() -> None:
        buf = ""
        try:
            async with read_w:
                async for chunk in TextReceiveStream(proc.stdout, errors="replace"):
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    if len(buf) > MAX_LINE:
                        raise ValueError(
                            f"a message from the server is over {MAX_LINE:,} characters"
                        )
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            msg: SessionMessage | Exception = SessionMessage(
                                types.jsonrpc_message_adapter.validate_json(
                                    line, by_name=False
                                )
                            )
                        except ValueError as e:
                            msg = e
                        await read_w.send(msg)
        except (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            anyio.EndOfStream,
            OSError,
        ):
            pass
        finally:
            exited()

    async def writer() -> None:
        try:
            async with write_r:
                async for message in write_r:
                    data = (
                        message.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        )
                        + "\n"
                    )
                    await proc.stdin.send(data.encode())
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_w.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(reader)
        tg.start_soon(writer)
        try:
            yield read_r, write_w
        finally:
            with anyio.CancelScope(shield=True):
                await write_w.aclose()
                with contextlib.suppress(
                    OSError, anyio.ClosedResourceError, anyio.BrokenResourceError
                ):
                    await proc.stdin.aclose()
                await _end_group(proc)
                with contextlib.suppress(
                    OSError, anyio.ClosedResourceError, anyio.BrokenResourceError
                ):
                    await proc.stdout.aclose()
                transport = getattr(getattr(proc, "_process", None), "_transport", None)
                if transport is not None:
                    with contextlib.suppress(Exception):
                        transport.close()
            tg.cancel_scope.cancel()


# ---------- process groups left by a crash ----------

pids_lock = threading.Lock()


def _pids() -> dict[str, Any]:
    try:
        data = json.loads((LOGS / "pids.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_pids(data: dict[str, Any]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / "pids.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def record_pid(name: str, pid: int | None, command: list[str] | None = None) -> None:
    with pids_lock:
        data = _pids()
        if pid is None:
            data.pop(name, None)
        else:
            data[name] = {"pid": pid, "command": command, "started": started(pid)}
        _write_pids(data)


def started(pid: int) -> str | None:
    """When the process started, as ps shows it: with the command line it tells a leftover server from a process
    that got its id later."""
    r = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def leader_command(pid: int) -> str | None:
    r = subprocess.run(
        ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def same_command(shown: str | None, command: list[str]) -> bool:
    """Whether ps shows this command line. A launcher can show another program path (a framework Python runs
    Python.app), so with arguments the arguments must match exactly at the end."""
    if not shown or not command:
        return False
    if shown == " ".join(command):
        return True
    return len(command) > 1 and shown.endswith(" " + " ".join(command[1:]))


def reap_leftovers() -> list[str]:
    """Stop the process groups of servers that a crashed run left: only a group whose leader still runs the recorded
    command and started at the recorded time (a reused process id is left alone). Returns the names of the servers
    it stopped."""
    stopped = []
    with pids_lock:
        data = _pids()
        for name, item in data.items():
            pid = item.get("pid") if isinstance(item, dict) else None
            command = item.get("command") if isinstance(item, dict) else None
            if not isinstance(pid, int) or pid <= 1 or not isinstance(command, list):
                continue
            when = item.get("started")
            if (
                isinstance(when, str)
                and group_alive(pid)
                and started(pid) == when
                and same_command(leader_command(pid), command)
            ):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, sig)
                    end = time.monotonic() + STOP_WAIT
                    while time.monotonic() < end and group_alive(pid):
                        time.sleep(0.05)
                stopped.append(name)
        if data:
            _write_pids({})
    return stopped


# ---------- connections ----------


@dataclass
class Conn:
    """One server's connection, owned by the manager's loop."""

    name: str
    state: str = "stopped"  # stopped, starting, ready, failed
    error: str | None = None
    live: dict[str, dict[str, Any]] | None = (
        None  # name -> definition, from the last tool list
    )
    client: Client | None = None
    spec: str | None = None  # the approval hash it started with
    task: asyncio.Task[None] | None = None
    ready: asyncio.Event | None = None
    stop: asyncio.Event | None = None
    scope: anyio.CancelScope | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failed_at: float = 0.0
    secrets: list[str] = field(default_factory=list)  # values to hide in the error text
    log: Path | None = None
    stopping: bool = False


def first_error(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def describe(e: BaseException) -> str:
    e = first_error(e)
    if isinstance(e, FileNotFoundError):
        return f"The program was not found: {e.filename or e}."
    if isinstance(e, PermissionError):
        return f"The program cannot run: {e}."
    if isinstance(e, MCPError):
        return f"The server answered with an error: {e}."
    if isinstance(e, httpx2.HTTPError):
        return f"The connection failed: {type(e).__name__}: {e}."
    text = str(e) or type(e).__name__
    return f"{type(e).__name__}: {text}" if not isinstance(e, StartError) else text


INFO = types.Implementation(name="mlx-media-workstation", version="1")


class Manager:
    """Every MCP connection of the app, on one asyncio loop in a daemon thread. The methods without a leading _ are
    for other threads (the chat loop, the API) and wait for the loop."""

    def __init__(self) -> None:
        self.conns: dict[str, Conn] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.guard = threading.Lock()

    # --- the loop thread ---

    def _loop(self) -> asyncio.AbstractEventLoop:
        with self.guard:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                ready = threading.Event()

                def run(loop: asyncio.AbstractEventLoop) -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                self.thread = threading.Thread(
                    target=run, args=(self.loop,), name="mcp-client", daemon=True
                )
                self.thread.start()
                ready.wait(5)
            return self.loop

    def submit(self, coro: Any) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop())

    def conn(self, name: str) -> Conn:
        with self.guard:
            c = self.conns.get(name)
            if c is None:
                c = self.conns[name] = Conn(name)
            return c

    def peek(self, name: str) -> Conn | None:
        with self.guard:
            return self.conns.get(name)

    def live(self, name: str) -> dict[str, dict[str, Any]] | None:
        c = self.peek(name)
        return c.live if c else None

    # --- start and stop (in the loop) ---

    async def _ensure(self, entry: dict[str, Any], force: bool = False) -> Conn:
        conn = self.conn(entry["name"])
        spec = approval(entry)
        async with conn.lock:
            running = (
                conn.task is not None
                and not conn.task.done()
                and conn.client is not None
            )
            if running and conn.spec == spec and not force:
                return conn
            if conn.task is not None and not conn.task.done():
                await self._stop(conn)
            recent = time.monotonic() - conn.failed_at < RETRY_AFTER
            if not force and conn.state == "failed" and conn.spec == spec and recent:
                raise StartError(conn.error or "It failed to start.")
            if not approved(entry):
                raise StartError(
                    "Its command is not approved. Approve it in Settings > MCP servers."
                )
            conn.spec, conn.state, conn.error, conn.stopping = (
                spec,
                "starting",
                None,
                False,
            )
            conn.ready, conn.stop = asyncio.Event(), asyncio.Event()
            conn.task = asyncio.create_task(self._run(conn, entry))
            try:
                await asyncio.wait_for(asyncio.shield(conn.ready.wait()), START_TIMEOUT)
            except TimeoutError:
                await self._stop(conn)
                self._failed(
                    conn, f"It did not start within {START_TIMEOUT:g} seconds."
                )
            if conn.client is None:
                if conn.state != "failed":
                    self._failed(conn, conn.error or "It stopped while it started.")
                raise StartError(conn.error or "It failed to start.")
            return conn

    def _failed(self, conn: Conn, error: str) -> None:
        conn.state, conn.failed_at = "failed", time.monotonic()
        conn.error = self._redact(conn, error + self._tail(conn))

    def _tail(self, conn: Conn) -> str:
        if conn.log is None:
            return ""
        try:
            with open(conn.log, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - TAIL))
                text = f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""
        return f"\nThe server's last output (stderr):\n{text}" if text else ""

    def redact(self, name: str, text: str) -> str:
        """The text with the server's secret values (its env values or token) written as [secret]: for its results
        and errors, which go to the model and into chat.json."""
        conn = self.peek(name)
        return self._redact(conn, text) if conn is not None else text

    def _redact(self, conn: Conn, text: str) -> str:
        for value in conn.secrets:
            if len(value) >= 4:
                text = text.replace(value, "[secret]")
        return text

    async def _transport(
        self, stack: AsyncExitStack, conn: Conn, entry: dict[str, Any]
    ) -> Any:
        values = (
            await anyio.to_thread.run_sync(keychain.get, entry["name"])
            if entry.get("secrets")
            else {}
        )
        conn.secrets = list(values.values())
        if entry["kind"] == "remote":
            headers = (
                {"Authorization": f"Bearer {values[TOKEN]}"}
                if values.get(TOKEN)
                else {}
            )
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(30.0, read=300.0),
                    trust_env=False,
                )
            )
            conn.log = None
            return streamable_http_client(entry["url"], http_client=http)
        command = argv(entry)
        cwd = os.path.expanduser(entry["cwd"]) or None
        if cwd is not None and not os.path.isdir(cwd):
            raise StartError(f"The working folder {cwd} does not exist.")
        env = base_env() | {k: v for k, v in values.items() if k in entry["env"]}
        LOGS.mkdir(parents=True, exist_ok=True)
        conn.log = LOGS / f"{entry['name']}.log"
        errlog = stack.enter_context(open(conn.log, "wb"))  # noqa: ASYNC230, SIM115 - a local file, closed by the stack

        def started(pid: int) -> None:
            record_pid(entry["name"], pid, command)

        def exited() -> None:
            if conn.stop is not None and not conn.stopping:
                conn.error = "The server exited."
                conn.stop.set()

        stack.callback(record_pid, entry["name"], None)
        return stdio_transport(command, env, cwd, errlog, started, exited)

    async def _run(self, conn: Conn, entry: dict[str, Any]) -> None:
        assert conn.ready is not None and conn.stop is not None
        error: BaseException | None = None
        try:
            with anyio.CancelScope() as scope:
                conn.scope = scope
                async with AsyncExitStack() as stack:
                    transport = await self._transport(stack, conn, entry)
                    client = Client(
                        transport,
                        read_timeout_seconds=CALL_TIMEOUT,
                        client_info=INFO,
                        cache=None,
                    )
                    await stack.enter_async_context(client)
                    conn.live = await self._list(client)
                    conn.client, conn.state = client, "ready"
                    conn.ready.set()
                    await conn.stop.wait()
                    conn.client = None
        except Exception as e:  # noqa: BLE001 - recorded as the server's error, never raised into a chat
            error = e
        finally:
            conn.client = None
            if (error is not None or conn.error) and not conn.stopping:
                # "The server exited." (from the transport) says more than the session's "Connection closed"
                self._failed(conn, conn.error or describe(error))
            elif conn.state != "failed":
                conn.state = "stopped"
            conn.ready.set()

    async def _stop(self, conn: Conn) -> None:
        conn.stopping = True
        if conn.stop is not None:
            conn.stop.set()
        if conn.client is None and conn.scope is not None:
            conn.scope.cancel()  # still starting
        if conn.task is not None:
            # the transport's own stop takes at most 3 waits of STOP_WAIT
            done, _ = await asyncio.wait({conn.task}, timeout=STOP_WAIT * 3 + 2)
            if not done:
                conn.task.cancel()
        conn.client = None
        if conn.state != "failed":
            conn.state = "stopped"

    async def _list(self, client: Client) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        cursor = None
        for _ in range(20):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for t in page.tools:
                if len(found) < MAX_TOOLS and t.name not in found:
                    found[t.name] = definition(t)
            cursor = page.next_cursor
            if not cursor:
                break
        return found

    async def _call(
        self, entry: dict[str, Any], tool: str, args: dict[str, Any], timeout: float
    ) -> Any:
        conn = await self._ensure(entry)
        client = conn.client
        if client is None:
            raise StartError(conn.error or "It is not running.")
        # the guard: the live definition must still be the approved one at the time of the call
        conn.live = await asyncio.wait_for(self._list(client), timeout)
        saved = (entry.get("tools") or {}).get(tool)
        state = tool_state(entry, tool, conn.live)
        if saved is None or state != "approved":
            raise ChangedError(state)
        return await client.call_tool(tool, args, read_timeout_seconds=timeout)

    # --- for other threads ---

    def wait(
        self,
        fut: concurrent.futures.Future[Any],
        timeout: float,
        abort: threading.Event | None = None,
    ) -> Any:
        """The future's result; raises TimeoutError after `timeout` seconds, Aborted when `abort` is set (the
        future is cancelled in both cases)."""
        end = time.monotonic() + timeout
        while True:
            try:
                return fut.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                if abort is not None and abort.is_set():
                    fut.cancel()
                    raise Aborted from None
                if time.monotonic() >= end:
                    fut.cancel()
                    raise TimeoutError from None

    def test(self, entry: dict[str, Any]) -> Conn:
        """Start (or restart) the server and list its tools, for Settings > Test connection. Errors stay on the
        connection (conn.error)."""
        try:
            self.wait(
                self.submit(self._ensure(entry, force=True)),
                START_TIMEOUT + STOP_WAIT * 3 + 5,
            )
        except (StartError, TimeoutError):
            pass
        return self.conn(entry["name"])

    def warm(self, entry: dict[str, Any]) -> None:
        """Start an enabled server in the background, so its first call does not wait for the start."""
        c = self.peek(entry["name"])
        if (
            c is not None
            and c.state in ("ready", "starting")
            and c.spec == approval(entry)
        ):
            return
        fut = self.submit(self._ensure(entry))
        fut.add_done_callback(
            lambda f: f.cancelled() or f.exception()
        )  # the error stays on the connection

    def call(
        self,
        entry: dict[str, Any],
        tool: str,
        args: dict[str, Any],
        abort: threading.Event | None,
        timeout: float = CALL_TIMEOUT,
    ) -> types.CallToolResult:
        return self.wait(
            self.submit(self._call(entry, tool, args, timeout)),
            START_TIMEOUT + timeout + 5,
            abort,
        )

    def stop(self, name: str) -> None:
        c = self.peek(name)
        if c is None or self.loop is None:
            return
        with contextlib.suppress(TimeoutError, concurrent.futures.CancelledError):
            self.wait(self.submit(self._stop(c)), STOP_WAIT * 3 + 5)
        c.state, c.error = "stopped", None

    def forget(self, name: str) -> None:
        self.stop(name)
        with self.guard:
            self.conns.pop(name, None)

    def stop_all(self) -> None:
        """Stop every server (app shutdown): each gets its stdin closed, then SIGTERM and SIGKILL to its group."""
        if self.loop is None:
            return

        async def all_of() -> None:
            await asyncio.gather(
                *(self._stop(c) for c in list(self.conns.values())),
                return_exceptions=True,
            )

        with contextlib.suppress(TimeoutError, concurrent.futures.CancelledError):
            self.wait(self.submit(all_of()), STOP_WAIT * 3 + 5)
        # a group whose stop did not finish (a stuck loop): stop it from here
        reap_leftovers()


class ChangedError(Exception):
    """The tool is not approved now: its state ("new", "changed", "gone")."""


class Aborted(Exception):
    """The user pressed Stop while the call waited."""


_manager: Manager | None = None
_manager_lock = threading.Lock()


def manager() -> Manager:
    """The app's one Manager. The first use stops the server groups that a crashed run left."""
    global _manager
    with _manager_lock:
        if _manager is None:
            reap_leftovers()
            _manager = Manager()
        return _manager


def stop_all() -> None:
    """At app shutdown (app/server.py shutdown)."""
    if _manager is not None:
        _manager.stop_all()


def result_text(result: types.CallToolResult) -> str:
    """The text the model gets from a tool result: text blocks as they are, other blocks as a short note."""
    parts: list[str] = []
    for c in result.content:
        if isinstance(c, types.TextContent):
            parts.append(c.text)
        elif isinstance(c, types.ImageContent):
            parts.append(
                f"[An image ({c.mime_type}). The app does not show it to the model.]"
            )
        elif isinstance(c, types.AudioContent):
            parts.append(f"[An audio clip ({c.mime_type}). The app does not play it.]")
        elif isinstance(c, types.EmbeddedResource):
            r = c.resource
            if isinstance(r, types.TextResourceContents):
                parts.append(f"[Resource {r.uri}]\n{r.text}")
            else:
                parts.append(f"[A binary resource: {r.uri}]")
        elif isinstance(c, types.ResourceLink):
            parts.append(f"[A resource link: {c.uri} ({c.name})]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    text = "\n\n".join(parts) or "(The tool returned no content.)"
    return text if len(text) <= MAX_RAW else text[:MAX_RAW]
