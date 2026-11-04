"""Pure parts of the runner scripts in app/runners/. The model libraries they import (mlx, ltx-2-mlx, mlx-audio,
numpy, scipy) are replaced with small stand-ins, so the scripts load without any model environment."""

import contextlib
import importlib.util
import json
import runpy
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
            "scipy.signal": {"lfilter": None},
        },
    )
    return load("drama3")


@pytest.fixture
def drama_np(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The Drama 3 runner with the real numpy and scipy (they live in the Drama 3 environment, not the app's)."""
    pytest.importorskip("numpy")
    pytest.importorskip("scipy.signal")
    stub(
        monkeypatch,
        {
            "mlx.core": {},
            "mlx_audio.stt.utils": {"load_model": None},
            "mlx_audio.tts.models.fish_drama3.chunking": {"split_text": None},
            "mlx_audio.tts.utils": {"load_model": None},
        },
    )
    return load("drama3")


def test_drama_loudness_of_a_sine(drama_np: ModuleType) -> None:
    import numpy as np

    sr = 48000
    t = np.arange(5 * sr) / sr
    # BS.1770: a full-scale 1 kHz sine in one channel is -3.01 LUFS, so 0.1 of full scale is -23.01
    assert drama_np.loudness(0.1 * np.sin(2 * np.pi * 1000 * t), sr) == pytest.approx(
        -23.01, abs=0.1
    )
    assert drama_np.loudness(np.zeros(sr), sr) is None


def test_drama_level_raises_quiet_speech(drama_np: ModuleType) -> None:
    import numpy as np

    sr = 44100
    t = np.arange(6 * sr) / sr
    on = (t % 1.0) < 0.6  # word-like bursts with gaps
    quiet = (0.02 * np.sin(2 * np.pi * 300 * t) * on).astype(np.float32)
    out, measured = drama_np.level(quiet, sr)
    assert measured < -30
    assert drama_np.loudness(out, sr) == pytest.approx(-16.0, abs=0.1)
    assert out.dtype == np.float32

    spiky = quiet.copy()
    spiky[1000] = 0.5  # one loud click: the gain stops where it reaches -1 dBFS
    out, _ = drama_np.level(spiky, sr)
    assert 20 * np.log10(np.max(np.abs(out))) == pytest.approx(-1.0, abs=0.01)
    assert drama_np.loudness(out, sr) < -16.5

    silent = np.zeros(sr, np.float32)
    out, measured = drama_np.level(silent, sr)
    assert measured is None and not out.any()


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


# Transcribe


class FakeAsr:
    """Stands in for Qwen3-ASR: one result per part, the language as the model names it."""

    sample_rate = 10

    def __init__(self, texts: list[str], language: str) -> None:
        self.texts, self.language = list(texts), language
        self.calls: list[dict[str, Any]] = []

    def generate(self, part: Any, **kw: Any) -> Any:
        self.calls.append(kw)
        language = kw["language"] or self.language
        return type("R", (), {"text": self.texts.pop(0), "language": [language]})


@pytest.fixture
def asr(monkeypatch: pytest.MonkeyPatch) -> FakeAsr:
    model = FakeAsr(["Hello  there.", "", "How are\nyou?"], "English")
    stub(
        monkeypatch,
        {
            "mlx.core": {
                "set_cache_limit": lambda n: None,
                "clear_cache": lambda: None,
            },
            "numpy": {"array": list},
            "mlx_audio.stt.utils": {
                "load_model": lambda name: model,
                "load_audio": lambda path, sr: (
                    [0.0] * 950
                ),  # 95 s at 10 samples a second
            },
            "mlx_audio.stt.models.qwen3_asr.qwen3_asr": {
                "split_audio_into_chunks": lambda audio, sr, chunk_duration: [
                    ("a", 0.0),
                    ("b", 29.5),
                    ("c", 61.0),
                ]
            },
        },
    )
    return model


def test_transcribe_writes_timed_lines_and_the_language(
    asr: FakeAsr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"audio": "speech.wav", "language": None, "out": str(tmp_path)})
    )
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(job)])
    load("transcribe").main()
    assert (tmp_path / "transcript.txt").read_text() == (
        "Language: English\n\n[0:00-0:29] Hello there.\n[1:01-1:35] How are you?\n"
    )
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "transcript.txt": "English, 1:35"
    }
    assert capsys.readouterr().out.splitlines() == [
        "part 1/3: 0:00-0:29",
        "part 2/3: 0:29-1:01",
        "part 3/3: 1:01-1:35",
    ]
    # a token budget for each part that its length cannot run out of
    assert [c["max_tokens"] for c in asr.calls] == [418, 442, 472]


def test_transcribe_with_a_given_language(
    asr: FakeAsr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"audio": "s.wav", "language": "German", "out": str(tmp_path)})
    )
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(job)])
    load("transcribe").main()
    assert {c["language"] for c in asr.calls} == {"German"}
    assert (tmp_path / "transcript.txt").read_text().startswith("Language: German\n")


# Mix level


def test_mix_level_adds_the_measured_loudness_to_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mix = tmp_path / "mixed.mp4"
    (tmp_path / "notes.json").write_text(
        json.dumps({"mixed.mp4": "no sound in the video: music at -16 LUFS"})
    )
    runner = load("mix_level")
    summary = (
        "  Integrated loudness:\n    I:         -14.2 LUFS\n    Threshold: -24.2 LUFS\n"
    )
    monkeypatch.setattr(
        runner.subprocess, "run", lambda cmd, **kw: SimpleNamespace(stderr=summary)
    )
    monkeypatch.setattr(sys, "argv", ["mix_level.py", str(mix)])
    runner.main()
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "mixed.mp4": "no sound in the video: music at -16 LUFS; final mix -14.2 LUFS"
    }
    assert capsys.readouterr().out == "final mix -14.2 LUFS\n"


def test_mix_level_without_a_measurement() -> None:
    assert (
        load("mix_level").measured("Error opening input file")
        == "final mix not measured"
    )
