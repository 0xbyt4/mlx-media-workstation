"""Agent tools and the sandbox for the Chat's agent mode (docs/AGENT.md).

The chat loop uses this interface:

    registry = build_registry(port, load_settings())   # once, at start; port = the server's WorkstationPort
    tools = registry.schemas(names)                     # for the chat template (OpenAI-style function schemas)

    tool = registry.get(call.name)
    verdict = decide(tool, grants, tainted, enabled)    # run, ask (approval card) or refuse
    explain = tool.describe(args) if tool else ""       # one line for the approval card
    result = registry.call(call.name, call.arguments, Context(chat_id, grants.folder, abort, work_dir))
    tainted = taints(tool, tainted)                     # after the call ran

Nothing here imports app.server: the server's functions come in through WorkstationPort (app/agent/ports.py).
"""

from pathlib import Path

from app.agent import file_tools, plan_tool, python_tool, web_tools, ws_tools
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
    "Settings",
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
    """The chat's workspace, where run_python reads and writes: outputs/chats/<id>/ws."""
    return chats / chat_id / "ws"


def build_registry(
    port: WorkstationPort | None,
    settings: Settings,
    *,
    net: Net | None = None,
    sandbox: Sandbox | None = None,
    limits: Limits | None = None,
) -> Registry:
    """Every agent tool this setup can offer. No port (chat-only profile) = no Workstation tools; no usable
    sandbox = no run_python. Pass `net` and `sandbox` to replace the network and the sandbox (tests)."""
    tools = plan_tool.build()
    if port is not None:
        tools += ws_tools.build(port)
    tools += file_tools.build()
    tools += web_tools.build(settings, net or Net())
    box = sandbox if sandbox is not None else SeatbeltSandbox(settings.python)
    if box.unavailable() is None:
        packages = (
            "Standard library only."
            if settings.python is None
            else "The packages of the sandbox environment are available."
        )
        tools.append(python_tool.build(box, limits or Limits(), packages))
    return Registry(tools)
