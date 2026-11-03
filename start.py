#!/usr/bin/env python3
"""Start the workstation: the server on 127.0.0.1:7870 and a native window (pywebview, the system WebKit) showing it.

  .venv/bin/python start.py            # app window
  .venv/bin/python start.py --browser  # server only; open the sign-in link it prints in a browser
  .venv/bin/python start.py --chat     # Chat, Models and Settings only (the chat profile)

Without --chat, `profile` in config.toml [app] picks the profile ("full" when it is not set).

If the port is taken, start.py asks the server there for its profile (GET /api/system). It first checks that the
server is this app: the server writes a random token to outputs/.server-token (mode 0600) at start, and start.py
sends a random nonce and checks the answer's HMAC(token, nonce), so the token never leaves the file. When the running
app shows another profile, it opens only a window on that server in the profile asked for, so there is one job queue
and one local LLM (a second server would load a second copy of the 18 GB model). Closing that window leaves the
server running. In every other case (also a server that cannot prove the token) it says that the port is in use, and
exits without touching the running app's jobs.
Closing the server's own window, or Ctrl-C in --browser mode, cancels the queue and stops every model process before
exiting.
"""

import argparse
import hmac
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
# Opened from Finder or the Dock, the app gets a bare PATH without Homebrew; the models need ffmpeg and ffprobe from it
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")]
)
sys.path.insert(0, str(ROOT))
HOST, PORT = "127.0.0.1", 7870
TOKEN_FILE = ROOT / "outputs" / ".server-token"  # written by app/server.py start()
URL = f"http://{HOST}:{PORT}"
TITLES = {"full": "Workstation", "chat": "Workstation Chat"}
# width, height and smallest size of the window; a chat window needs less room than the media forms
SIZES = {"full": (1500, 950, (1100, 700)), "chat": (1100, 850, (800, 600))}


def notify(message: str) -> None:
    """Visible feedback when started from Finder or the Dock, where printed messages go only to the log."""
    subprocess.run(
        [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            'display notification (item 1 of argv) with title "Workstation"',
            "-e",
            "end run",
            message,
        ],
        check=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="run the server only")
    ap.add_argument(
        "--chat",
        action="store_true",
        help="show only Chat, Models and Settings (overrides profile in config.toml)",
    )
    return ap.parse_args(argv)


def port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # as uvicorn binds: a port left in TIME_WAIT by the last run counts as free, a listening one does not
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, PORT))
        except OSError:
            return False
    return True


def server_token() -> str | None:
    """The running server's token: a regular file (not a link) of this user that only this user can read; else
    None."""
    try:
        fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


def running_profile(timeout: float = 2.0) -> str | None:
    """The profile of the Workstation that answers on the port; None when nothing answers, or when the server there
    cannot prove that it knows the token in outputs/.server-token (another program, or no token file)."""
    token = server_token()
    if token is None:
        return None
    nonce = secrets.token_hex(16)
    try:
        with urllib.request.urlopen(
            f"{URL}/api/system?nonce={nonce}", timeout=timeout
        ) as r:
            data = json.load(r)
    except (OSError, ValueError):  # no answer, an HTTP error, or not JSON
        return None
    if not isinstance(data, dict) or not isinstance(data.get("proof"), str):
        return None
    expected = hmac.new(token.encode(), nonce.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(data["proof"], expected):
        return None
    return str(data.get("profile") or "full")


def window_url(profile: str) -> str:
    """The page for a window in this profile on a server that runs in another one: ?profile= fixes the window's
    profile, whatever the server's is."""
    return f"{URL}/?profile={profile}" + ("#/chat" if profile == "chat" else "")


def sign_in_url(token: str, page: str = URL) -> str:
    """The server's sign-in link (GET /auth): it sets the session cookie and goes on to the page."""
    path = page.removeprefix(URL) or "/"
    return f"{URL}/auth?" + urllib.parse.urlencode({"t": token, "next": path})


def open_window(profile: str, url: str) -> None:
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = (
        True  # without it a Download link replaces the app page
    )
    width, height, min_size = SIZES[profile]
    webview.create_window(
        TITLES[profile], url, width=width, height=height, min_size=min_size
    )
    webview.start()


def attach(profile: str, browser: bool) -> None:
    """The port is in use: open a window on the running server when it shows another profile, else say it is open."""
    running = running_profile()
    if running is None:
        notify("Already open, or still closing: try again in a few seconds.")
        raise SystemExit(
            f"Port {PORT} is in use: the Workstation is probably already open ({URL})."
        )
    if running == profile:
        notify("Workstation is already open.")
        raise SystemExit(f"The Workstation is already open ({URL}).")
    token = server_token()
    if token is None:  # the server closed in between
        raise SystemExit(f"Port {PORT} is in use: try again in a few seconds.")
    url = sign_in_url(token, window_url(profile))
    if browser:
        print(f"The Workstation runs already. Open {url}", flush=True)
        return
    open_window(profile, url)  # the server stays with the app that started it


def wait_ready(server: uvicorn.Server, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if server.started:
            try:
                urllib.request.urlopen(  # open without the session (server.open_route)
                    f"{URL}/api/system?nonce={secrets.token_hex(16)}", timeout=1
                )
                return
            except OSError:
                pass
        time.sleep(0.2)
    raise SystemExit("The server did not start.")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # app.config only reads config.toml. app.server is imported after the port check, so that loading it cannot
    # touch a running instance's jobs.
    try:
        from app import config
    except (
        SystemExit,
        tomllib.TOMLDecodeError,
        UnicodeDecodeError,
    ) as e:  # config.toml could not be read
        notify("config.toml has an error; see the log.")
        raise SystemExit(f"config.toml: {e}") from None
    profile = "chat" if args.chat else config.APP.profile
    if not port_free():
        attach(profile, args.browser)
        return
    config.APP = replace(config.APP, profile=profile)  # for this run; the file stays
    from app import server as srv

    srv.start()
    server = uvicorn.Server(
        # Ctrl-C in --browser mode: open answer streams get 3 s, then shutdown() stops them
        uvicorn.Config(
            srv.app,
            host=HOST,
            port=PORT,
            log_level="warning",
            timeout_graceful_shutdown=3,
        )
    )
    try:
        if args.browser:
            # the link holds this run's token: only this user can see it here and in outputs/.server-token
            print(f"Workstation at {sign_in_url(srv.token or '')}", flush=True)
            server.run()
            return
        threading.Thread(target=server.run, daemon=True).start()
        wait_ready(server)
        open_window(profile, sign_in_url(srv.token or ""))
    finally:
        srv.shutdown()
        server.should_exit = True


if __name__ == "__main__":
    main()
