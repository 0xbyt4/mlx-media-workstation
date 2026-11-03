"""The tool registry: what each agent tool is, its schema for the chat template, and one checked way to run it.

A Tool has a name, a description and a JSON-schema `parameters` object (the model sees these three), a risk level
(app/agent/policy.py) and run(args, ctx). Registry.call() checks the arguments against the schema, runs the tool and
caps the text the model gets. A tool reports a problem the model can act on with ToolError; any other exception
becomes an error result too, so a broken tool never ends the turn.
"""

import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.agent import schema
from app.agent.policy import Risk

MAX_RESULT = 12000  # characters of a result in the model's context (about 4,000 tokens)
# characters that hide or reorder text on the approval card: C0 and C1 controls, the soft hyphen, zero-width
# characters and bidi marks, line and paragraph separators, bidi embeddings, overrides and isolates, invisible
# operators, variation selectors, the byte order mark and the tag characters (invisible text)
HIDDEN = re.compile(
    "[\x00-\x1f\x7f-\x9f\xad\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069"
    "\ufe00-\ufe0f\ufeff\U000e0000-\U000e007f]"
)


def visible(text: str) -> str:
    """The text with each hiding character written as \\uXXXX (\\uXXXXX for a tag character), so the user reads
    what the call really sends."""
    return HIDDEN.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


class ToolError(Exception):
    """A failure the model should read ("No such file: notes.txt")."""


class Cancelled(Exception):
    """The user pressed Stop while the tool ran."""


@dataclass
class Context:
    """What one call may use. The loop makes one per call (or per turn)."""

    chat_id: str
    folder: Path | None  # the folder the user granted to this chat, or None
    abort: threading.Event  # set by Stop: a tool checks it and ends early
    work_dir: Path  # the chat's workspace (outputs/chats/<id>/ws): run_python reads and writes only here
    # when the answer must end (time.monotonic()), so a tool that waits stops in time; None = no limit
    deadline: float | None = None

    def check_abort(self) -> None:
        if self.abort.is_set():
            raise Cancelled


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[
        str, Any
    ]  # a JSON-schema object (app/agent/schema.py lists the supported subset)
    risk: Risk
    run: Callable[[dict[str, Any], Context], "str | Output"]
    network: bool = False  # sends data off this Mac
    # its result may hold text from outside the app (pages, files, program output)
    untrusted_output: bool = False
    # one line for the approval card: what this call does ("Sends the query ... to en.wikipedia.org")
    explain: Callable[[dict[str, Any]], str] | None = None
    # the host the call contacts, as the network sees it (IDNA), for its own line on the approval card
    host: Callable[[dict[str, Any]], str | None] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe(self, args: dict[str, Any]) -> str:
        """The approval card's line, with hiding characters escaped (visible)."""
        return visible(self.explain(args) if self.explain else f"Runs {self.name}.")

    def target(self, args: dict[str, Any]) -> str | None:
        """The host the call contacts (ASCII, IDNA), or None for a tool without one or a URL with no valid host."""
        return self.host(args) if self.host else None


@dataclass
class Output:
    """A tool's result when it has more than text: files it made (paths relative to the work dir)."""

    text: str
    files: list[str] = field(default_factory=list)
    job: str | None = (
        None  # the media job the call queued or waited for: the page shows its card
    )


@dataclass
class Result:
    status: Literal["ok", "error", "cancelled"]
    content: str  # what the model gets: capped at MAX_RESULT characters
    full: str  # the whole text, for chat.json and the page
    truncated: bool
    files: list[str]
    seconds: float
    args: (
        dict[str, Any] | None
    )  # the checked arguments (defaults filled in); None when they were invalid
    job: str | None = None  # Output.job


def cap(text: str, limit: int = MAX_RESULT) -> tuple[str, bool]:
    """The text cut to `limit` characters, with a note that says how much was left out."""
    if len(text) <= limit:
        return text, False
    note = f"\n\n[Truncated: showing the first {limit:,} of {len(text):,} characters.]"
    return text[: max(0, limit - len(note))] + note, True


class Registry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self.tools: dict[str, Tool] = {}
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"duplicate tool {t.name}")
            self.tools[t.name] = t

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools)

    def schemas(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI-style function schemas for the chat template; unknown names are skipped."""
        picked = self.tools if names is None else names
        return [self.tools[n].schema() for n in picked if n in self.tools]

    def call(self, name: str, args: Any, ctx: Context) -> Result:
        """Check the arguments, run the tool and cap its text. Call only after policy.decide() said run (or the user
        allowed it)."""
        t0 = time.monotonic()
        tool = self.tools.get(name)

        def done(status, text, checked=None, files=None, job=None) -> Result:
            content, truncated = cap(text)
            return Result(
                status,
                content,
                text,
                truncated,
                files or [],
                round(time.monotonic() - t0, 2),
                checked,
                job,
            )

        if tool is None:
            return done(
                "error", f"Unknown tool {name!r}. Tools: {', '.join(self.tools)}."
            )
        try:
            checked = schema.check(tool.parameters, args)
        except schema.ArgumentError as e:
            return done("error", f"Invalid arguments for {name}: {e}")
        if ctx.abort.is_set():
            return done("cancelled", "Stopped by the user.", checked)
        try:
            out = tool.run(checked, ctx)
        except Cancelled:
            return done("cancelled", "Stopped by the user.", checked)
        except ToolError as e:
            return done("error", str(e), checked)
        except Exception as e:  # noqa: BLE001 - a broken tool must not end the turn
            return done("error", f"The tool failed: {type(e).__name__}: {e}", checked)
        if isinstance(out, Output):
            return done("ok", out.text, checked, out.files, out.job)
        return done("ok", out, checked)
