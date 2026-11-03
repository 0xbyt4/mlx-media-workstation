"""The two macOS helpers behind Settings > Jobs: keep the Mac awake while jobs wait or run, and notify when one ends.

Awake holds one `caffeinate -i -w <app pid>` process: no idle system sleep while it runs (the display may still sleep).
The -w makes caffeinate exit by itself when the app process ends, also after a crash or a kill, so none is left over.
notify passes the text as arguments of a fixed AppleScript (`on run argv`), so no job text becomes script code.
"""

import os
import subprocess
import threading
from collections.abc import Callable

CAFFEINATE = "/usr/bin/caffeinate"
OSASCRIPT = "/usr/bin/osascript"
NOTIFY_SCRIPT = [
    "-e",
    "on run argv",
    "-e",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "-e",
    "end run",
]
STOP_WAIT = 2.0  # seconds for caffeinate to exit after SIGTERM before SIGKILL


def caffeinate_cmd() -> list[str]:
    return [CAFFEINATE, "-i", "-w", str(os.getpid())]


class Awake:
    """Starts caffeinate when the queue has work and stops it when it has none. Thread-safe."""

    def __init__(self, cmd: Callable[[], list[str]] = caffeinate_cmd) -> None:
        self.cmd = cmd
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def sync(self, want: Callable[[], bool]) -> bool:
        """Hold or release by want() (called under this lock, so two callers cannot apply stale answers in the
        wrong order). Returns whether caffeinate runs now."""
        with self.lock:
            if want():
                if self.proc is None or self.proc.poll() is not None:
                    try:
                        self.proc = subprocess.Popen(
                            self.cmd(),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except (
                        OSError
                    ):  # not macOS, or caffeinate is missing: jobs still run
                        self.proc = None
            else:
                self._stop()
            return self.proc is not None

    def release(self) -> None:
        with self.lock:
            self._stop()

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def notify(text: str, title: str = "Workstation") -> None:
    """Show a macOS notification without waiting for it (osascript can take a moment)."""

    def run() -> None:
        try:
            subprocess.run(
                [OSASCRIPT, *NOTIFY_SCRIPT, text, title],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # a missed notification must not affect the jobs

    threading.Thread(target=run, daemon=True).start()
