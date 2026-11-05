"""config.toml loading: defaults, overrides, "~" and relative paths, unknown keys."""

import dataclasses
import tomllib
from pathlib import Path

import pytest

from app import config


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home folder, so "~" expands the same way on every machine."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


def write(tmp_path: Path, text: str) -> Path:
    file = tmp_path / "config.toml"
    file.write_text(text)
    return file


def defaults(home: Path) -> config.Paths:
    base = home / "mlx-media"
    return config.Paths(
        mflux_bin=base / "mflux" / ".venv" / "bin",
        ltx_bin=base / "venv-ltx" / "bin",
        ltx_model=base / "models" / "ltx-2.5-mlx-q8",
        drama_python=base / "venv-drama" / "bin" / "python",
        drama_model=base / "models" / "fish-audio-drama-3-4bit",
        ace_dir=base / "ace-step",
        qwen_python=base / "venv-qwen" / "bin" / "python",
        qwen_model=base / "models" / "qwen3.8-27b-4bit",
        ocr_model=base / "models" / "ovisocr3",
    )


def test_defaults_without_a_file(tmp_path: Path, home: Path) -> None:
    assert config.load(tmp_path / "missing.toml") == defaults(home)


@pytest.mark.parametrize("text", ["", "# only a comment\n", "[paths]\n"])
def test_defaults_with_an_empty_file(tmp_path: Path, home: Path, text: str) -> None:
    assert config.load(write(tmp_path, text)) == defaults(home)


def test_override_replaces_only_the_given_keys(tmp_path: Path, home: Path) -> None:
    file = write(
        tmp_path,
        '[paths]\nmflux_bin = "/opt/mflux/bin"\nace_dir = "/Volumes/Models/ace"\n',
    )
    assert config.load(file) == dataclasses.replace(
        defaults(home),
        mflux_bin=Path("/opt/mflux/bin"),
        ace_dir=Path("/Volumes/Models/ace"),
    )


def test_tilde_expands_to_the_home_folder(tmp_path: Path, home: Path) -> None:
    file = write(tmp_path, '[paths]\nltx_model = "~/weights/ltx"\n')
    assert config.load(file).ltx_model == home / "weights" / "ltx"


def test_relative_path_counts_from_the_repository(tmp_path: Path, home: Path) -> None:
    file = write(tmp_path, '[paths]\ndrama_python = "envs/drama/bin/python"\n')
    assert config.load(file).drama_python == config.ROOT / "envs/drama/bin/python"


def test_unknown_keys_are_refused(tmp_path: Path, home: Path) -> None:
    file = write(tmp_path, '[paths]\nzeta = "x"\nmflux = "y"\nace_dir = "z"\n')
    with pytest.raises(SystemExit) as exc:
        config.load(file)
    assert str(exc.value) == (
        "config.toml: unknown key(s) in [paths]: mflux, zeta. Known keys: mflux_bin, "
        "ltx_bin, ltx_model, drama_python, drama_model, ace_dir, qwen_python, qwen_model, ocr_model."
    )


def test_invalid_toml_raises_the_toml_error(tmp_path: Path) -> None:
    # start.py catches this error and reports it
    with pytest.raises(tomllib.TOMLDecodeError):
        config.load(write(tmp_path, "[paths\nmflux_bin = 1\n"))


def test_app_switches_default_without_a_file(tmp_path: Path) -> None:
    assert config.load_app(tmp_path / "missing.toml") == config.App(
        keep_awake=True, notify=True, llm_idle_minutes=10
    )


def test_app_switches_from_the_file(tmp_path: Path) -> None:
    file = write(tmp_path, "[app]\nnotify = false\nllm_idle_minutes = 0\n")
    assert config.load_app(file) == config.App(
        keep_awake=True, notify=False, llm_idle_minutes=0
    )


def test_app_profile_from_the_file(tmp_path: Path) -> None:
    assert config.load_app(tmp_path / "missing.toml").profile == "full"
    file = write(tmp_path, '[app]\nprofile = "chat"\n')
    assert config.load_app(file) == config.App(
        keep_awake=True, notify=True, llm_idle_minutes=10, profile="chat"
    )


@pytest.mark.parametrize(
    "line",
    [
        'keep_awake = "yes"',
        "notify = 1",
        "llm_idle_minutes = 2.5",
        "llm_idle_minutes = -5",
        'profile = "media"',
        "profile = true",
    ],
)
def test_bad_app_values_are_refused(tmp_path: Path, line: str) -> None:
    with pytest.raises(SystemExit, match=r"config.toml: bad value in \[app\]"):
        config.load_app(write(tmp_path, f"[app]\n{line}\n"))


def test_example_config_holds_the_defaults() -> None:
    example = config.ROOT / "config.example.toml"
    assert config.load_app(example) == config.App(**config.APP_DEFAULTS)
    assert tomllib.loads(example.read_text())["app"] == config.APP_DEFAULTS
