#!/usr/bin/env python3
"""Start the workstation: the server on 127.0.0.1:7870 and a native window (pywebview, the system WebKit) showing it.

  .venv/bin/python start.py            # app window
  .venv/bin/python start.py --browser  # server only; open http://127.0.0.1:7870 in a browser
  .venv/bin/python start.py --chat     # Chat, Models and Settings only (the chat profile)

Without --chat, `profile` in config.toml [app] picks the profile ("full" when it is not set).

If the port is taken, start.py asks the server there for its profile (GET /api/system). When the running app shows
another profile, it opens only a window on that server in the profile asked for, so there is one job queue and one
local LLM (a second server would load a second copy of the 18 GB model). Closing that window leaves the server
running. In every other case it says that the app is open, and exits without touching the running app's jobs.
Closing the server's own window, or Ctrl-C in --browser mode, cancels the queue and stops every model process before
exiting.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tomllib
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
        try:
            s.bind((HOST, PORT))
        except OSError:
            return False
    return True


def running_profile(timeout: float = 2.0) -> str | None:
    """The profile of the Workstation that answers on the port; None when nothing answers, or not the Workstation."""
    try:
        with urllib.request.urlopen(f"{URL}/api/system", timeout=timeout) as r:
            data = json.load(r)
    except (OSError, ValueError):  # no answer, an HTTP error, or not JSON
        return None
    if not isinstance(data, dict) or "mem_total_gb" not in data:
        return None
    return str(
        data.get("profile") or "full"
    )  # a server from before profiles shows everything


def window_url(profile: str) -> str:
    """The page for a window in this profile on a server that runs in another one: ?profile= fixes the window's
    profile, whatever the server's is."""
    return f"{URL}/?profile={profile}" + ("#/chat" if profile == "chat" else "")


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
    url = window_url(profile)
    if browser:
        print(f"The Workstation runs already. Open {url}")
        return
    open_window(profile, url)  # the server stays with the app that started it


def wait_ready(server: uvicorn.Server, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if server.started:
            try:
                urllib.request.urlopen(f"{URL}/api/system", timeout=1)
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
    except (SystemExit, tomllib.TOMLDecodeError) as e:  # config.toml could not be read
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
        uvicorn.Config(srv.app, host=HOST, port=PORT, log_level="warning")
    )
    try:
        if args.browser:
            print(f"Workstation at {URL}")
            server.run()
            return
        threading.Thread(target=server.run, daemon=True).start()
        wait_ready(server)
        open_window(profile, URL)
    finally:
        srv.shutdown()
        server.should_exit = True


if __name__ == "__main__":
    main()
