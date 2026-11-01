#!/usr/bin/env python3
"""Start the workstation: the server on 127.0.0.1:7870 and a native window (pywebview, the system WebKit) showing it.

  .venv/bin/python start.py            # app window
  .venv/bin/python start.py --browser  # server only; open http://127.0.0.1:7870 in a browser

If the port is taken (the app is already open), it says so and exits without touching the running app's jobs.
Closing the window, or Ctrl-C in --browser mode, cancels the queue and stops every model process before exiting.
"""

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
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


def port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, PORT))
        except OSError:
            return False
    return True


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="run the server only")
    args = ap.parse_args()
    if not port_free():
        notify("Already open, or still closing: try again in a few seconds.")
        raise SystemExit(
            f"Port {PORT} is in use: the Workstation is probably already open ({URL})."
        )
    # imported only now: loading it must not touch a running instance's jobs
    try:
        from app import server as srv
    except (SystemExit, tomllib.TOMLDecodeError) as e:  # config.toml could not be read
        notify("config.toml has an error; see the log.")
        raise SystemExit(f"config.toml: {e}") from None

    srv.start()
    server = uvicorn.Server(
        uvicorn.Config(srv.app, host=HOST, port=PORT, log_level="warning")
    )
    try:
        if args.browser:
            print(f"Workstation at {URL}")
            server.run()
            return
        import webview

        threading.Thread(target=server.run, daemon=True).start()
        wait_ready(server)
        webview.settings["ALLOW_DOWNLOADS"] = (
            True  # without it a Download link replaces the app page
        )
        webview.create_window(
            "Workstation", URL, width=1500, height=950, min_size=(1100, 700)
        )
        webview.start()
    finally:
        srv.shutdown()
        server.should_exit = True


if __name__ == "__main__":
    main()
