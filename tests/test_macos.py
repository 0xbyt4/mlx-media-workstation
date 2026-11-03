"""Keep-awake and notifications without touching the Mac: a `sleep` stands in for caffeinate, and osascript is not
run (its argument list is checked)."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app import macos
from app.macos import (
    notify as real_notify,
)  # conftest replaces macos.notify in every test

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_caffeinate_watches_the_app_process() -> None:
    assert macos.caffeinate_cmd() == [
        "/usr/bin/caffeinate",
        "-i",
        "-w",
        str(os.getpid()),
    ]


def test_awake_starts_one_process_and_stops_it() -> None:
    awake = macos.Awake(lambda: SLEEPER)
    assert awake.sync(lambda: True)
    proc = awake.proc
    assert proc is not None and proc.poll() is None
    assert awake.sync(lambda: True)
    assert awake.proc is proc  # still the same process
    assert not awake.sync(lambda: False)
    assert awake.proc is None
    assert proc.poll() is not None  # gone, not left behind


def test_awake_starts_again_after_the_process_died() -> None:
    awake = macos.Awake(lambda: SLEEPER)
    awake.sync(lambda: True)
    first = awake.proc
    first.kill()
    first.wait()
    assert awake.sync(lambda: True)
    assert awake.proc is not first and awake.proc.poll() is None
    awake.release()
    assert awake.proc is None


def test_awake_release_kills_a_process_that_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"
    code = (
        "import pathlib, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    )
    stubborn = [sys.executable, "-c", code, str(ready)]
    monkeypatch.setattr(macos, "STOP_WAIT", 0.3)
    awake = macos.Awake(lambda: stubborn)
    awake.sync(lambda: True)
    proc = awake.proc
    end = time.time() + 5
    while (
        not ready.exists() and time.time() < end
    ):  # SIGTERM must come after the handler is set
        time.sleep(0.02)
    assert ready.exists()
    awake.release()
    assert proc.poll() is not None


def test_awake_without_caffeinate_lets_jobs_run() -> None:
    awake = macos.Awake(lambda: ["/nonexistent/caffeinate"])
    assert not awake.sync(lambda: True)
    awake.release()


def test_awake_release_without_a_process() -> None:
    macos.Awake(lambda: SLEEPER).release()


def test_notify_passes_the_text_as_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[list[str]] = []
    done = threading.Event()

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        got.append(cmd)
        done.set()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(macos.subprocess, "run", run)
    text = 'Job "x" is done" & do shell script "touch /tmp/pwned'
    real_notify(text)
    assert done.wait(5)
    assert got == [
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e",
            "end run",
            text,
            "Workstation",
        ]
    ]


def test_notify_ignores_a_missing_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    done = threading.Event()

    def run(cmd: list[str], **kw: Any) -> None:
        done.set()
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(macos.subprocess, "run", run)
    real_notify("x")
    assert done.wait(5)
