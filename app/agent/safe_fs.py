"""Safe file writes for the agent tools: new files and changes in the granted folder, and files in the chat's work
folder (outputs/chats/<id>/ws).

Rules for every write:

- Never follow a link. In the granted folder, the given path must have no link in any part (its real path is the
  path as given). In the work folder, each part is opened with O_NOFOLLOW from the folder before it, because the
  sandboxed code can put links there.
- Write atomically: a temp file in the same folder, then link() (a new file; fails when the name exists, so nothing
  is overwritten) or rename() (a change of an existing file).
- No hidden or secret names, no executable or script types, no file with an executable bit. A new file gets the
  default mode (0644 less the umask). A changed file keeps its mode.
"""

import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

from app.agent.file_tools import blocked, fd_path, inside, resolve
from app.agent.registry import ToolError

MAX_TEXT = 1024 * 1024  # bytes of a text file that write_file and edit_file write
MAX_NAME = 255  # bytes of one file name (APFS)
# file types that run code when the user opens them (or that macOS loads): never written in the granted folder.
# Python counts: a double-clicked .py runs in Python Launcher when python.org's Python is installed.
EXEC_TYPES = frozenset(
    {
        ".app", ".command", ".tool", ".sh", ".bash", ".zsh", ".csh", ".tcsh", ".ksh", ".fish",
        ".py", ".pyw", ".pyz", ".pyc", ".rb", ".pl", ".php", ".lua", ".tcl",
        ".scpt", ".scptd", ".applescript", ".workflow", ".action", ".terminal",
        ".plist", ".mobileconfig", ".dylib", ".so", ".bundle", ".kext", ".framework", ".plugin", ".xpc",
        ".prefpane", ".saver", ".qlgenerator", ".mdimporter", ".osax", ".appex",
        ".exe", ".msi", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
        ".scr", ".reg", ".jar", ".pkg", ".mpkg", ".dmg", ".osascript", ".pth",
        ".url", ".webloc", ".inetloc", ".fileloc", ".desktop", ".lnk",
    }
)  # fmt: skip
# .pth: Python's site module runs its import lines at every start of that Python.
# folder types that macOS runs or loads as one bundle: never written in, also not as the granted folder
BUNDLE_TYPES = frozenset(
    {
        ".app", ".bundle", ".framework", ".plugin", ".kext", ".xpc", ".prefpane", ".saver", ".qlgenerator",
        ".mdimporter", ".osax", ".appex", ".scptd", ".workflow", ".action",
    }
)  # fmt: skip
# folders Python loads code from: never written in, also not as the granted folder
CODE_FOLDERS = frozenset({"site-packages", "dist-packages"})
# binary types export_from_workspace may copy into the granted folder (other files must be UTF-8 text)
BINARY_TYPES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic",
        ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff",
        ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi",
        ".pdf", ".csv", ".tsv", ".xlsx", ".docx", ".pptx", ".parquet",
    }
)  # fmt: skip
EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
CHUNK = 1024 * 1024


def check_name(name: str, where: str) -> None:
    """A file name the write tools may create: not hidden or secret, not an executable type, printable, not too long."""
    if not name or name in (".", "..") or "/" in name:
        raise ToolError(f"{where}: give a file name.")
    if blocked(name):
        raise ToolError(f"{where}: hidden or secret files cannot be written.")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ToolError(f"{where}: the name has a control character.")
    if len(name.encode("utf-8", "surrogatepass")) > MAX_NAME:
        raise ToolError(f"{where}: the name is longer than {MAX_NAME} bytes.")
    if name.endswith((" ", ".")):
        raise ToolError(f"{where}: the name cannot end with a space or a dot.")
    if Path(name).suffix.lower() in EXEC_TYPES:
        raise ToolError(
            f"{where}: {Path(name).suffix} files can run code, so the agent does not write them. Use a text type "
            "such as .txt or .md, or ask the user to save it."
        )


def code_folder(part: str) -> bool:
    """Whether a folder name is a bundle that macOS runs or loads, or a folder Python loads code from."""
    return Path(part).suffix.lower() in BUNDLE_TYPES or part.lower() in CODE_FOLDERS


def check_parts(rel: str, where: str, root: str = "") -> None:
    """No folder on the way may be an app or another bundle that macOS runs or loads, or a folder Python loads
    code from; the same for the granted folder `root` and the folders above it (a grant made before the app
    refused such folders)."""
    for part in Path(rel).parts[:-1]:
        if Path(part).suffix.lower() in EXEC_TYPES or code_folder(part):
            raise ToolError(f"{where}: cannot write inside {part}.")
    for part in Path(root).parts:
        if code_folder(part):
            raise ToolError(
                f"{where}: the granted folder is inside {part}, so the agent does not write there."
            )


def text_bytes(text: str, where: str) -> bytes:
    """The text as UTF-8, at most MAX_TEXT bytes, without NUL characters."""
    if "\x00" in text:
        raise ToolError(
            f"{where}: the text has a NUL character; only text files are written."
        )
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError(f"{where}: the text is not valid UTF-8.") from None
    if len(data) > MAX_TEXT:
        raise ToolError(f"{where}: the text is larger than {MAX_TEXT // 1024} KB.")
    return data


def decode_text(data: bytes, where: str) -> str:
    """The text of an existing file: UTF-8 only (a change would damage text in another encoding), no NUL."""
    if b"\x00" in data:
        raise ToolError(f"{where}: not a text file.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(
            f"{where}: not UTF-8 text. The agent changes only UTF-8 text files."
        ) from None


# ---------- the granted folder ----------


def no_links(root: str, given: str) -> str:
    """The path of `given` inside the folder, which must have no link in any part (also not one to a place inside)."""
    path = resolve(root, given)
    plain = os.path.normpath(os.path.join(root, os.path.expanduser(given.strip())))
    if path != plain:
        raise ToolError(
            f"{given}: the path goes through a link; the agent does not write through links."
        )
    return path


def new_target(root: str, given: str) -> tuple[str, str]:
    """(folder, name) for a new file: the folder exists inside the granted folder without links, the name is
    allowed, and no file has that name yet."""
    given = given.strip()
    folder, name = os.path.split(given)
    check_name(name, given)
    rel = os.path.relpath(os.path.normpath(os.path.join(root, given)), root)
    check_parts(rel, given, root)
    parent = no_links(root, folder or ".")
    if not os.path.isdir(parent):
        raise ToolError(f"{given}: {folder} is not a folder.")
    if os.path.lexists(os.path.join(parent, name)):
        raise ToolError(
            f"{given}: the file exists. Use edit_file to change it, or choose a new name."
        )
    return parent, name


def open_dir(path: str, root: str) -> int:
    """A folder inside the granted folder, opened with O_NOFOLLOW; its real path is checked again after the open."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise ToolError(f"cannot open the folder ({e.strerror}).") from None
    real = fd_path(fd)
    rel = os.path.relpath(real, root)
    if not inside(real, root) or (
        real != root and any(blocked(p) for p in rel.split(os.sep))
    ):
        os.close(fd)
        raise ToolError("the folder moved out of the granted folder.")
    return fd


# ---------- writing ----------


def write_new(
    dir_fd: int,
    name: str,
    fill: Callable[[int], None],
    *,
    replace: bool = False,
    mode: int = 0o644,
    expect: os.stat_result | None = None,
    exists: str = "the file exists. Use edit_file to change it, or choose a new name.",
) -> None:
    """Write a file in the folder `dir_fd` atomically: fill(fd) writes a temp file in that folder, which then
    becomes `name`. A new file (replace=False) is linked in, so an existing name is never overwritten. A change
    (replace=True) renames over the old file, and only while it is still the file `expect` describes. `exists` is
    the message when a new file's name is taken."""
    tmp = f".mmw-{secrets.token_hex(6)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode, dir_fd=dir_fd)
    try:
        try:
            if replace:
                os.fchmod(
                    fd, mode
                )  # the old file's mode, also where the umask would clear bits
            fill(fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        if replace:
            now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if expect is not None and (
                (now.st_ino, now.st_size, now.st_mtime_ns)
                != (expect.st_ino, expect.st_size, expect.st_mtime_ns)
            ):
                raise ToolError(
                    "the file changed while the agent wrote it; nothing was written."
                )
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return
        try:
            os.link(
                tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False
            )
        except FileExistsError:
            raise ToolError(f"{name}: {exists}") from None
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass  # renamed


def copy_fd(src: int, limit: int, abort: Callable[[], None]) -> Callable[[int], None]:
    """A fill function for write_new that copies the open file `src` from its start, at most `limit` bytes."""

    def fill(dst: int) -> None:
        os.lseek(src, 0, os.SEEK_SET)
        done = 0
        while chunk := os.read(src, CHUNK):
            abort()
            done += len(chunk)
            if done > limit:
                raise ToolError(
                    "the file grew past the size limit while it was copied."
                )
            view = memoryview(chunk)
            while view:
                view = view[os.write(dst, view) :]

    return fill


def write_bytes(data: bytes) -> Callable[[int], None]:
    def fill(fd: int) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]

    return fill


# ---------- the work folder ----------


def work_parts(rel: str, where: str) -> list[str]:
    """The parts of a path in the work folder: relative, no "." or "..", no hidden or secret part."""
    parts = rel.strip().strip("/").split("/")
    if not rel.strip() or any(p in ("", ".", "..") for p in parts):
        raise ToolError(
            f"{where}: give a path relative to the work folder, without '..'."
        )
    if any(blocked(p) for p in parts):
        raise ToolError(f"{where}: hidden or secret files are not available.")
    return parts


def open_work(work_dir: Path, create: bool = False) -> int:
    """The chat's work folder, opened with O_NOFOLLOW. It must not be a link (the sandbox checks the same)."""
    if create:
        os.makedirs(work_dir, mode=0o700, exist_ok=True)
    try:
        fd = os.open(work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ToolError(
            "The work folder is empty: no file was made in this chat yet."
        ) from None
    except OSError:
        raise ToolError("The work folder is a link or cannot be opened.") from None
    if fd_path(fd) != os.path.join(os.path.realpath(work_dir.parent), work_dir.name):
        os.close(fd)
        raise ToolError("The work folder is a link or cannot be opened.")
    return fd


def work_dir_fd(work_dir: Path, parts: list[str], create: bool, where: str) -> int:
    """The folder that holds the last part, each part opened with O_NOFOLLOW (made when `create`)."""
    fd = open_work(work_dir, create)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError as e:
        os.close(fd)
        raise ToolError(f"{where}: cannot open its folder ({e.strerror}).") from None
    return fd


def open_work_file(work_dir: Path, rel: str) -> tuple[int, os.stat_result]:
    """A regular file in the work folder, read-only, without following a link at any part."""
    parts = work_parts(rel, rel)
    dfd = work_dir_fd(work_dir, parts, False, rel)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        raise ToolError(f"{rel}: no such file in the work folder.") from None
    except OSError as e:
        raise ToolError(f"{rel}: cannot open ({e.strerror}).") from None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ToolError(f"{rel}: not a regular file.")
    return fd, st
