"""Result spill: a tool result over the cap (registry.MAX_RESULT) is saved whole in the chat's workspace, and the model
gets the start of it with the file name; read_result reads on.

The files are results/<call id>.txt in the workspace (outputs/chats/<id>/ws), so run_python and run_shell can read
them too. Code in the sandbox writes in the workspace, so every part of the path is opened with O_NOFOLLOW from the
workspace's own folder: a link that the code made cannot send a write or a read out of it.
"""

import os
import re
import uuid
from pathlib import Path
from typing import Any

from app.agent.policy import Risk
from app.agent.registry import MAX_RESULT, Context, Tool, ToolError

NAME = "read_result"
FOLDER = "results"
MAX_SPILL = (
    4 * 1024 * 1024
)  # characters of one saved result; the rest is left out (with a note)
FILE = re.compile(r"[A-Za-z0-9_-]{1,80}\.txt")
CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
NOFOLLOW = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _folder(work: Path, create: bool) -> int:
    """An fd of <work>/results, opened without following links; the folders are made when create is true."""
    if create:
        work.mkdir(parents=True, exist_ok=True)
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        if create:
            try:
                os.mkdir(FOLDER, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        return os.open(FOLDER, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def save(work: Path, call_id: str, text: str) -> str | None:
    """Write a whole result as results/<call id>.txt in the workspace; returns that path (relative to the workspace),
    or None when it cannot be written (the model then gets the cut result as before)."""
    name = f"{call_id if CALL_ID.fullmatch(call_id) else uuid.uuid4().hex[:12]}.txt"
    if len(text) > MAX_SPILL:
        text = text[:MAX_SPILL] + f"\n\n[Cut: the result had {len(text):,} characters.]"
    try:
        folder = _folder(work, True)
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW,
                0o600,
                dir_fd=folder,
            )
        finally:
            os.close(folder)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogatepass") as f:
            f.write(text)
    except OSError:
        return None
    return f"{FOLDER}/{name}"


def note(text: str, path: str) -> str:
    """The start of a long result with a note that names the saved file, MAX_RESULT characters in all."""
    tail = (
        f"\n\n[Cut: this shows the first {{shown:,}} of {len(text):,} characters. The whole result is saved in the "
        f'work folder as {path}. Read on with {NAME}(file="{path}", offset={{shown}}).]'
    )
    shown = MAX_RESULT - len(tail.format(shown=MAX_RESULT))
    return text[:shown] + tail.format(shown=shown)


def build() -> Tool:
    def read(args: dict[str, Any], ctx: Context) -> str:
        name = args["file"].strip().removeprefix(f"{FOLDER}/")
        if not FILE.fullmatch(name):
            raise ToolError(
                f"{args['file']}: not a saved result. Give the file name from the cut result."
            )
        try:
            folder = _folder(ctx.work_dir, False)
            try:
                fd = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=folder)
            finally:
                os.close(folder)
            with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_SPILL + 1000)
        except OSError:
            raise ToolError(f"No saved result {args['file']!r} in this chat.") from None
        start = args["offset"]
        if start >= len(text):
            return f"{FOLDER}/{name} has {len(text):,} characters: nothing from offset {start:,}."
        head = f"{FOLDER}/{name} ({len(text):,} characters), characters {start:,}-{{end:,}}"
        part = text[start : start + MAX_RESULT - 200]
        end = start + len(part)
        more = (
            f"; read on with offset={end}" if end < len(text) else "; this is the end"
        )
        return head.format(end=end) + more + ":\n" + part

    return Tool(
        name=NAME,
        description=(
            "Read on in a long tool result that was cut: the app saved it whole in the work folder. Give the file "
            "name from the cut result and the offset where to go on."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "minLength": 1, "maxLength": 200},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["file"],
            "additionalProperties": False,
        },
        risk=Risk.R0,
        run=read,
        needs_folder=False,
        untrusted_output=True,  # the text of a tool result, which the sandbox's code could also have changed
        effect="Reads a long tool result that the app saved in this chat's work folder. Runs without asking.",
    )
