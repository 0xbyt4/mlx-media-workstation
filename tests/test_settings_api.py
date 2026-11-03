"""The Settings endpoints on a temp config.toml: what GET reports, and PUT edits that keep the rest of the file."""

import os
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, settings_api

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
    return file


def put(client: TestClient, body: dict, **kw) -> object:
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
    assert row(data, "qwen_python")["used_by"]["features"] == ["Chat", "Prompt help"]
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
