"""The tools that write files (docs/AGENT.md "Writing files"):

- write_file (R2): a new text file in the granted folder. Asks on the first use in a chat.
- edit_file (R3): a change of an existing text file in the granted folder. Asks every time; the approval card shows
  the change as a unified diff, and the change runs only while the file still gives that diff. The old file is first
  copied to the chat's work folder (backups/).
- copy_to_workspace (R1): a copy of a file of any type from the granted folder into the chat's work folder, so
  run_python can process it.
- export_from_workspace (R2): a copy of a file from the work folder into the granted folder, under a new name.

Every path rule is in app/agent/safe_fs.py: no links, no hidden or secret names, no executable types, atomic writes.
"""

import difflib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent import safe_fs
from app.agent.file_tools import _rel, _root, human, open_file, resolve
from app.agent.policy import Risk
from app.agent.registry import Context, Output, Tool, ToolError, digest, visible_lines
from app.agent.sandbox import dir_size

MAX_COPY = (
    500 * 1024 * 1024
)  # bytes of one file that copy_to_workspace or export_from_workspace copies
DISK_RESERVE = 1024 * 1024 * 1024  # free disk space a copy must leave
MAX_DIFF_LINES = 400  # lines of the diff on the approval card
MAX_DIFF_CHARS = 20000
BACKUPS = "backups"  # in the work folder


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


LINE_ENDS = "(Only the line breaks change: the end of a line or of the file.)"


def unified(old: str, new: str, rel: str, created: bool = False) -> list[str]:
    return list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            "/dev/null" if created else f"a/{rel}",
            f"b/{rel}",
            n=3,
            lineterm="",
        )
    )


def whole_diff(old: str, new: str, rel: str) -> str:
    """The whole change as a unified diff, for edit_file. A change whose diff does not fit on the approval card
    (MAX_DIFF_LINES lines, MAX_DIFF_CHARS characters) is refused, so the user always sees all of what they approve."""
    lines = unified(old, new, rel)
    if not lines:
        return LINE_ENDS
    if (
        len(lines) > MAX_DIFF_LINES
        or sum(len(x) + 1 for x in lines) - 1 > MAX_DIFF_CHARS
    ):
        raise ToolError(
            f"{rel}: the change is too large to show the user ({len(lines)} diff lines; the approval card shows at "
            f"most {MAX_DIFF_LINES} lines and {MAX_DIFF_CHARS:,} characters). Split it into smaller edit_file "
            "calls with old_text and new_text."
        )
    return "\n".join(lines)


def diff(old: str, new: str, rel: str, created: bool = False) -> str:
    """The change as a unified diff, cut at MAX_DIFF_LINES lines or MAX_DIFF_CHARS characters with a note."""
    lines = unified(old, new, rel, created)
    if not lines:
        return LINE_ENDS
    shown: list[str] = []
    size = 0
    for line in lines:
        if len(shown) == MAX_DIFF_LINES or size + len(line) > MAX_DIFF_CHARS:
            rest = len(lines) - len(shown)
            shown.append(f"[The diff goes on: {rest} more lines are not shown.]")
            break
        shown.append(line)
        size += len(line) + 1
    return "\n".join(shown)


def check_free(path: Path, size: int) -> None:
    """Refuse a copy that would leave less than DISK_RESERVE free on the disk of `path`."""
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free - size < DISK_RESERVE:
        raise ToolError(
            f"Not enough free disk space: {human(free)} free, the copy needs {human(size)} and "
            f"{human(DISK_RESERVE)} must stay free."
        )


def backup(ctx: Context, name: str, data: bytes) -> str:
    """A copy of a file's old bytes in the work folder (backups/<time>-<name>); its path there."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dfd = safe_fs.work_dir_fd(ctx.work_dir, [BACKUPS, name], True, BACKUPS)
    try:
        for n in range(1, 100):
            base = f"{stamp}-{name}" if n == 1 else f"{stamp}-{n}-{name}"
            base = base[: safe_fs.MAX_NAME]
            try:
                safe_fs.write_new(dfd, base, safe_fs.write_bytes(data), mode=0o600)
            except ToolError:
                continue  # that name exists
            return f"{BACKUPS}/{base}"
    finally:
        os.close(dfd)
    raise ToolError("Could not make a backup copy; nothing was written.")


@dataclass
class Edit:
    rel: str  # the path relative to the granted folder
    parent: str  # the real path of its folder
    name: str
    old: str  # the file's text now
    new: str  # the text after the change
    data: bytes  # the file's bytes now
    st: os.stat_result
    count: int  # replacements (0 for a full overwrite)


def plan_edit(args: dict[str, Any], ctx: Context) -> Edit:
    """Read the file and work out the change; ToolError when it cannot be made. Nothing is written here."""
    root = _root(ctx)
    given = args["path"]
    path = safe_fs.no_links(root, given)
    rel = _rel(path, root)
    if os.path.isdir(path):
        raise ToolError(f"{given}: a folder, not a file.")
    parent, name = os.path.split(path)
    safe_fs.check_name(name, given)
    safe_fs.check_parts(rel, given, root)
    fd, st = open_file(path, root)
    try:
        if st.st_mode & safe_fs.EXEC_BITS:
            raise ToolError(
                f"{given}: the file is executable; the agent does not change it."
            )
        if st.st_nlink > 1:
            raise ToolError(
                f"{given}: the file has other hard links; the agent does not change it."
            )
        if st.st_size > safe_fs.MAX_TEXT:
            raise ToolError(
                f"{given}: larger than {safe_fs.MAX_TEXT // 1024} KB; the agent changes smaller files only."
            )
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(safe_fs.MAX_TEXT + 1)
    finally:
        os.close(fd)
    old = safe_fs.decode_text(data, given)
    content, find, repl = (
        args.get("content"),
        args.get("old_text"),
        args.get("new_text"),
    )
    if content is not None:
        if find is not None or repl is not None:
            raise ToolError(
                "Give either content (the whole new file) or old_text and new_text, not both."
            )
        new, count = content, 0
    else:
        if find is None or repl is None:
            raise ToolError(
                "Give old_text and new_text, or content for the whole new file."
            )
        if find == "":
            raise ToolError("old_text must not be empty.")
        if find not in old and "\r\n" in old and "\r" not in find:
            # the file has Windows line ends and the model wrote plain ones
            find, repl = find.replace("\n", "\r\n"), repl.replace("\n", "\r\n")
        count = old.count(find)
        if count == 0:
            raise ToolError(
                f"{given}: old_text was not found. It must match the file exactly, also its spaces and line "
                "breaks; read the file again."
            )
        if count > 1 and not args["replace_all"]:
            raise ToolError(
                f"{given}: old_text is found {count} times. Give more of the text around it so it is found once, "
                "or set replace_all."
            )
        new = old.replace(find, repl)
    if new == old:
        raise ToolError(f"{given}: the change leaves the file as it is.")
    safe_fs.text_bytes(new, given)
    return Edit(rel, parent, name, old, new, data, st, count)


def build(limits_disk_mb: int) -> list[Tool]:
    work_budget = (
        limits_disk_mb * 1024 * 1024
    )  # run_python stops when the work folder grows past this

    def write_preview(args: dict[str, Any], ctx: Context) -> str:
        root = _root(ctx)
        parent, name = safe_fs.new_target(root, args["path"])
        safe_fs.text_bytes(args["content"], args["path"])
        return diff("", args["content"], _rel(os.path.join(parent, name), root), True)

    def write_file(args: dict[str, Any], ctx: Context) -> Output:
        root = _root(ctx)
        parent, name = safe_fs.new_target(root, args["path"])
        data = safe_fs.text_bytes(args["content"], args["path"])
        rel = _rel(os.path.join(parent, name), root)
        dfd = safe_fs.open_dir(parent, root)
        try:
            safe_fs.write_new(dfd, name, safe_fs.write_bytes(data))
        finally:
            os.close(dfd)
        lines = len(args["content"].splitlines())
        return Output(f"Wrote {rel} ({human(len(data))}, {lines} lines).", saved=[rel])

    def edit_preview(args: dict[str, Any], ctx: Context) -> str:
        e = plan_edit(args, ctx)
        return whole_diff(e.old, e.new, e.rel)

    def edit_file(args: dict[str, Any], ctx: Context) -> Output:
        e = plan_edit(args, ctx)
        change = whole_diff(e.old, e.new, e.rel)
        # the whole diff, unescaped, must be the one the user approved (Tool.show set ctx.digest)
        if ctx.preview is not None and (
            digest(change) != ctx.digest or visible_lines(change) != ctx.preview
        ):
            raise ToolError(
                f"{e.rel}: the file changed after the user saw the change, so nothing was written. Read it "
                "again and call edit_file again."
            )
        copy = backup(ctx, e.name, e.data)
        root = _root(ctx)
        dfd = safe_fs.open_dir(e.parent, root)
        try:
            safe_fs.write_new(
                dfd,
                e.name,
                safe_fs.write_bytes(e.new.encode("utf-8")),
                replace=True,
                mode=e.st.st_mode & 0o7777,
                expect=e.st,
            )
        finally:
            os.close(dfd)
        what = (
            "replaced the whole file"
            if args.get("content") is not None
            else plural(e.count, "replacement")
        )
        removed = added = 0
        for line in difflib.unified_diff(
            e.old.splitlines(), e.new.splitlines(), n=0, lineterm=""
        ):
            if not line.startswith(("---", "+++")):
                removed += line.startswith("-")
                added += line.startswith("+")
        return Output(
            f"Changed {e.rel}: {what}, {plural(removed, 'line')} removed, {added} added. The old file is saved "
            f"in the work folder as {copy}.",
            files=[copy],
            saved=[e.rel],
        )

    def copy_to_workspace(args: dict[str, Any], ctx: Context) -> Output:
        root = _root(ctx)
        path = resolve(root, args["path"])
        if os.path.isdir(path):
            raise ToolError(f"{args['path']}: a folder; copy one file at a time.")
        rel = _rel(path, root)
        target = args.get("to") or os.path.basename(path)
        parts = safe_fs.work_parts(target, target)
        fd, st = open_file(path, root)
        try:
            if st.st_size > MAX_COPY:
                raise ToolError(
                    f"{rel}: {human(st.st_size)}; the agent copies files up to {human(MAX_COPY)}."
                )
            used, _ = dir_size(ctx.work_dir) if ctx.work_dir.exists() else (0, 0)
            if used + st.st_size > work_budget:
                raise ToolError(
                    f"{rel}: the work folder holds {human(used)} and may hold {human(work_budget)}, so this "
                    f"file ({human(st.st_size)}) does not fit. Delete files there with run_python first."
                )
            check_free(ctx.work_dir, st.st_size)
            dfd = safe_fs.work_dir_fd(ctx.work_dir, parts, True, target)
            try:
                safe_fs.write_new(
                    dfd,
                    parts[-1],
                    safe_fs.copy_fd(fd, MAX_COPY, ctx.check_abort),
                    exists="the work folder has a file with this name. Choose another name with 'to'.",
                )
            finally:
                os.close(dfd)
        finally:
            os.close(fd)
        dest = "/".join(parts)
        return Output(
            f"Copied {rel} ({human(st.st_size)}) to the work folder as {dest}. run_python can open it there "
            f"as {dest!r}.",
            files=[dest],
        )

    def export_from_workspace(args: dict[str, Any], ctx: Context) -> Output:
        root = _root(ctx)
        src = args["path"]
        target = args.get("to") or src.strip().strip("/").split("/")[-1]
        parent, name = safe_fs.new_target(root, target)
        rel = _rel(os.path.join(parent, name), root)
        binary = Path(name).suffix.lower() in safe_fs.BINARY_TYPES
        fd, st = safe_fs.open_work_file(ctx.work_dir, src)
        try:
            if st.st_mode & safe_fs.EXEC_BITS:
                raise ToolError(
                    f"{src}: the file is executable; the agent does not export it."
                )
            if binary:
                if st.st_size > MAX_COPY:
                    raise ToolError(
                        f"{src}: {human(st.st_size)}; the agent exports files up to {human(MAX_COPY)}."
                    )
                check_free(Path(parent), st.st_size)
                fill = safe_fs.copy_fd(fd, MAX_COPY, ctx.check_abort)
            else:
                if st.st_size > safe_fs.MAX_TEXT:
                    raise ToolError(
                        f"{src}: larger than {safe_fs.MAX_TEXT // 1024} KB. Text files are exported up to that "
                        "size; media, PDF and table files up to "
                        + human(MAX_COPY)
                        + "."
                    )
                with os.fdopen(os.dup(fd), "rb") as f:
                    data = f.read(safe_fs.MAX_TEXT + 1)
                safe_fs.text_bytes(safe_fs.decode_text(data, src), src)
                fill = safe_fs.write_bytes(data)
            dfd = safe_fs.open_dir(parent, root)
            try:
                safe_fs.write_new(dfd, name, fill)
            finally:
                os.close(dfd)
        finally:
            os.close(fd)
        return Output(
            f"Copied {src} from the work folder to {rel} ({human(st.st_size)}).",
            saved=[rel],
        )

    folder_note = " Paths are relative to the folder the user granted to this chat."
    path_arg = {"type": "string", "minLength": 1, "maxLength": 1024}
    no_exec = (
        "No hidden names, no executable or script types (.sh, .command, .py, .app, .plist...), at most 1 MB of "
        "UTF-8 text."
    )
    return [
        Tool(
            name="write_file",
            description=(
                "Create a NEW text file in the granted folder. It never overwrites: to change a file that exists, "
                f"use edit_file. {no_exec}" + folder_note
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_arg,
                    "content": {"type": "string", "maxLength": safe_fs.MAX_TEXT},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=write_file,
            writes=True,
            needs_folder=True,
            explain=lambda a: (
                f"Creates the new file {a.get('path', '')!r} in the granted folder "
                f"({len(str(a.get('content', '')).splitlines())} lines)."
            ),
            preview=write_preview,
            effect=(
                "Creates new text files in the folder you grant; never overwrites one. Asks first; you can allow "
                "it for the whole chat. Once web pages or files are in the chat, it asks before every call."
            ),
        ),
        Tool(
            name="edit_file",
            description=(
                "Change an existing text file in the granted folder. Either replace old_text with new_text "
                "(old_text must match the file exactly, spaces and line breaks too, and be found once; or set "
                "replace_all), or give content to replace the whole file. Read the file first. The old file is "
                f"saved in the work folder (backups/). {no_exec}" + folder_note
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_arg,
                    "old_text": {"type": "string", "maxLength": safe_fs.MAX_TEXT},
                    "new_text": {"type": "string", "maxLength": safe_fs.MAX_TEXT},
                    "replace_all": {"type": "boolean", "default": False},
                    "content": {"type": "string", "maxLength": safe_fs.MAX_TEXT},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk=Risk.R3,
            run=edit_file,
            writes=True,
            needs_folder=True,
            explain=lambda a: (
                f"Changes {a.get('path', '')!r} in the granted folder"
                + (
                    ": replaces the whole file."
                    if a.get("content") is not None
                    else " as the diff shows. A copy of the old file goes to the chat's work folder."
                )
            ),
            preview=edit_preview,
            effect=(
                "Changes existing text files in the folder you grant. Asks before every call and shows the change; "
                "a copy of the old file goes to the chat's work folder."
            ),
        ),
        Tool(
            name="copy_to_workspace",
            description=(
                "Copy a file of any type (at most 500 MB) from the granted folder into this chat's work folder, "
                "where run_python can open it. to: its path in the work folder (default: the same name)."
                + folder_note
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_arg,
                    "to": {"type": "string", "maxLength": 1024},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk=Risk.R1,
            run=copy_to_workspace,
            side_effects=True,
            untrusted_output=True,
            explain=lambda a: (
                f"Copies {a.get('path', '')!r} from the granted folder to the chat's work folder."
            ),
            effect=(
                "Copies a file from the folder you grant into the chat's work folder, so Python can use it. Runs "
                "without asking once a folder is granted."
            ),
        ),
        Tool(
            name="export_from_workspace",
            description=(
                "Copy a file from this chat's work folder (what run_python wrote) into the granted folder, under a "
                "new name: it never overwrites. to: its path in the granted folder (default: the same name). "
                "Images, audio, video, PDF, CSV and office files up to 500 MB; other files must be UTF-8 text up "
                "to 1 MB. No hidden names, no executable or script types." + folder_note
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_arg,
                    "to": {"type": "string", "maxLength": 1024},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk=Risk.R2,
            run=export_from_workspace,
            writes=True,
            needs_folder=True,
            explain=lambda a: (
                f"Copies {a.get('path', '')!r} from the chat's work folder into the granted folder as "
                f"{a.get('to') or str(a.get('path', '')).strip('/').split('/')[-1]!r}."
            ),
            effect=(
                "Saves a file from the chat's work folder into the folder you grant, under a new name. Asks first; "
                "you can allow it for the whole chat. Once web pages or files are in the chat, it asks before every "
                "call."
            ),
        ),
    ]
