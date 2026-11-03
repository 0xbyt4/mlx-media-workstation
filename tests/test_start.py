"""start.py without a window or a server: the flags, the profile of the run, and what it does when the port is in
use (a window on the running server in the other profile, never a second server)."""

import io
import os
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from app import config, server


@pytest.fixture
def start(monkeypatch: pytest.MonkeyPatch) -> Any:
    # importing start.py puts Homebrew first in PATH; monkeypatch sets the old PATH again after the test
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    import start as module

    return module


@pytest.fixture
def calls(start: Any, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Records notifications and windows instead of showing them; a server start fails the test."""
    rec = SimpleNamespace(notes=[], windows=[])
    monkeypatch.setattr(start, "notify", rec.notes.append)
    monkeypatch.setattr(
        start, "open_window", lambda profile, url: rec.windows.append((profile, url))
    )
    monkeypatch.setattr(server, "start", lambda: pytest.fail("a server started"))
    return rec


def busy(start: Any, monkeypatch: pytest.MonkeyPatch, running: str | None) -> None:
    """The port is in use, by a Workstation in this profile (None: by something that is not the Workstation)."""
    monkeypatch.setattr(start, "port_free", lambda: False)
    monkeypatch.setattr(start, "running_profile", lambda: running)


def test_flags(start: Any) -> None:
    args = start.parse_args([])
    assert (args.chat, args.browser) == (False, False)
    args = start.parse_args(["--chat", "--browser"])
    assert (args.chat, args.browser) == (True, True)


# The port is in use


def test_chat_opens_a_window_on_the_running_full_app(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy(start, monkeypatch, "full")
    start.main(["--chat"])
    assert calls.windows == [("chat", "http://127.0.0.1:7870/?profile=chat#/chat")]
    assert calls.notes == []


def test_full_opens_a_window_on_the_running_chat_app(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy(start, monkeypatch, "chat")
    start.main([])
    assert calls.windows == [("full", "http://127.0.0.1:7870/?profile=full")]


def test_the_profile_in_config_counts_without_the_flag(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config, "APP", config.App(**{**config.APP_DEFAULTS, "profile": "chat"})
    )
    busy(start, monkeypatch, "full")
    start.main([])
    assert calls.windows == [("chat", "http://127.0.0.1:7870/?profile=chat#/chat")]


@pytest.mark.parametrize(("argv", "running"), [(["--chat"], "chat"), ([], "full")])
def test_same_profile_says_it_is_open(
    start: Any,
    calls: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    running: str,
) -> None:
    busy(start, monkeypatch, running)
    with pytest.raises(SystemExit, match="already open"):
        start.main(argv)
    assert calls.windows == []
    assert calls.notes == ["Workstation is already open."]


def test_port_used_by_something_else(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy(start, monkeypatch, None)
    with pytest.raises(SystemExit, match="Port 7870 is in use"):
        start.main(["--chat"])
    assert calls.windows == []
    assert calls.notes == [
        "Already open, or still closing: try again in a few seconds."
    ]


def test_browser_prints_the_address_of_the_window(
    start: Any,
    calls: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    busy(start, monkeypatch, "full")
    start.main(["--chat", "--browser"])
    assert calls.windows == []
    assert "http://127.0.0.1:7870/?profile=chat#/chat" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "profile"),
    [
        (b'{"mem_total_gb": 48.0, "running": null, "profile": "chat"}', "chat"),
        (b'{"mem_total_gb": 48.0, "running": null}', "full"),  # before profiles
        (b'{"detail": "Not Found"}', None),
        (b"[1, 2]", None),
        (b"<html>other app</html>", None),
        (urllib.error.URLError("refused"), None),
        (TimeoutError("timed out"), None),
    ],
)
def test_running_profile(
    start: Any,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes | Exception,
    profile: str | None,
) -> None:
    def urlopen(url: str, timeout: float) -> io.BytesIO:
        assert url == "http://127.0.0.1:7870/api/system"
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body)

    monkeypatch.setattr(start.urllib.request, "urlopen", urlopen)
    assert start.running_profile() == profile


# The port is free: this start runs the server


@pytest.mark.parametrize(
    ("argv", "saved", "profile"),
    [
        (["--browser"], "full", "full"),
        (["--browser"], "chat", "chat"),
        (["--browser", "--chat"], "full", "chat"),
    ],
)
def test_the_server_runs_in_the_profile(
    start: Any,
    calls: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    saved: str,
    profile: str,
) -> None:
    monkeypatch.setattr(
        config, "APP", config.App(**{**config.APP_DEFAULTS, "profile": saved})
    )
    monkeypatch.setattr(start, "port_free", lambda: True)
    ran: list[str] = []
    monkeypatch.setattr(server, "start", lambda: ran.append("start"))
    monkeypatch.setattr(server, "shutdown", lambda: ran.append("shutdown"))

    class FakeServer:
        def __init__(self, cfg: object) -> None:
            self.should_exit = False

        def run(self) -> None:
            # the server sees the profile of this run
            ran.append(config.APP.profile)

    monkeypatch.setattr(start.uvicorn, "Config", lambda *a, **kw: None)
    monkeypatch.setattr(start.uvicorn, "Server", FakeServer)
    start.main(argv)
    assert ran == ["start", profile, "shutdown"]
    assert calls.windows == []
