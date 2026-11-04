"""Extension points: a Provider adds agent tools and one short prompt section to chats (skills, MCP servers, memory,
host tools).

The chat loop asks the providers once, at a chat's first turn, and keeps what they gave for the whole chat. The
Qwen3.8 chat template puts the tools before the system text, so a change to either one in the middle of a chat makes
the model read the whole chat again. What changes later reaches the model as a message at the end of the chat.

- tools(): every tool of the provider. Names are unique across the app; MCP tools are "<server>__<tool>".
- core(chat): the names of the tools that go in the chat's tools array. The other tools are deferred: the model finds
  them with tool_search and runs them with tool_call.
- section(chat): a short text for the system prompt (a catalog, a memory snapshot), or None. At most `budget` tokens.

A provider must be quick and must not raise. When one raises, collect() leaves it out and says why in `problems`.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent.registry import Tool


def tokens(text: str) -> int:
    """A quick token estimate (about 3 characters a token), the same rule as the chat's context check."""
    return len(text) // 3


@dataclass(frozen=True)
class Section:
    title: str  # "Skills", "Memory"
    text: str
    budget: int  # tokens

    def fits(self) -> bool:
        return tokens(self.text) <= self.budget


class Provider(Protocol):
    name: str

    def tools(self) -> list[Tool]: ...

    def core(self, chat: dict[str, Any]) -> list[str]: ...

    def section(self, chat: dict[str, Any]) -> Section | None: ...


@dataclass
class Collected:
    """What the providers give one chat."""

    tools: list[Tool] = field(default_factory=list)
    core: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    problems: list[str] = field(
        default_factory=list
    )  # "memory: <error>" for a provider left out


def collect(providers: Iterable[Provider], chat: dict[str, Any]) -> Collected:
    """Ask each provider for its tools, core names and section. A provider that raises, or whose section is over its
    budget, is left out (its tools too) and named in `problems`."""
    out = Collected()
    seen: set[str] = set()
    for p in providers:
        try:
            tools = list(p.tools())
            names = {t.name for t in tools}
            core = [n for n in p.core(chat) if n in names]
            section = p.section(chat)
        except Exception as e:  # noqa: BLE001 - one broken provider must not stop the chat
            out.problems.append(f"{p.name}: {type(e).__name__}: {e}")
            continue
        clash = sorted(names & seen)
        if clash:
            out.problems.append(
                f"{p.name}: tool names already used: {', '.join(clash)}"
            )
            continue
        if section is not None and not section.fits():
            out.problems.append(
                f"{p.name}: its prompt section has {tokens(section.text)} tokens, over its budget of {section.budget}"
            )
            continue
        seen |= names
        out.tools += tools
        out.core += core
        if section is not None:
            out.sections.append(section)
    return out
