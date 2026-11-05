"""Agent tools and the sandbox for the Chat's agent mode (docs/AGENT.md).

The chat loop uses this interface:

    registry = build_registry(port, load_settings())   # once, at start; port = the server's WorkstationPort
    tools = registry.schemas(names)                     # for the chat template (OpenAI-style function schemas)

    tool = registry.get(call.name)
    verdict = decide(tool, grants, tainted, enabled)    # run, ask (approval card) or refuse
    explain = tool.describe(args) if tool else ""       # one line for the approval card
    ctx = Context(chat_id, grants.folder, abort, work_dir, deadline)
    ctx.preview = tool.show(args, ctx)                  # on "ask": the card's diff; ToolError = do not ask
    result = registry.call(call.name, call.arguments, ctx)  # result.images: pictures for the next request
    tainted = taints(tool, tainted)                     # after the call ran

Nothing here imports app.server: the server's functions come in through WorkstationPort (app/agent/ports.py).

A chat's tools array holds its core tools only; app/agent/deferred.py has the bridge tools (tool_search, tool_describe,
tool_call) that reach the others, and app/agent/spill.py saves a result over MAX_RESULT for read_result. The chat loop
builds one registry per chat from these (app/chat.py chat_registry).
"""

from pathlib import Path

from app.agent import (
    file_tools,
    media_tools,
    plan_tool,
    python_tool,
    shell_tool,
    web_tools,
    write_tools,
    ws_tools,
)
from app.agent.policy import Action, Grants, Risk, Verdict, decide, taints
from app.agent.ports import WorkstationPort
from app.agent.registry import (
    MAX_RESULT,
    Context,
    Output,
    Registry,
    Result,
    Tool,
    ToolError,
)
from app.agent.sandbox import Limits, RunResult, Sandbox, SeatbeltSandbox
from app.agent.settings import Settings
from app.agent.settings import load as load_settings
from app.agent.shell_sandbox import SeatbeltShell
from app.agent.shell_tool import ShellSandbox
from app.agent.web_tools import Net

__all__ = [
    "MAX_RESULT",
    "Action",
    "Context",
    "Grants",
    "Limits",
    "Net",
    "Output",
    "Registry",
    "Result",
    "Risk",
    "RunResult",
    "Sandbox",
    "SeatbeltSandbox",
    "SeatbeltShell",
    "Settings",
    "ShellSandbox",
    "Tool",
    "ToolError",
    "Verdict",
    "WorkstationPort",
    "build_registry",
    "decide",
    "load_settings",
    "taints",
    "work_dir",
]


def work_dir(chats: Path, chat_id: str) -> Path:
    """The chat's workspace, where run_python and run_shell read and write: outputs/chats/<id>/ws."""
    return chats / chat_id / "ws"


def build_registry(
    port: WorkstationPort | None,
    settings: Settings,
    *,
    net: Net | None = None,
    sandbox: Sandbox | None = None,
    limits: Limits | None = None,
    shell: ShellSandbox | None = None,
) -> Registry:
    """Every agent tool this setup can offer. No port (chat-only profile) = no Workstation tools; no usable
    sandbox = no run_python or run_shell. Pass `net`, `sandbox` and `shell` to replace the network and the
    sandboxes (tests)."""
    tools = plan_tool.build()
    if port is not None:
        tools += ws_tools.build(port)
    tools += file_tools.build()
    tools += write_tools.build((limits or Limits()).disk_mb, port)
    tools += media_tools.build(port)
    tools += web_tools.build(settings, net or Net())
    box = sandbox if sandbox is not None else SeatbeltSandbox(settings.python)
    if box.unavailable() is None:
        packages = (
            "Standard library only."
            if settings.python is None
            else "The packages of the sandbox environment are available."
        )
        tools.append(python_tool.build(box, limits or Limits(), packages))
    sh = shell if shell is not None else SeatbeltShell()
    if sh.unavailable() is None:
        tools.append(shell_tool.build(sh, limits or Limits(), shell_tool.programs()))
    return Registry(tools)
