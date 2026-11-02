"""Pure parts of the runner scripts in app/runners/. The model libraries they import (mlx, ltx-2-mlx, mlx-audio,
numpy, scipy) are replaced with small stand-ins, so the scripts load without any model environment."""

import contextlib
import importlib.util
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RUNNERS = Path(__file__).resolve().parents[1] / "app" / "runners"


def stub(monkeypatch: pytest.MonkeyPatch, modules: dict[str, dict[str, Any]]) -> None:
    """Puts stand-in modules (and their parent packages) in sys.modules for this test."""
    made: dict[str, ModuleType] = {}
    for dotted, attrs in modules.items():
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            name = ".".join(parts[:i])
            if name not in made:
                made[name] = ModuleType(name)
                made[name].__path__ = []  # a package, so submodules can be imported
                monkeypatch.setitem(sys.modules, name, made[name])
                if i > 1:
                    setattr(made[".".join(parts[: i - 1])], parts[i - 1], made[name])
        for key, value in attrs.items():
            setattr(made[dotted], key, value)


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"runner_{name}", RUNNERS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# LTX extend wrapper


@pytest.fixture
def ltx(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stand-ins for mlx and ltx-2-mlx; returns the list of events they record."""
    events: list[Any] = []

    @contextlib.contextmanager
    def decode_cache_limit() -> Iterator[None]:
        events.append("cache off")
        try:
            yield
        finally:
            events.append("cache on")

    def encode_video_tensor(x: Any) -> Any:
        events.append(("video", x))
        return f"latent {x}"

    def encode_source_audio_latent(x: Any) -> Any:
        events.append(("audio", x))
        raise RuntimeError("bad audio")

    def main() -> int:
        events.append(("main", sys.argv[0]))
        return 0

    stub(
        monkeypatch,
        {
            "mlx.core": {"set_cache_limit": lambda n: events.append(("limit", n))},
            "ltx_core_mlx.model.video_vae.video_vae": {
                "decode_cache_limit": decode_cache_limit
            },
            "ltx_pipelines_mlx.extend_distilled": {
                "encode_video_tensor": encode_video_tensor,
                "encode_source_audio_latent": encode_source_audio_latent,
            },
            "ltx_pipelines_mlx.cli": {"main": main},
        },
    )
    return events


def test_ltx_extend_caps_the_cache_on_import(ltx: list[Any]) -> None:
    module = load("ltx_extend")
    assert module.CACHE_LIMIT == 2_000_000_000
    assert ltx == [("limit", 2_000_000_000)]


def test_ltx_extend_encodes_without_the_cache(ltx: list[Any]) -> None:
    load("ltx_extend")
    encode = sys.modules["ltx_pipelines_mlx.extend_distilled"].encode_video_tensor
    assert encode.__name__ == "encode_video_tensor"
    ltx.clear()
    assert encode("clip") == "latent clip"
    assert ltx == ["cache off", ("video", "clip"), "cache on"]


def test_ltx_extend_restores_the_cache_after_an_error(ltx: list[Any]) -> None:
    load("ltx_extend")
    encode = sys.modules[
        "ltx_pipelines_mlx.extend_distilled"
    ].encode_source_audio_latent
    ltx.clear()
    with pytest.raises(RuntimeError, match="bad audio"):
        encode("sound")
    assert ltx == ["cache off", ("audio", "sound"), "cache on"]


def test_ltx_extend_runs_the_cli_as_ltx_2_mlx(
    ltx: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", [str(RUNNERS / "ltx_extend.py"), "extend"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(RUNNERS / "ltx_extend.py"), run_name="__main__")
    assert exc.value.code == 0
    assert ("main", "ltx-2-mlx") in ltx


# Fish Drama 3 speech check


@pytest.fixture
def drama(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    stub(
        monkeypatch,
        {
            "mlx.core": {},
            "numpy": {"ndarray": object},  # only used in annotations here
            "mlx_audio.stt.utils": {"load_model": None},
            "mlx_audio.tts.models.fish_drama3.chunking": {"split_text": None},
            "mlx_audio.tts.utils": {"load_model": None},
            "scipy.io": {"wavfile": None},
        },
    )
    return load("drama3")


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("<|speaker:0|> Hello [laughs] there.", ["hello", "there"]),
        (
            "<|speaker:1|>Wait, <em>what</em>? [nervous, quiet] No.",
            ["wait", "what", "no"],
        ),
        ("Don't STOP, 2 more times!", ["don't", "stop", "2", "more", "times"]),
        ("[sighs]", []),
    ],
)
def test_drama_spoken_words(drama: ModuleType, text: str, words: list[str]) -> None:
    assert drama.words(drama.spoken(text)) == words


@pytest.mark.parametrize(
    ("expected", "heard", "share"),
    [
        ("one two three four", "one two three four", 0.0),
        ("one two three four", "One, three!", 0.5),
        ("go go go stop", "stop", 0.75),
        ("hello", "", 1.0),
        ("", "anything", 0.0),
    ],
)
def test_drama_missing_share(
    drama: ModuleType, expected: str, heard: str, share: float
) -> None:
    assert drama.missing_share(expected, heard) == share
