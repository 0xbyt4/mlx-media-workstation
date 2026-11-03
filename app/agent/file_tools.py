"""Read-only file tools (R1): list, read and search text files inside the one folder the user granted to the chat.

Containment: every path is resolved with realpath and must stay inside the granted folder's real path, so "..",
absolute paths and symlinks cannot leave it. A file is opened with O_NOFOLLOW and its path is read back from the open
descriptor (F_GETPATH) and checked again, so a symlink swapped in after the check cannot redirect the read.

Hidden and secret files are never listed, searched or read: any path part that starts with "." (.env, .ssh, .git,
.aws), key and certificate files (*.pem, *.key, *.p12, id_rsa...), keychains and password databases. Only text files
are read (no NUL byte and mostly printable in the first 8 KB), at most MAX_READ bytes each.
"""

import codecs
import fcntl
import fnmatch
import os
import stat
import time
from typing import Any

from app.agent.policy import Risk
from app.agent.registry import Context, Tool, ToolError

MAX_READ = 1024 * 1024  # bytes read from one file
MAX_ENTRIES = 500  # lines of list_files
MAX_SCAN = 5000  # files search_files opens
SEARCH_SECONDS = 15.0
SAMPLE = 8192
# file and folder names never shown or read (lower case; any name starting with "." is refused too)
SECRET_NAMES = (
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.keychain",
    "*.keychain-db",
    "keychains",
    "*.kdbx",
    "*.gpg",
    "*.ovpn",
    "*.p8",
    "*.ppk",
    "*.asc",
    "*.tfstate",
    "*.tfstate.backup",
    "keystore",
    "utc--*",
    "*.mobileprovision",
    "credentials",
    "credentials.json",
    "*.netrc",
    "_netrc",
    "secrets.*",
    "*.secret",
    "*.secrets",
    "service-account*.json",
)


def blocked(name: str) -> bool:
    low = name.lower()
    return low.startswith(".") or any(fnmatch.fnmatchcase(low, p) for p in SECRET_NAMES)


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _root(ctx: Context) -> str:
    if ctx.folder is None:
        raise ToolError("No folder is granted to this chat.")
    try:
        fd = os.open(ctx.folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise ToolError("The granted folder is missing.") from None
    try:
        # the path as the disk spells it, so it compares with F_GETPATH of the files below it
        return os.path.realpath(_fd_path(fd))
    finally:
        os.close(fd)


def _fd_path(fd: int) -> str:
    return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])


def _rel(path: str, root: str) -> str:
    return "." if path == root else os.path.relpath(path, root)


def resolve(root: str, given: str) -> str:
    """The real path of `given` (relative to the folder, or absolute inside it), or ToolError."""
    given = given.strip() or "."
    candidate = os.path.normpath(os.path.join(root, os.path.expanduser(given)))
    for p in (candidate, os.path.realpath(candidate)):
        if not inside(p, root):
            raise ToolError(f"{given}: outside the granted folder.")
        rel = os.path.relpath(p, root)
        if rel != "." and any(blocked(part) for part in rel.split(os.sep)):
            raise ToolError(f"{given}: hidden or secret files are not available.")
    real = os.path.realpath(candidate)
    if not os.path.exists(real):
        raise ToolError(f"{given}: no such file or folder.")
    return real


def looks_text(sample: bytes) -> bool:
    """No NUL byte, few control characters, and UTF-8 (a cut character at the end is fine) or a one-byte charset."""
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 9 or 13 < b < 32)
    if control > len(sample) // 100:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        pass  # Latin-1, Windows-1252...: read with replacement characters
    return True


def read_text(path: str, root: str, limit: int | None = None) -> tuple[str, int, bool]:
    """The text of a file (decoded as UTF-8, bad bytes replaced), its size and whether it was cut at `limit`.

    Raises ToolError for a binary file, a non-regular file, or a path that leaves the folder after opening."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ToolError(f"{_rel(path, root)}: cannot open ({e.strerror}).") from None
    try:
        real = os.path.realpath(_fd_path(fd))
        if not inside(real, root):
            raise ToolError(f"{_rel(path, root)}: outside the granted folder.")
        if real != root and any(
            blocked(p) for p in os.path.relpath(real, root).split(os.sep)
        ):
            raise ToolError(
                f"{_rel(path, root)}: hidden or secret files are not available."
            )
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ToolError(f"{_rel(path, root)}: not a regular file.")
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(limit or MAX_READ)
    finally:
        os.close(fd)
    if not looks_text(data[:SAMPLE]):
        raise ToolError(f"{_rel(path, root)}: not a text file.")
    return data.decode("utf-8", errors="replace"), st.st_size, len(data) < st.st_size


def walk(start: str, root: str, ctx: Context, recursive: bool):
    """(path, is_dir, size) of the visible entries under `start`, sorted, folders followed by their contents, without
    leaving the folder. Symlinked folders are listed but not entered."""
    ctx.check_abort()
    try:
        names = sorted(os.listdir(start))
    except OSError:
        return
    for name in names:
        if blocked(name):
            continue
        path = os.path.join(start, name)
        real = os.path.realpath(path)
        if not inside(real, root) or (
            real != root
            and any(blocked(p) for p in os.path.relpath(real, root).split(os.sep))
        ):
            continue  # a symlink out of the folder, or to a hidden or secret file
        try:
            st = os.stat(real)
        except OSError:
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        if not is_dir and not stat.S_ISREG(st.st_mode):
            continue
        yield path, is_dir, st.st_size
        if is_dir and recursive and not os.path.islink(path):
            yield from walk(path, root, ctx, recursive)


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def build() -> list[Tool]:
    def list_files(args: dict[str, Any], ctx: Context) -> str:
        root = _root(ctx)
        start = resolve(root, args["path"])
        if not os.path.isdir(start):
            raise ToolError(f"{args['path']}: not a folder.")
        lines, more = [], False
        for path, is_dir, size in walk(start, root, ctx, args["recursive"]):
            if len(lines) == MAX_ENTRIES:
                more = True
                break
            rel = _rel(path, root)
            lines.append(f"{rel}/" if is_dir else f"{rel}  ({human(size)})")
        if not lines:
            return f"{_rel(start, root)}: empty (hidden files are not shown)."
        if more:
            lines.append(f"[Stopped at {MAX_ENTRIES} entries: list a subfolder.]")
        return "\n".join(lines)

    def read_file(args: dict[str, Any], ctx: Context) -> str:
        root = _root(ctx)
        path = resolve(root, args["path"])
        if os.path.isdir(path):
            raise ToolError(f"{args['path']}: a folder; use list_files.")
        text, size, cut = read_text(path, root)
        lines = text.splitlines()
        first = args["start_line"]
        shown = lines[first - 1 : first - 1 + args["max_lines"]]
        last = first + len(shown) - 1
        head = f"{_rel(path, root)} ({human(size)}, {len(lines)} lines"
        head += f" in the first {human(MAX_READ)})" if cut else ")"
        if not shown:
            return f"{head}: no lines from line {first}."
        head += f", lines {first}-{last}"
        if last < len(lines):
            head += f"; read on with start_line={last + 1}"
        return head + ":\n" + "\n".join(shown)

    def search_files(args: dict[str, Any], ctx: Context) -> str:
        root = _root(ctx)
        start = resolve(root, args["path"])
        if not os.path.isdir(start):
            raise ToolError(f"{args['path']}: not a folder.")
        query = args["query"].casefold()
        pattern = args["glob"].lower()
        limit = args["max_results"]
        hits: list[str] = []
        scanned = 0
        end = time.monotonic() + SEARCH_SECONDS
        note = ""
        for path, is_dir, size in walk(start, root, ctx, True):
            if scanned >= MAX_SCAN or time.monotonic() > end:
                note = f"[Stopped after {scanned} files: narrow the path or the glob.]"
                break
            if is_dir or not fnmatch.fnmatchcase(
                os.path.basename(path).lower(), pattern
            ):
                continue
            scanned += 1
            rel = _rel(path, root)
            if query in os.path.basename(path).casefold():
                hits.append(f"{rel}  (file name)")
            if size > MAX_READ:
                continue
            try:
                text, _, _ = read_text(os.path.realpath(path), root)
            except ToolError:
                continue  # binary or not readable
            for n, line in enumerate(text.splitlines(), 1):
                if query in line.casefold():
                    line = line.strip()
                    hits.append(f"{rel}:{n}: {line[:300]}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                note = f"[Stopped at {limit} matches.]"
                break
        if not hits:
            return f"No match for {args['query']!r} in {scanned} files." + (
                f"\n{note}" if note else ""
            )
        return "\n".join(hits[:limit] + ([note] if note else []))

    folder_note = " Paths are relative to the folder the user granted to this chat."
    path_arg = {"type": "string", "maxLength": 1024, "default": "."}
    return [
        Tool(
            name="list_files",
            description="List files and folders in the granted folder." + folder_note,
            parameters={
                "type": "object",
                "properties": {
                    "path": path_arg,
                    "recursive": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            risk=Risk.R1,
            run=list_files,
            untrusted_output=True,
        ),
        Tool(
            name="read_file",
            description="Read a text file in the granted folder, by lines."
            + folder_note,
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "default": 400,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            risk=Risk.R1,
            run=read_file,
            untrusted_output=True,
        ),
        Tool(
            name="search_files",
            description=(
                "Find text (case-insensitive, plain text, not a regex) in the text files of the granted folder "
                "and its subfolders. Returns path:line: text." + folder_note
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "path": path_arg,
                    "glob": {"type": "string", "maxLength": 100, "default": "*"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            risk=Risk.R1,
            run=search_files,
            untrusted_output=True,
        ),
    ]
