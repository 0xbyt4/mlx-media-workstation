"""Read the app's token file (outputs/.server-token, written by app/server.py at start) the safe way: only a regular
file (not a link) of this user that only this user can read. start.py and the MCP server (app/agent/http_port.py)
use it. Never print or log the token."""

import os
import stat
from pathlib import Path


def read(path: Path) -> str | None:
    """The token in the file, or None: no file, a link, a folder, another user's file, or a file others can read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
        ):
            return None
        text = os.read(fd, 1024).decode("ascii", errors="replace").strip()
    finally:
        os.close(fd)
    return text or None
