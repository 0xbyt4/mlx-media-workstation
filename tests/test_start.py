"""start.py without a window or a server: the flags, the profile of the run, and what it does when the port is in
use (a window on the running server in the other profile, never a second server)."""

import hmac
import io
import json
import os
import re
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
    monkeypatch.setattr(start, "server_token", lambda: "tok")


def signed(start: Any, page: str) -> str:
    """The window's address: the sign-in link that goes on to the page."""
    return "http://127.0.0.1:7870/auth?" + start.urllib.parse.urlencode(
        {"t": "tok", "next": page.removeprefix("http://127.0.0.1:7870")}
    )


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
    assert calls.windows == [
        ("chat", signed(start, "http://127.0.0.1:7870/?profile=chat#/chat"))
    ]
    assert calls.notes == []


def test_full_opens_a_window_on_the_running_chat_app(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy(start, monkeypatch, "chat")
    start.main([])
    assert calls.windows == [
        ("full", signed(start, "http://127.0.0.1:7870/?profile=full"))
    ]


def test_the_profile_in_config_counts_without_the_flag(
    start: Any, calls: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config, "APP", config.App(**{**config.APP_DEFAULTS, "profile": "chat"})
    )
    busy(start, monkeypatch, "full")
    start.main([])
    assert calls.windows == [
        ("chat", signed(start, "http://127.0.0.1:7870/?profile=chat#/chat"))
    ]


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
    out = capsys.readouterr().out
    assert signed(start, "http://127.0.0.1:7870/?profile=chat#/chat") in out


TOKEN = "ab" * 32


@pytest.fixture
def token_file(start: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The running server's token file, as server.publish_token writes it."""
    path = tmp_path / ".server-token"
    path.write_text(TOKEN)
    path.chmod(0o600)
    monkeypatch.setattr(start, "TOKEN_FILE", path)
    return path


def answer(body: dict[str, Any], key: str = TOKEN) -> Any:
    """A /api/system answer with the proof for the nonce in the URL, made with this key."""

    def make(url: str) -> bytes:
        nonce = parse_qs(urlsplit(url).query)["nonce"][0]
        proof = hmac.new(key.encode(), nonce.encode(), "sha256").hexdigest()
        return json.dumps({**body, "proof": proof}).encode()

    return make


@pytest.mark.parametrize(
    ("body", "profile"),
    [
        (answer({"mem_total_gb": 48.0, "running": None, "profile": "chat"}), "chat"),
        (answer({"mem_total_gb": 48.0, "profile": "full"}), "full"),
        # the server cannot prove the token: another program, or a server from before the token
        (answer({"mem_total_gb": 48.0, "profile": "chat"}, key="cd" * 32), None),
        (b'{"mem_total_gb": 48.0, "running": null, "profile": "chat"}', None),
        (b'{"mem_total_gb": 48.0, "profile": "chat", "proof": null}', None),
        (b'{"detail": "Not Found"}', None),
        (b"[1, 2]", None),
        (b"<html>other app</html>", None),
        (urllib.error.URLError("refused"), None),
        (TimeoutError("timed out"), None),
    ],
)
def test_running_profile(
    start: Any,
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
    profile: str | None,
) -> None:
    def urlopen(url: str, timeout: float) -> io.BytesIO:
        assert re.fullmatch(
            r"http://127\.0\.0\.1:7870/api/system\?nonce=[0-9a-f]{32}", url
        )
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body(url) if callable(body) else body)

    monkeypatch.setattr(start.urllib.request, "urlopen", urlopen)
    assert start.running_profile() == profile


def test_a_new_nonce_each_time(
    start: Any, token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []
    make = answer({"profile": "chat"})

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        urls.append(url)
        return io.BytesIO(make(url))

    monkeypatch.setattr(start.urllib.request, "urlopen", urlopen)
    assert start.running_profile() == start.running_profile() == "chat"
    # a new nonce each time, and the token itself is never sent
    assert len(set(urls)) == 2 and TOKEN not in "".join(urls)


@pytest.mark.parametrize("problem", ["missing", "group", "world", "link", "folder"])
def test_no_usable_token_file_means_not_the_workstation(
    start: Any,
    token_file: Path,
    calls: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    """Without a token file only this user can read, start.py does not ask the port at all and exits as before."""
    if problem == "missing":
        token_file.unlink()
    elif problem in ("group", "world"):
        token_file.chmod(0o640 if problem == "group" else 0o604)
    elif problem == "link":
        real = token_file.with_name("real-token")
        token_file.rename(real)
        token_file.symlink_to(real)
    else:
        token_file.unlink()
        token_file.mkdir()
    monkeypatch.setattr(
        start.urllib.request,
        "urlopen",
        lambda *a, **kw: pytest.fail("asked the port without a token"),
    )
    monkeypatch.setattr(start, "port_free", lambda: False)
    with pytest.raises(SystemExit, match="Port 7870 is in use"):
        start.main(["--chat"])
    assert calls.windows == []
    assert calls.notes == [
        "Already open, or still closing: try again in a few seconds."
    ]


def test_the_server_proves_its_token(
    start: Any, client: Any, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real round trip: server.publish_token writes the file (0600), GET /api/system?nonce= answers the proof,
    start.py checks it; a clean shutdown removes the file."""
    monkeypatch.setattr(server, "token", None)  # restored after the test
    server.publish_token()
    path = state / ".server-token"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == server.token
    monkeypatch.setattr(start, "TOKEN_FILE", path)

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        r = client.get(url.removeprefix(start.URL))
        assert server.token not in r.text
        return io.BytesIO(r.content)

    monkeypatch.setattr(start.urllib.request, "urlopen", urlopen)
    assert start.running_profile() == "full"
    assert "proof" not in client.get("/api/system").json()  # the page's poll gets none
    assert client.get("/api/system?nonce=xyz").status_code == 400
    server.publish_token()  # a new start: the old proof no longer matches
    path.write_text("ef" * 32)  # a file this run did not write stays at shutdown
    server.remove_token()
    assert path.exists()
    assert start.running_profile() is None
    path.write_text(server.token)
    server.remove_token()
    assert not path.exists()


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
