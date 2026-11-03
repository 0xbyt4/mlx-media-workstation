"""When a tool call may run: pure functions over the tool's risk, the chat's grants and the chat's taint state.

Risk levels (docs/AGENT.md):

- R0 reads app state only: runs.
- R1 reads user files in the folder the user granted: runs when a folder is granted, else refused. Granting the folder
  is the consent.
- R2 sends text off the Mac or queues minutes of compute: asks on the first use in a chat; the user can answer "allow
  in this chat". Once untrusted content (a web page, a file, a search result, program output) is in the model's
  context, every R2 call asks again, even with a chat grant. Reason: a page could tell the model to put private text
  into a query or URL (data leak), or to queue jobs; the user then sees the call before it runs.
- R3 runs code or has effects the app cannot check: asks every time. No chat-wide grant is offered.

decide() never has side effects. The loop keeps the state: the chat's Grants (stored in chat.json) and a "tainted"
flag, set by taints() after a tool that returns untrusted content ran. The flag stays set while any such result is in
the model's context: in practice for the rest of the chat (store it with the grants). A new turn does not clear it,
because the text that could steer the model is still there.
"""

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.registry import Tool


class Risk(IntEnum):
    R0 = 0  # app state only
    R1 = 1  # user files in the granted folder
    R2 = 2  # off the Mac, or minutes of compute
    R3 = 3  # code, or unknown effects


class Action(StrEnum):
    RUN = "run"
    ASK = "ask"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Grants:
    """What the user allowed in one chat: a folder for R1 tools, and R2 tools allowed for the whole chat."""

    folder: Path | None = None
    tools: frozenset[str] = field(default_factory=frozenset)

    def allow(self, name: str) -> "Grants":
        """The grants after the user answered "allow in this chat" for this tool."""
        return Grants(self.folder, self.tools | {name})

    def revoke(self, name: str) -> "Grants":
        return Grants(self.folder, self.tools - {name})


@dataclass(frozen=True)
class Verdict:
    action: Action
    reason: str  # why it asks or refuses; for the approval card or the tool message
    chat_scope: bool = (
        False  # whether "Allow in this chat" may be offered with the question
    )


def decide(
    tool: "Tool | None",
    grants: Grants,
    tainted: bool,
    enabled: frozenset[str] | None = None,
) -> Verdict:
    """Run, ask or refuse one call. `enabled` is the set of tools this chat offers (None = all)."""
    if tool is None:
        return Verdict(Action.REFUSE, "There is no such tool.")
    if enabled is not None and tool.name not in enabled:
        return Verdict(
            Action.REFUSE,
            f"The tool {tool.name} is off in this chat. Do not call it again; tell the user that they can "
            "turn it on in this chat's Tools menu.",
        )
    if tool.risk == Risk.R0:
        return Verdict(Action.RUN, "Reads app state only.")
    if tool.risk == Risk.R1:
        if grants.folder is None:
            return Verdict(
                Action.REFUSE,
                "No folder is granted to this chat. Ask the user to grant a folder.",
            )
        return Verdict(Action.RUN, "Reads the folder the user granted.")
    if tool.risk == Risk.R2:
        if tainted:
            what = "sends data off this Mac" if tool.network else "has a cost"
            return Verdict(
                Action.ASK,
                f"Untrusted content is in this chat and this call {what}: confirm it.",
            )
        if tool.name in grants.tools:
            return Verdict(Action.RUN, "Allowed in this chat.")
        # a tool whose result taints the chat asks again after its first result: a chat-wide yes would do nothing
        return Verdict(
            Action.ASK, "First use in this chat.", chat_scope=not tool.untrusted_output
        )
    return Verdict(Action.ASK, "This tool asks every time.")


def taints(tool: "Tool", tainted: bool) -> bool:
    """The chat's tainted flag after this tool ran: once set, it stays set while the result is in the context."""
    return tainted or tool.untrusted_output
