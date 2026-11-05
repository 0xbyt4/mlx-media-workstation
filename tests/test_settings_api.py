"""The Settings endpoints on a temp config.toml: what GET reports, and PUT edits that keep the rest of the file."""

import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, llm, settings_api
from app.agent import sandbox
from app.agent import settings as agent_settings

USER_FILE = """# My Mac's installs.
# Keep this comment.

[paths]
# the mflux checkout
mflux_bin = "~/mlx-media/mflux/.venv/bin"   # moved in May
  ltx_model = '/Volumes/Big/ltx'
ace_dir = "relative/ace"

[assist]
provider = "local"  # the only one

[extra]
# a section the app does not know
note = "keep me"
list = [
  [1, 2],
  [3],
]
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def cfg(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """config.toml in a temp folder; the running app's config is what that file held at start."""
    file = tmp_path / "config.toml"
    file.write_text(USER_FILE)
    monkeypatch.setattr(config, "FILE", file)
    monkeypatch.setattr(config, "PATHS", config.load(file))
    monkeypatch.setattr(config, "ASSIST", config.load_assist(file))
    monkeypatch.setattr(config, "APP", config.load_app(file))
    monkeypatch.setattr(settings_api, "AGENT", agent_settings.load(file))
    return file


ORIGIN = {"origin": "http://127.0.0.1:7870"}


def put(client: TestClient, body: dict, **kw) -> object:
    """A write from the app's own page (with its Origin), unless the test passes other headers."""
    kw.setdefault("headers", ORIGIN)
    return client.put("/api/settings", json=body, **kw)


def row(data: dict, key: str) -> dict:
    return next(r for r in data["paths"] if r["key"] == key)


# Reading


def test_get_reports_every_path(client: TestClient, cfg: Path, home: Path) -> None:
    (home / "mlx-media" / "mflux" / ".venv" / "bin").mkdir(parents=True)
    data = client.get("/api/settings").json()
    assert [r["key"] for r in data["paths"]] == list(config.DEFAULTS)
    mflux = row(data, "mflux_bin")
    assert mflux["value"] == "~/mlx-media/mflux/.venv/bin"
    assert mflux["in_file"] is True
    assert mflux["exists"] is True
    assert mflux["path"] == str(home / "mlx-media" / "mflux" / ".venv" / "bin")
    assert mflux["active"] == mflux["path"]
    assert mflux["used_by"]["tools"] == [
        "MOSS-Image",
        "FLUX.3 Klein",
        "Klein Edit (references)",
    ]
    drama = row(data, "drama_python")
    assert drama["in_file"] is False
    assert drama["value"] == drama["default"] == config.DEFAULTS["drama_python"]
    assert drama["exists"] is False
    assert row(data, "ace_dir")["path"] == str(config.ROOT / "relative" / "ace")
    assert row(data, "qwen_python")["used_by"]["features"] == [
        "Chat",
        "Prompt help",
        "Transcribe (Nemotron)",
    ]
    assert row(data, "nemotron_model")["used_by"]["tools"] == ["Transcribe"]
    assert row(data, "qwen_model")["used_by"]["models"] == ["Qwen3.8 27B (4-bit)"]
    assert data["config"] == {"path": str(cfg), "exists": True, "error": None}
    assert data["assist"]["provider"] == "local"
    assert [p["id"] for p in data["assist"]["providers"]] == ["local"]
    assert data["restart_required"] is False


def test_get_without_a_file(client: TestClient, cfg: Path) -> None:
    cfg.unlink()
    data = client.get("/api/settings").json()
    assert data["config"]["exists"] is False
    assert all(r["value"] == r["default"] for r in data["paths"])
    assert all(not r["in_file"] for r in data["paths"])


def test_get_with_a_broken_file(client: TestClient, cfg: Path) -> None:
    cfg.write_text("[paths\n")
    data = client.get("/api/settings").json()
    assert "not valid TOML" in data["config"]["error"]
    assert put(client, {"paths": {"ace_dir": "/x"}}).status_code == 409
    assert cfg.read_text() == "[paths\n"


def test_settings_hold_no_secrets(client: TestClient, cfg: Path) -> None:
    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for v in value for k in keys(v)}
        return set()

    names = " ".join(keys(client.get("/api/settings").json())).lower()
    for word in ("api_key", "apikey", "token", "password", "secret"):
        assert word not in names


# Writing


def test_put_changes_only_the_given_lines(client: TestClient, cfg: Path) -> None:
    r = put(
        client,
        {"paths": {"mflux_bin": "/opt/mflux/bin", "ltx_model": "~/models/ltx"}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] == ["paths.mflux_bin", "paths.ltx_model"]
    assert data["restart_required"] is True
    assert row(data, "mflux_bin")["value"] == "/opt/mflux/bin"
    assert row(data, "mflux_bin")["active"] != row(data, "mflux_bin")["path"]
    expected = USER_FILE.replace(
        'mflux_bin = "~/mlx-media/mflux/.venv/bin"   # moved in May',
        'mflux_bin = "/opt/mflux/bin"   # moved in May',
    ).replace("  ltx_model = '/Volumes/Big/ltx'", '  ltx_model = "~/models/ltx"')
    assert cfg.read_text() == expected


def test_put_adds_a_missing_key_after_the_last_key(
    client: TestClient, cfg: Path
) -> None:
    assert put(client, {"paths": {"qwen_model": "/q"}}).status_code == 200
    text = cfg.read_text()
    assert 'ace_dir = "relative/ace"\nqwen_model = "/q"\n\n[assist]' in text
    assert tomllib.loads(text)["extra"]["note"] == "keep me"


def test_put_adds_a_missing_section(client: TestClient, cfg: Path) -> None:
    cfg.write_text("# only a comment")
    assert put(client, {"paths": {"ace_dir": "/ace"}}).status_code == 200
    assert cfg.read_text() == '# only a comment\n\n[paths]\nace_dir = "/ace"\n'


def test_put_makes_the_file(client: TestClient, cfg: Path) -> None:
    cfg.unlink()
    r = put(client, {"paths": {"ace_dir": "~/ace"}, "assist": {"provider": "local"}})
    assert r.status_code == 200
    assert tomllib.loads(cfg.read_text()) == {
        "paths": {"ace_dir": "~/ace"},
        "assist": {"provider": "local"},
    }


def test_put_same_value_writes_nothing(client: TestClient, cfg: Path) -> None:
    before = cfg.stat().st_mtime_ns
    r = put(client, {"paths": {"ace_dir": " relative/ace "}})
    assert r.json()["changed"] == []
    assert r.json()["restart_required"] is False
    assert cfg.stat().st_mtime_ns == before


def test_put_keeps_the_file_mode(client: TestClient, cfg: Path) -> None:
    cfg.chmod(0o600)
    assert put(client, {"paths": {"ace_dir": "/ace"}}).status_code == 200
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert not [p for p in cfg.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize(
    "value",
    ["", "   ", 'a"b', "a\\b", "a\nb", "a\rb", "x" * 2000],
)
def test_put_refuses_bad_values(client: TestClient, cfg: Path, value: str) -> None:
    r = put(client, {"paths": {"ace_dir": value}})
    assert r.status_code == 400
    assert "paths.ace_dir" in r.json()["detail"]["fields"]
    assert cfg.read_text() == USER_FILE


def test_put_refuses_unknown_keys(client: TestClient, cfg: Path) -> None:
    r = put(client, {"paths": {"api_key": "x"}, "assist": {"provider": "cloud"}})
    assert r.status_code == 400
    fields = r.json()["detail"]["fields"]
    assert set(fields) == {"paths.api_key", "assist.provider"}
    assert put(client, {"assist": {"model": "x"}}).status_code == 400
    assert cfg.read_text() == USER_FILE


def test_put_refuses_a_form_it_cannot_edit(client: TestClient, cfg: Path) -> None:
    text = 'paths.ace_dir = "/inline"\n'
    cfg.write_text(text)
    r = put(client, {"paths": {"ace_dir": "/new"}})
    assert r.status_code == 409
    assert cfg.read_text() == text


def test_put_needs_the_app_origin(client: TestClient, cfg: Path) -> None:
    r = put(
        client,
        {"paths": {"ace_dir": "/ace"}},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert cfg.read_text() == USER_FILE
    # no Origin at all (curl, a script on this Mac): refused too, unlike most writes
    r = put(client, {"paths": {"ace_dir": "/ace"}}, headers={})
    assert r.status_code == 403
    r = put(client, {"agent": {"wikipedia_language": "de"}}, headers={})
    assert r.status_code == 403
    assert cfg.read_text() == USER_FILE
    ok = put(
        client,
        {"paths": {"ace_dir": "/ace"}},
        headers={"origin": "http://127.0.0.1:7870"},
    )
    assert ok.status_code == 200


def test_written_file_loads_in_the_app(
    client: TestClient, cfg: Path, home: Path
) -> None:
    put(client, {"paths": {"drama_model": "~/drama"}})
    assert config.load(cfg).drama_model == home / "drama"
    assert config.load_assist(cfg).provider == "local"


def test_crlf_line_endings_stay(client: TestClient, cfg: Path) -> None:
    cfg.write_bytes(b'[paths]\r\nace_dir = "/a"\r\n')
    assert put(client, {"paths": {"ace_dir": "/b"}}).status_code == 200
    assert cfg.read_bytes() == b'[paths]\r\nace_dir = "/b"\r\n'


def test_symlinked_file_keeps_the_link(client: TestClient, cfg: Path) -> None:
    real = cfg.parent / "real.toml"
    real.write_text(USER_FILE)
    cfg.unlink()
    os.symlink(real, cfg)
    assert put(client, {"paths": {"ace_dir": "/a"}}).status_code == 200
    assert cfg.is_symlink()
    assert 'ace_dir = "/a"' in real.read_text()


def test_labels_cover_every_path() -> None:
    assert set(settings_api.LABELS) == set(config.DEFAULTS)


# The [app] switches: they apply without a restart


def test_get_reports_the_switches(client: TestClient, cfg: Path) -> None:
    data = client.get("/api/settings").json()
    assert data["app"] == {
        "keep_awake": True,
        "notify": True,
        "llm_idle_minutes": 10,
        "profile": "full",
        "chat_effort": "off",
        "llm_draft": False,
        "defaults": {
            "keep_awake": True,
            "notify": True,
            "llm_idle_minutes": 10,
            "profile": "full",
            "chat_effort": "off",
            "llm_draft": False,
        },
        "idle_choices": [5, 10, 30, 60, 0],
        "profiles": ["full", "chat"],
        "efforts": ["off", "low", "medium", "high", "xhigh", "max"],
        "saved_profile": "full",
    }
    assert data["llm"] == {"idle_stop_s": 600.0}


def test_put_switches_apply_at_once(
    client: TestClient, cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(settings_api, "on_change", [lambda: calls.append(True)])
    body = {"app": {"keep_awake": False, "notify": False, "llm_idle_minutes": 0}}
    r = put(client, body)
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] == ["app.keep_awake", "app.notify", "app.llm_idle_minutes"]
    assert cfg.read_text() == USER_FILE + (
        "\n[app]\nkeep_awake = false\nnotify = false\nllm_idle_minutes = 0\n"
    )
    assert config.APP == config.App(keep_awake=False, notify=False, llm_idle_minutes=0)
    assert config.load_app(cfg) == config.APP
    assert calls == [True]
    assert llm.idle_stop() is None
    assert data["llm"] == {"idle_stop_s": None}
    assert data["app"]["keep_awake"] is False
    assert data["restart_required"] is False  # unlike a path change


def test_put_switch_keeps_the_line_and_its_comment(
    client: TestClient, cfg: Path
) -> None:
    cfg.write_text("[app]\nnotify = true  # yes please\nllm_idle_minutes = 5\n")
    assert put(client, {"app": {"notify": False, "llm_idle_minutes": 60}}).json()[
        "changed"
    ] == ["app.notify", "app.llm_idle_minutes"]
    assert (
        cfg.read_text()
        == "[app]\nnotify = false  # yes please\nllm_idle_minutes = 60\n"
    )
    assert llm.idle_stop() == 3600.0


def test_put_same_switch_applies_without_a_write(
    client: TestClient, cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the file says false (a change by hand after the start); the app still uses true
    cfg.write_text("[app]\nnotify = false\n")
    calls: list[bool] = []
    monkeypatch.setattr(settings_api, "on_change", [lambda: calls.append(True)])
    r = put(client, {"app": {"notify": False}})
    assert r.json()["changed"] == []
    assert config.APP.notify is False
    assert calls == [True]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("keep_awake", "yes"),
        ("keep_awake", 1),
        ("notify", None),
        ("llm_idle_minutes", 7.5),
        ("llm_idle_minutes", -1),
        ("llm_idle_minutes", 24 * 60 + 1),
        ("llm_idle_minutes", True),
        ("llm_idle_minutes", "10"),
        ("profile", "media"),
        ("profile", "Chat"),
        ("profile", True),
        ("chat_effort", "high "),
        ("chat_effort", "on"),
        ("chat_effort", True),
        ("theme", "dark"),
    ],
)
def test_put_refuses_bad_switch_values(
    client: TestClient, cfg: Path, key: str, value: object
) -> None:
    r = put(client, {"app": {key: value}})
    assert r.status_code == 400
    assert list(r.json()["detail"]["fields"]) == [f"app.{key}"]
    assert cfg.read_text() == USER_FILE
    assert config.APP == config.App(**config.APP_DEFAULTS)


def test_put_profile_applies_at_once(client: TestClient, cfg: Path) -> None:
    r = put(client, {"app": {"profile": "chat"}})
    assert r.status_code == 200
    assert r.json()["changed"] == ["app.profile"]
    assert r.json()["app"]["profile"] == "chat"
    assert r.json()["restart_required"] is False
    assert cfg.read_text() == USER_FILE + '\n[app]\nprofile = "chat"\n'
    assert config.APP.profile == "chat"
    assert config.load_app(cfg).profile == "chat"
    assert client.get("/api/system").json()["profile"] == "chat"


def test_put_chat_effort_applies_at_once(client: TestClient, cfg: Path) -> None:
    r = put(client, {"app": {"chat_effort": "medium"}})
    assert r.status_code == 200
    assert r.json()["changed"] == ["app.chat_effort"]
    assert r.json()["restart_required"] is False
    assert cfg.read_text() == USER_FILE + '\n[app]\nchat_effort = "medium"\n'
    assert config.APP.chat_effort == "medium"
    assert config.load_app(cfg).chat_effort == "medium"
    assert client.get("/api/llm").json()["default_effort"] == "medium"


def test_put_switch_keeps_the_profile_of_this_run(
    client: TestClient, cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # started with --chat: the file says nothing about the profile, and another switch does not change it
    monkeypatch.setattr(config, "APP", replace(config.APP, profile="chat"))
    app = client.get("/api/settings").json()["app"]
    assert (app["profile"], app["saved_profile"]) == ("chat", "full")
    put(client, {"app": {"notify": False}})
    assert config.APP.profile == "chat"
    assert "profile" not in cfg.read_text()
    # choosing "full" in Settings applies now and is saved for the next start
    assert put(client, {"app": {"profile": "full"}}).json()["changed"] == [
        "app.profile"
    ]
    assert config.APP.profile == "full"
    assert config.load_app(cfg).profile == "full"
    # and back: the next start is now in the chat profile, and Settings says so
    app = put(client, {"app": {"profile": "chat"}}).json()["app"]
    assert (app["profile"], app["saved_profile"]) == ("chat", "chat")


def test_put_switch_over_an_int_that_means_true(client: TestClient, cfg: Path) -> None:
    # "keep_awake = 1" is not a TOML boolean (the app would not start with it), so it counts as a change
    cfg.write_text("[app]\nkeep_awake = 1\n")
    r = put(client, {"app": {"keep_awake": True}})
    assert r.json()["changed"] == ["app.keep_awake"]
    assert cfg.read_text() == "[app]\nkeep_awake = true\n"


# The [agent] values: the registry reads them at start, so a change needs a restart

AGENT_FILE = (
    USER_FILE
    + """
[agent]
# the sandbox interpreter
python = ""   # empty: standard library only
searxng_url = ""
wikipedia_language = "en"  # the Wikipedia web_search asks
marginalia = true
"""
)


@pytest.fixture
def exe(home: Path) -> Path:
    """An executable file in the temp home folder, as a sandbox venv's python."""
    path = home / "venv-sandbox" / "bin" / "python"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_get_reports_the_agent_values(client: TestClient, cfg: Path) -> None:
    agent = client.get("/api/settings").json()["agent"]
    assert agent == {
        **agent_settings.DEFAULTS,
        "defaults": agent_settings.DEFAULTS,
        "python_path": None,
        "python_found": False,
        "python_problem": None,
        "default_python": sandbox.default_interpreter().argv[0],
        "languages": ["en", "tr", "de", "fr", "es", "it", "ja", "zh"],
        "restart_required": False,
    }


def test_put_agent_values_keep_the_comments(
    client: TestClient, cfg: Path, exe: Path
) -> None:
    cfg.write_text(AGENT_FILE)
    body = {
        "agent": {
            "python": "~/venv-sandbox/bin/python",
            "searxng_url": "http://127.0.0.1:8888/",
            "wikipedia_language": "tr",
            "marginalia": False,
        }
    }
    r = put(client, body)
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] == [f"agent.{k}" for k in body["agent"]]
    assert data["restart_required"] is True
    assert data["agent"]["restart_required"] is True
    assert data["agent"]["python"] == "~/venv-sandbox/bin/python"
    assert data["agent"]["python_path"] == str(exe)
    assert data["agent"]["python_found"] is True
    assert cfg.read_text() == USER_FILE + (
        "\n[agent]\n"
        "# the sandbox interpreter\n"
        'python = "~/venv-sandbox/bin/python"   # empty: standard library only\n'
        'searxng_url = "http://127.0.0.1:8888/"\n'
        'wikipedia_language = "tr"  # the Wikipedia web_search asks\n'
        "marginalia = false\n"
    )
    assert agent_settings.load(cfg) == agent_settings.Settings(
        python=exe,
        searxng_url="http://127.0.0.1:8888",
        wikipedia_language="tr",
        marginalia=False,
    )
    # back to the values of the start: no restart needed any more
    back = put(client, {"agent": agent_settings.DEFAULTS}).json()
    assert back["agent"]["restart_required"] is False
    assert back["restart_required"] is False
    assert cfg.read_text() == AGENT_FILE


def test_put_agent_adds_the_section(client: TestClient, cfg: Path) -> None:
    r = put(client, {"agent": {"marginalia": False, "wikipedia_language": "pt-br"}})
    assert r.status_code == 200
    assert cfg.read_text() == USER_FILE + (
        '\n[agent]\nmarginalia = false\nwikipedia_language = "pt-br"\n'
    )
    assert agent_settings.load(cfg).wikipedia_language == "pt-br"


def test_put_agent_same_value_writes_nothing(client: TestClient, cfg: Path) -> None:
    cfg.write_text(AGENT_FILE)
    r = put(client, {"agent": {"python": "  ", "marginalia": True}})
    assert r.json()["changed"] == []
    assert cfg.read_text() == AGENT_FILE


@pytest.mark.parametrize(
    "url",
    ["", "https://search.example/searx/", "http://[::1]:8080", "http://searx.local"],
)
def test_put_agent_takes_good_urls(client: TestClient, cfg: Path, url: str) -> None:
    assert put(client, {"agent": {"searxng_url": url}}).status_code == 200


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("python", "relative/bin/python"),
        ("python", "~/missing/bin/python"),
        ("python", "~"),  # a folder
        ("python", "~/venv-sandbox/bin/notes.txt"),  # not executable
        ("python", 'a"b'),
        ("python", "a\\b"),
        ("python", "/usr/bin\n/python3"),
        ("python", True),
        ("searxng_url", "ftp://x"),
        ("searxng_url", "localhost:8888"),
        ("searxng_url", "http://"),
        ("searxng_url", "http://user:pw@127.0.0.1:8888"),
        ("searxng_url", "http://user@127.0.0.1:8888"),
        ("searxng_url", "http://127.0.0.1:8888/?format=json"),
        ("searxng_url", "http://127.0.0.1:8888/?"),
        ("searxng_url", "http://127.0.0.1:8888/#top"),
        ("searxng_url", "http://127.0.0.1:99999"),
        ("searxng_url", 'http://a"b'),
        ("searxng_url", "http://h/" + "x" * 1100),
        ("wikipedia_language", "EN"),
        ("wikipedia_language", "english"),
        ("wikipedia_language", "en_US"),
        ("wikipedia_language", "e"),
        ("wikipedia_language", ""),
        ("wikipedia_language", "en/../x"),
        ("marginalia", "true"),
        ("marginalia", 1),
        ("marginalia", None),
        ("shell", True),
    ],
)
def test_put_refuses_bad_agent_values(
    client: TestClient, cfg: Path, exe: Path, key: str, value: object
) -> None:
    (exe.parent / "notes.txt").write_text("x")
    r = put(client, {"agent": {key: value}, "app": {"notify": False}})
    assert r.status_code == 400
    assert list(r.json()["detail"]["fields"]) == [f"agent.{key}"]
    assert cfg.read_text() == USER_FILE
    assert config.APP.notify is True  # nothing of a refused request applies


def test_put_agent_python_must_be_there_now(
    client: TestClient, cfg: Path, exe: Path
) -> None:
    r = put(client, {"agent": {"python": str(exe)}})
    assert r.status_code == 200
    exe.unlink()  # the environment was removed after the save
    agent = client.get("/api/settings").json()["agent"]
    assert agent["python"] == str(exe)
    assert agent["python_found"] is False


def test_bad_agent_value_in_the_file_shows_the_default(
    client: TestClient, cfg: Path
) -> None:
    cfg.write_text('[agent]\nwikipedia_language = "EN"\nmarginalia = false\n')
    agent = client.get("/api/settings").json()["agent"]
    assert agent["wikipedia_language"] == "en"
    assert agent["marginalia"] is False


@pytest.mark.parametrize(
    "url", ["http://user:pw@127.0.0.1:8888", "http://127.0.0.1:8888/?q=x"]
)
def test_a_url_the_screen_refuses_also_stops_the_start(
    tmp_path: Path, url: str
) -> None:
    file = tmp_path / "config.toml"
    file.write_text(f'[agent]\nsearxng_url = "{url}"\n')
    with pytest.raises(SystemExit, match="searxng_url"):
        agent_settings.load(file)


def test_put_agent_python_that_others_can_change_is_refused(
    client: TestClient,
    cfg: Path,
    exe: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app runs [agent] python outside the sandbox: the file and its folders must be the user's (or root's) and
    not writable by others, up to a folder only the user can open or one root owns."""

    def refused(reason: str) -> None:
        r = put(client, {"agent": {"python": str(exe)}})
        assert r.status_code == 400, r.text
        assert reason in r.json()["detail"]["fields"]["agent.python"]
        assert cfg.read_text() == USER_FILE

    exe.chmod(0o775)  # the file is group-writable
    refused("can be changed by other users")
    exe.chmod(0o757)  # world-writable
    refused("can be changed by other users")
    exe.chmod(0o755)
    exe.parent.chmod(0o777)  # its folder
    refused(str(exe.parent))
    exe.parent.chmod(0o755)
    home.chmod(0o775)  # a folder further up
    refused(str(home))
    home.chmod(0o755)
    other = os.getuid() + 1000
    with monkeypatch.context() as m:  # the files belong to another user
        m.setattr(sandbox.os, "getuid", lambda: other)
        refused("belongs to another user")
    assert put(client, {"agent": {"python": str(exe)}}).status_code == 200


def test_unsafe_python_after_the_save_is_refused_at_start(
    client: TestClient, cfg: Path, exe: Path
) -> None:
    """A python that became group-writable after the save: the Settings screen says why, and the sandbox refuses it
    (no run_python tool, and it is never run)."""
    assert put(client, {"agent": {"python": str(exe)}}).status_code == 200
    assert client.get("/api/settings").json()["agent"]["python_problem"] is None
    exe.chmod(0o775)
    agent = client.get("/api/settings").json()["agent"]
    assert agent["python_found"] is True
    assert "can be changed by other users" in agent["python_problem"]
    box = sandbox.SeatbeltSandbox(exe)
    assert "refused" in (box.unavailable() or "")
    with pytest.raises(ValueError, match="refused"):
        sandbox.configured_interpreter(exe)
    from app.agent import Settings, build_registry

    reg = build_registry(None, Settings(python=exe))
    assert "run_python" not in reg.names()
