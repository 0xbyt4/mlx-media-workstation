"""Deferred tools: the tools of a chat that are not in its tools array, and the three bridge tools that reach them.

A chat's tools array holds a few core tools (the chat template puts the array before the system text, and every schema
costs prompt tokens). Every other tool of the chat is deferred: the system text names it in a short listing, and the
model reaches it with

- tool_search(query): BM25 over each tool's name, group, description and parameter names (stdlib only); the best
  matches come back with their full schema;
- tool_describe(names): the full schema of named tools;
- tool_call(name, arguments): runs a deferred tool. The chat loop unwraps this call before anything else, so the
  policy, the approval card, the taint flag and the repeat rule see the real tool (app/chat.py Turn.call).

The model can also call a deferred tool by its name: the loop knows every tool of the chat, not only the array.

The helpers for small models live here too: nearest() for an unknown tool name, coerce() for the arguments of a
deferred tool that the parser read without its schema (as strings), and looping() for a text that repeats itself.
"""

import difflib
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.policy import Risk
from app.agent.registry import Context, Tool, ToolError

SEARCH, DESCRIBE, CALL = "tool_search", "tool_describe", "tool_call"
BRIDGE = (SEARCH, DESCRIBE, CALL)
MAX_MATCHES = 8  # tools in one tool_search or tool_describe result
OFFLINE = " Needs the network: this Mac had no network when this chat started."
# the group of each built-in tool in the listing of deferred tools; a provider's tool sets Tool.group, or takes the
# server part of its name ("<server>__<tool>")
GROUPS = {
    "update_plan": "Plan",
    "list_workstation_tools": "Media jobs",
    "job_status": "Media jobs",
    "search_library": "Media jobs",
    "create_job": "Media jobs",
    "list_jobs": "Media jobs",
    "wait_for_job": "Media jobs",
    "cancel_job": "Media jobs",
    "transcribe_audio": "Media jobs",
    "system_status": "Media jobs",
    "list_files": "Files",
    "read_file": "Files",
    "search_files": "Files",
    "write_file": "Files",
    "edit_file": "Files",
    "copy_to_workspace": "Files",
    "export_from_workspace": "Files",
    "media_info": "Media files",
    "view_image": "Media files",
    "web_search": "Web",
    "fetch_url": "Web",
    "run_python": "Code",
    "run_shell": "Code",
    "read_result": "Results",
}
WORD = re.compile(r"[a-z0-9]+")
# a text block repeated LOOP_TIMES times at the end of a text, LOOP_SPAN characters or more in all, is a loop
LOOP_TIMES = 4
LOOP_SPAN = 400
LOOP_BLOCK = 1000  # the longest block looping() looks for


def group_of(tool: Tool) -> str:
    if tool.group:
        return tool.group
    if tool.name in GROUPS:
        return GROUPS[tool.name]
    server, sep, _ = tool.name.partition("__")
    return server if sep else "Other"


def words(text: str) -> list[str]:
    """The search words of a text: lower case, split at anything but letters and digits (so "fetch_url" gives
    "fetch" and "url"), with a plain English plural "s" taken off."""
    out = []
    for w in WORD.findall(text.lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def document(tool: Tool) -> list[str]:
    """The words BM25 ranks a tool by: its name twice (a name word weighs more), its group, its description and its
    parameter names."""
    params = " ".join((tool.parameters.get("properties") or {}).keys())
    return words(
        f"{tool.name} {tool.name} {group_of(tool)} {tool.description} {params}"
    )


def rank(
    tools: list[Tool], query: str, k1: float = 1.2, b: float = 0.75
) -> list[tuple[float, Tool]]:
    """The tools that match the query, best first, by Okapi BM25 (score > 0 only)."""
    docs = [Counter(document(t)) for t in tools]
    if not docs:
        return []
    avg = sum(sum(d.values()) for d in docs) / len(docs)
    terms = set(words(query))
    df = {w: sum(1 for d in docs if w in d) for w in terms}
    scored = []
    for tool, d in zip(tools, docs, strict=True):
        size = sum(d.values())
        score = 0.0
        for w in terms:
            if not d[w]:
                continue
            idf = math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5))
            score += idf * d[w] * (k1 + 1) / (d[w] + k1 * (1 - b + b * size / avg))
        if score > 0:
            scored.append((score, tool))
    scored.sort(key=lambda pair: -pair[0])
    return scored


def nearest(name: str, names: list[str], n: int = 3) -> list[str]:
    """The tool names closest to a name the model wrote (difflib), at most n."""
    return difflib.get_close_matches(name, names, n=n, cutoff=0.5)


def coerce(parameters: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """The arguments with string values turned into the type their schema asks for, where they parse ("2" for an
    integer, "true" for a boolean, a JSON text for an array or an object). The worker types the arguments by the
    schemas of the tools array; a deferred tool's arguments come as strings."""
    props = parameters.get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        kinds = props.get(key, {}).get("type")
        kinds = [kinds] if isinstance(kinds, str) else list(kinds or [])
        if not isinstance(value, str) or not kinds or "string" in kinds:
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        ok = {
            "integer": isinstance(parsed, int) and not isinstance(parsed, bool),
            "number": isinstance(parsed, int | float) and not isinstance(parsed, bool),
            "boolean": isinstance(parsed, bool),
            "array": isinstance(parsed, list),
            "object": isinstance(parsed, dict),
        }
        if any(ok.get(k) for k in kinds):
            out[key] = parsed
    return out


def looping(text: str) -> tuple[int, int] | None:
    """(block length, times) when the text ends with one block repeated LOOP_TIMES times or more and LOOP_SPAN
    characters or more in all, else None. The shortest such block wins."""
    size = len(text)
    if size < LOOP_SPAN:
        return None
    for p in range(1, min(LOOP_BLOCK, size // LOOP_TIMES) + 1):
        if text[-1] != text[-1 - p] or text[-p:] != text[-2 * p : -p]:
            continue
        block, times = text[-p:], 2
        while (times + 1) * p <= size and text[-(times + 1) * p : -times * p] == block:
            times += 1
        if times >= LOOP_TIMES and times * p >= LOOP_SPAN:
            return p, times
    return None


@dataclass
class Catalog:
    """What the bridge tools of one chat see: every tool of the chat, the names in its tools array, the tools that
    are on now (a call, so a change in the Tools menu shows at once) and whether the chat started offline."""

    tools: dict[str, Tool]
    core: frozenset[str]
    enabled: Callable[[], frozenset[str]]
    offline: bool = False

    def visible(self) -> list[Tool]:
        """The tools the bridge can name: all but the bridge itself."""
        return [t for n, t in self.tools.items() if n not in BRIDGE]

    def schema(self, tool: Tool) -> str:
        """The tool's full schema as one line of JSON, with "needs the network" for a web tool of an offline chat."""
        fn = tool.schema()["function"]
        if self.offline and tool.network:
            fn = {**fn, "description": fn["description"] + OFFLINE}
        return json.dumps(fn, ensure_ascii=False)

    def entry(self, tool: Tool) -> str:
        if tool.name in self.core:
            return f"{tool.name}: already in your tools; call it directly."
        on = tool.name in self.enabled()
        state = (
            "Call it by name or with tool_call."
            if on
            else "It is off in this chat: ask the user to turn it on in the Tools menu."
        )
        return f"{tool.name} ({group_of(tool)}). {state}\n{self.schema(tool)}"

    def groups(self) -> str:
        names: dict[str, list[str]] = {}
        for t in self.visible():
            names.setdefault(group_of(t), []).append(t.name)
        return "; ".join(f"{g}: {', '.join(n)}" for g, n in names.items())

    def unknown(self, name: str) -> str:
        """The error for a tool name the chat does not have: the nearest names, the loaded tools, and tool_search
        when the chat has it."""
        close = nearest(name, [t.name for t in self.visible()])
        hint = f" The nearest names: {', '.join(close)}." if close else ""
        loaded = [n for n in self.tools if n in self.core and n not in BRIDGE]
        more = (
            " tool_search finds more tools by what they do."
            if SEARCH in self.core
            else ""
        )
        return f"There is no tool {name!r}.{hint} Your tools: {', '.join(loaded) or 'none'}.{more}"


def bridge(catalog: Catalog) -> list[Tool]:
    """tool_search, tool_describe and tool_call over a chat's catalog."""

    def search(args: dict[str, Any], ctx: Context) -> str:
        query, limit = args["query"].strip(), args["limit"]
        pool = catalog.visible()
        exact = [t for t in pool if t.name == query]
        hits = exact + [t for _, t in rank(pool, query) if t not in exact]
        if not hits:
            return (
                f"No tool matches {query!r}. Try other words. The tools of this chat by group: "
                f"{catalog.groups()}."
            )
        lines = [f"{i}. {catalog.entry(t)}" for i, t in enumerate(hits[:limit], 1)]
        return f"The best matches for {query!r}:\n" + "\n".join(lines)

    def describe(args: dict[str, Any], ctx: Context) -> str:
        names = [n for n in re.split(r"[\s,\[\]\"']+", args["names"]) if n]
        out = []
        for name in names[:MAX_MATCHES]:
            tool = catalog.tools.get(str(name).strip())
            out.append(
                catalog.unknown(str(name))
                if tool is None or tool.name in BRIDGE
                else catalog.entry(tool)
            )
        return "\n".join(out) or "Name at least one tool."

    def call(args: dict[str, Any], ctx: Context) -> str:
        # the chat loop runs the named tool itself (Turn.call); reaching this means it was called without the loop
        raise ToolError("tool_call runs only inside a chat answer.")

    return [
        Tool(
            name=SEARCH,
            description=(
                "Find more tools of this chat by what they do (words such as 'subtitles', 'cancel a job'). Returns "
                "the best matches with their full parameters. Use it when no tool you have fits the task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MATCHES,
                        "default": 4,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=search,
            repeatable=True,
            effect="Finds more tools of this chat. Runs without asking.",
        ),
        Tool(
            name=DESCRIBE,
            description=(
                "Show the full parameters of tools by their names, such as the names in the list of more tools."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "names": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                        "description": "One or more tool names, separated by commas.",
                    },
                },
                "required": ["names"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=describe,
            repeatable=True,
            effect="Shows the parameters of more tools. Runs without asking.",
        ),
        Tool(
            name=CALL,
            description=(
                "Run one of the more tools by its name, with its arguments as an object (tool_describe shows them). "
                "It asks for approval and follows the same rules as a direct call."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "arguments": {"type": "object", "default": {}},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            risk=Risk.R0,
            run=call,
            effect="Runs one of the more tools. The tool it runs asks as it would on its own.",
        ),
    ]
