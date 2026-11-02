"""config.toml loading: defaults, overrides, "~" and relative paths, unknown keys."""

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
    expected = defaults(home)
    assert config.load(file) == config.Paths(
        mflux_bin=Path("/opt/mflux/bin"),
        ltx_bin=expected.ltx_bin,
        ltx_model=expected.ltx_model,
        drama_python=expected.drama_python,
        drama_model=expected.drama_model,
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
        "ltx_bin, ltx_model, drama_python, drama_model, ace_dir."
    )


def test_invalid_toml_raises_the_toml_error(tmp_path: Path) -> None:
    # start.py catches this error and reports it
    with pytest.raises(tomllib.TOMLDecodeError):
        config.load(write(tmp_path, "[paths\nmflux_bin = 1\n"))
