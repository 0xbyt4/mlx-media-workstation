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
    quiet = (0.05 * np.sin(2 * np.pi * 300 * t) * on).astype(np.float32)
    out, measured = drama_np.level(quiet, sr)
    assert measured < -30
    assert drama_np.loudness(out, sr) == pytest.approx(-16.0, abs=0.1)
    assert out.dtype == np.float32

    spiky = quiet.copy()
    spiky[1000] = 0.5  # one loud click: the gain stops where it reaches -1 dBFS
    out, _ = drama_np.level(spiky, sr)
    assert 20 * np.log10(np.max(np.abs(out))) == pytest.approx(-1.0, abs=0.01)
    assert drama_np.loudness(out, sr) < -16.5

    faint = (quiet * 0.05).astype(np.float32)  # 26 dB under: the gain stops at +20 dB
    out, measured = drama_np.level(faint, sr)
    assert drama_np.loudness(out, sr) == pytest.approx(measured + 20.0, abs=0.1)

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
    monkeypatch.syspath_prepend(
        str(RUNNERS)
    )  # the runner imports subtitles.py from its own folder
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


def test_transcribe_loads_the_model_of_the_job(
    sub_asr: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nemotron: the folder from the job; it names no language, so the note says so."""
    sub_asr["asr"].language = None
    run_transcribe(tmp_path, monkeypatch, subtitles=False, model="/models/nemotron")
    assert sub_asr["loaded"] == ["/models/nemotron"]
    assert (tmp_path / "transcript.txt").read_text().startswith("Language: unknown\n")
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "transcript.txt": "language not named, 0:07"
    }


def test_transcribe_joins_a_short_last_part_to_the_part_before(
    asr: FakeAsr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The splitter pads a last part of 0.5 s to 1 s of silence; it goes with the part before, without the padding."""
    asr.texts = ["Hello.", "How are you?"]
    parts = [([1] * 600, 0.0), ([2] * 345, 60.0), ([3] * 5 + [0] * 5, 94.5)]
    monkeypatch.setattr(
        sys.modules["mlx_audio.stt.models.qwen3_asr.qwen3_asr"],
        "split_audio_into_chunks",
        lambda audio, sr, chunk_duration: parts,
    )
    monkeypatch.setattr(
        sys.modules["numpy"],
        "concatenate",
        lambda xs: [x for p in xs for x in p],
        raising=False,
    )
    seen: list[Any] = []
    generate = asr.generate
    monkeypatch.setattr(
        asr, "generate", lambda part, **kw: seen.append(part) or generate(part, **kw)
    )
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"audio": "s.wav", "language": None, "out": str(tmp_path)})
    )
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(job)])
    load("transcribe").main()
    assert seen[1] == [2] * 345 + [3] * 5
    assert (tmp_path / "transcript.txt").read_text().splitlines()[2:] == [
        "[0:00-1:00] Hello.",
        "[1:00-1:35] How are you?",
    ]


class FakeAligner:
    """Stands in for Qwen3-ForcedAligner: one item for each word without its punctuation, 0.5 s each."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.aligner_processor = SimpleNamespace(
            tokenize_space_lang=lambda text: [
                w for w in ("".join(ch for ch in text if ch.isalnum()),) if w
            ]
        )

    def generate(self, part: Any, text: str, language: str) -> list[Any]:
        self.calls.append((text, language))
        if self.fail:
            raise RuntimeError("out of memory")
        units = [
            u
            for u in (
                self.aligner_processor.tokenize_space_lang(w)[:1] for w in text.split()
            )
            if u
        ]
        return [
            SimpleNamespace(text=u[0], start_time=i * 0.5, end_time=i * 0.5 + 0.4)
            for i, u in enumerate(units)
        ]


@pytest.fixture
def sub_asr(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Qwen3-ASR (and an aligner) for a job with subtitles: two parts, the chunk sizes the runner asked for."""
    model = FakeAsr(["Hello there. How are you?", "Fine - thanks."], "English")
    seen: dict[str, Any] = {"asr": model, "chunks": [], "loaded": []}

    def chunks(audio: Any, sr: int, chunk_duration: float, **kw: Any) -> list[Any]:
        seen["chunks"].append((chunk_duration, kw))
        return [("a", 0.0), ("b", 4.0)]

    def load_model(name: str) -> Any:
        seen["loaded"].append(name)
        return seen.get("aligner") if "Aligner" in name else model

    monkeypatch.syspath_prepend(str(RUNNERS))
    stub(
        monkeypatch,
        {
            "mlx.core": {
                "set_cache_limit": lambda n: None,
                "clear_cache": lambda: None,
            },
            "numpy": {"array": list},
            "mlx_audio.stt.utils": {
                "load_model": load_model,
                "load_audio": lambda path, sr: [0.0] * 70,  # 7 s
            },
            "mlx_audio.stt.models.qwen3_asr.qwen3_asr": {
                "split_audio_into_chunks": chunks
            },
        },
    )
    return seen


def run_transcribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **job: Any
) -> ModuleType:
    spec = tmp_path / "job.json"
    spec.write_text(
        json.dumps(
            {
                "audio": "s.wav",
                "language": None,
                "out": str(tmp_path),
                "subtitles": True,
            }
            | job
        )
    )
    monkeypatch.setattr(sys, "argv", ["transcribe.py", str(spec)])
    runner = load("transcribe")
    # the speech in each part: from 1 s after its start to 3 s after it (numpy is not here)
    monkeypatch.setattr(runner, "speech_span", lambda part, sr: (1.0, 3.0))
    runner.main()
    return runner


def test_transcribe_subtitles_from_short_parts(
    sub_asr: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_transcribe(tmp_path, monkeypatch)
    assert sub_asr["chunks"] == [(6.0, {"search_expand_sec": 2.0})]
    assert sub_asr["loaded"] == ["mlx-community/Qwen3-ASR-0.6B-8bit"]
    # each part's words share 1-3 s after its start by their length; a cue ends after a sentence
    assert (tmp_path / "transcript.srt").read_text() == (
        "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nHow are you?\n\n"
        "3\n00:00:05,000 --> 00:00:07,000\nFine - thanks.\n\n"
    )
    assert (
        (tmp_path / "transcript.vtt")
        .read_text()
        .startswith("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello there.\n\n")
    )
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert (
        notes["transcript.srt"]
        == notes["transcript.vtt"]
        == ("3 subtitles, times from short parts, about +-1 s")
    )
    assert (tmp_path / "transcript.txt").read_text().splitlines()[2:] == [
        "[0:00-0:04] Hello there. How are you?",
        "[0:04-0:07] Fine - thanks.",
    ]


def test_transcribe_subtitles_with_the_aligner(
    sub_asr: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub_asr["aligner"] = FakeAligner()
    run_transcribe(
        tmp_path, monkeypatch, aligner="mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
    )
    # 30 s parts: the aligner gives the times; "-" is not aligned and goes with the word before it
    assert sub_asr["chunks"] == [(30.0, {})]
    assert sub_asr["aligner"].calls == [
        ("Hello there. How are you?", "English"),
        ("Fine - thanks.", "English"),
    ]
    assert (tmp_path / "transcript.srt").read_text() == (
        "1\n00:00:00,000 --> 00:00:01,000\nHello there.\n\n"
        "2\n00:00:01,000 --> 00:00:02,400\nHow are you?\n\n"
        "3\n00:00:04,000 --> 00:00:05,000\nFine - thanks.\n\n"
    )
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes["transcript.srt"] == "3 subtitles, word times from the aligner"


def test_transcribe_keeps_the_spread_times_when_the_aligner_fails(
    sub_asr: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sub_asr["aligner"] = FakeAligner(fail=True)
    run_transcribe(
        tmp_path, monkeypatch, aligner="mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
    )
    assert "aligner: RuntimeError: out of memory" in capsys.readouterr().out
    assert (
        (tmp_path / "transcript.srt")
        .read_text()
        .startswith("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n")
    )


def test_transcribe_without_subtitles_writes_no_srt(
    sub_asr: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_transcribe(tmp_path, monkeypatch, subtitles=False, aligner="x/Aligner")
    assert sub_asr["chunks"] == [(30.0, {})]
    assert sub_asr["loaded"] == ["mlx-community/Qwen3-ASR-0.6B-8bit"]
    assert not (tmp_path / "transcript.srt").exists()
    assert list(json.loads((tmp_path / "notes.json").read_text())) == ["transcript.txt"]


class FakeNemotron:
    """Stands in for Nemotron 3.5 Transcribe: one result per call, as (text, words); the words have times from the
    start of the part and come only with word_timestamps. An exception in the list is raised."""

    sample_rate = 10

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def generate(self, part: Any, **kw: Any) -> Any:
        self.calls.append(kw)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        text, words = r
        segments = words if kw.get("word_timestamps") else None
        return SimpleNamespace(text=text, segments=segments, language=kw["language"])


def word(text: str, start: float, end: float, speaker: int = 0) -> dict[str, Any]:
    return {"text": text, "start": start, "end": end, "speaker": speaker}


@pytest.fixture
def nemo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Nemotron for a job of 40 s in two 30 s parts (at 0 and 30 s); set seen["model"] before the run."""
    seen: dict[str, Any] = {"chunks": []}

    def chunks(audio: Any, sr: int, chunk_duration: float, **kw: Any) -> list[Any]:
        seen["chunks"].append(chunk_duration)
        return [("a", 0.0), ("b", 30.0)]

    monkeypatch.syspath_prepend(str(RUNNERS))
    stub(
        monkeypatch,
        {
            "mlx.core": {
                "set_cache_limit": lambda n: None,
                "clear_cache": lambda: None,
            },
            "numpy": {"array": list},
            "mlx_audio.stt.utils": {
                "load_model": lambda name: seen["model"],
                "load_audio": lambda path, sr: [0.0] * 400,  # 40 s
            },
            "mlx_audio.stt.models.qwen3_asr.qwen3_asr": {
                "split_audio_into_chunks": chunks
            },
        },
    )
    return seen


def test_nemotron_subtitles_from_its_word_times(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nemo["model"] = model = FakeNemotron(
        [
            ("Hello there.", [word("Hello", 1.0, 1.4), word("there.", 1.5, 2.0)]),
            ("Fine.", [word("Fine.", 2.0, 2.5)]),
        ]
    )
    run_transcribe(tmp_path, monkeypatch, model="/models/nemotron")
    assert nemo["chunks"] == [30.0]  # 30 s parts: the words have their own times
    assert {c["task"] for c in model.calls} == {"asr"}
    assert all(c["word_timestamps"] for c in model.calls)
    assert (tmp_path / "transcript.srt").read_text() == (
        "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n\n"
        "2\n00:00:32,000 --> 00:00:33,000\nFine.\n\n"
    )
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes["transcript.srt"] == "2 subtitles, word times from Nemotron"
    assert (tmp_path / "transcript.txt").read_text().splitlines()[2:] == [
        "[0:00-0:30] Hello there.",
        "[0:30-0:40] Fine.",
    ]


def test_nemotron_without_word_times_spreads_the_words(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A part whose words get no time (Thai, say) is transcribed again without them; its words share its time."""
    nemo["model"] = model = FakeNemotron(
        [
            ValueError("Thai word timestamps are not supported"),
            ("Hello there.", []),
            ("Fine.", [word("Fine.", 2, 3)]),
        ]
    )
    run_transcribe(tmp_path, monkeypatch, model="/models/nemotron")
    assert (
        "word times: Thai word timestamps are not supported" in capsys.readouterr().out
    )
    assert "word_timestamps" not in model.calls[1]
    assert (
        (tmp_path / "transcript.srt")
        .read_text()
        .startswith("1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n")
    )  # 1-3 s after the part's start (speech_span), by length


def test_nemotron_speakers_name_each_turn(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    words = [
        word("Did", 0.5, 0.8, 0),
        word("you?", 0.9, 1.4, 0),
        word("Yes.", 2.0, 2.5, 1),
        word("Good.", 40.0, 40.0, 0),
    ]
    nemo["model"] = model = FakeNemotron(
        [("<spk:0> Did you? <spk:1> Yes. <spk:0> Good.", words)]
    )
    run_transcribe(tmp_path, monkeypatch, model="/models/nemotron", speakers=True)
    assert nemo["chunks"] == []  # one pass over the whole audio
    assert model.calls == [
        {"language": None, "max_tokens": 544, "task": "saa", "word_timestamps": True}
    ]
    assert (tmp_path / "transcript.txt").read_text() == (
        "Language: unknown\nSpeakers: 2\n\n"
        "[0:00-0:01] Speaker 1: Did you?\n"
        "[0:02-0:02] Speaker 2: Yes.\n"
        "[0:40-0:40] Speaker 1: Good.\n"
    )
    srt = (tmp_path / "transcript.srt").read_text()
    # a turn's first subtitle names its speaker; it ends where the next turn starts
    assert srt.startswith(
        "1\n00:00:00,500 --> 00:00:01,500\nSpeaker 1: Did you?\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nSpeaker 2: Yes.\n\n"
    )
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes["transcript.txt"] == "language not named, 0:40, 2 speakers"


def test_nemotron_speakers_follow_the_order_of_the_text(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mlx-audio sorts the words by time, and the first word of a later turn can get a time in an earlier turn (the
    times come from the NVFP4 smoke run of a 2-speaker clip). The turns follow the text, and no turn starts before
    the turn before it ends."""
    words = [  # as mlx-audio gives them: sorted by start
        word("tomorrow?", 1.8, 2.35, 0),
        word("I", 2.395, 2.954, 0),
        word("Almost.", 2.954, 2.99, 1),
        word("month.", 6.2, 6.627, 1),
        word("Great,", 6.627, 7.28, 1),
        word("can", 7.28, 7.6, 0),
        word("afternoon.", 8.8, 9.26, 0),
        word("then", 9.92, 10.3, 1),
        word("lunch.", 12.5, 12.99, 1),
    ]
    text = "<spk:0> tomorrow? <spk:1> Almost. month. <spk:0> I can afternoon. <spk:1> Great, then lunch."
    nemo["model"] = FakeNemotron([(text, words)])
    run_transcribe(tmp_path, monkeypatch, model="/models/nemotron", speakers=True)
    assert (tmp_path / "transcript.txt").read_text().splitlines()[3:] == [
        "[0:01-0:02] Speaker 1: tomorrow?",
        "[0:02-0:06] Speaker 2: Almost. month.",
        "[0:06-0:09] Speaker 1: I can afternoon.",
        "[0:09-0:12] Speaker 2: Great, then lunch.",
    ]
    srt = (tmp_path / "transcript.srt").read_text()
    assert "00:00:06,627 --> 00:00:07,627\nSpeaker 1: I can\n" in srt
    assert "00:00:09,260 --> 00:00:10,300\nSpeaker 2: Great, then\n" in srt


def test_nemotron_speakers_keep_the_time_order_when_the_words_do_not_match_the_text(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chinese and Japanese words are characters: their count is not that of the text."""
    words = [word("你", 0.5, 0.6, 0), word("好", 0.6, 0.7, 0), word("是", 2.0, 2.1, 1)]
    nemo["model"] = FakeNemotron([("<spk:0> 你好 <spk:1> 是", words)])
    run_transcribe(
        tmp_path,
        monkeypatch,
        model="/models/nemotron",
        speakers=True,
        language="Chinese",
    )
    assert (tmp_path / "transcript.txt").read_text().splitlines()[3:] == [
        "[0:00-0:00] Speaker 1: 你好",
        "[0:02-0:02] Speaker 2: 是",
    ]


def test_nemotron_speakers_without_word_times_come_from_the_tags(
    nemo: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nemo["model"] = FakeNemotron(
        [
            ValueError("No diarization speaker matched"),
            ("<spk:0> Hi. <spk:1> Hello.", []),
        ]
    )
    run_transcribe(
        tmp_path, monkeypatch, model="/models/nemotron", speakers=True, subtitles=False
    )
    assert (tmp_path / "transcript.txt").read_text().splitlines()[3:] == [
        "[0:00-0:40] Speaker 1: Hi.",
        "[0:00-0:40] Speaker 2: Hello.",
    ]


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
