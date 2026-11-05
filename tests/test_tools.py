"""The tool catalogue, the small helpers in app/tools.py, the ffmpeg/ffprobe wrappers and validate()."""

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import server, tools
from tests.conftest import FakeMedia

FIELD_TYPES = {"text", "textarea", "number", "select", "checkbox", "file", "files"}


# The catalogue


def test_tool_ids_are_unique_and_indexed() -> None:
    ids = [t["id"] for t in tools.TOOLS]
    assert len(ids) == len(set(ids))
    assert tools.BY_ID == {t["id"]: t for t in tools.TOOLS}


def test_groups_follow_the_sidebar_order() -> None:
    groups = list(dict.fromkeys(t["group"] for t in tools.TOOLS))
    assert groups == ["Image", "Voice", "Music", "Video", "Edit", "Document"]
    # each group is one block, so the sidebar does not repeat a heading
    assert [t["group"] for t in tools.TOOLS] == sorted(
        (t["group"] for t in tools.TOOLS), key=groups.index
    )


def test_every_description_gives_a_time() -> None:
    # before the first job a tool has no measured time, and the Create screen points to the description
    for t in tools.TOOLS:
        assert re.search(r"\b(min|s|seconds|real time)\b", t["description"]), t["id"]


def test_every_model_tool_lists_what_it_needs() -> None:
    assert set(tools.REQUIRES) == set(tools.BY_ID) - {
        "edit.mix",
        "edit.concat",
        "edit.subtitle",
    }


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["id"])
def test_fields_are_well_formed(tool: dict[str, Any]) -> None:
    names = [f["name"] for f in tool["fields"]]
    assert len(names) == len(set(names))
    for f in tool["fields"]:
        assert f["type"] in FIELD_TYPES
        if f["type"] == "select":
            assert f["default"] in {o["value"] for o in f["options"]}


def form_value(default: Any) -> str:
    """A default as the page sends it."""
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["id"])
def test_defaults_pass_their_own_field_rules(tool: dict[str, Any]) -> None:
    for f in tool["fields"]:
        if f["type"] in ("file", "files") or f.get("default") in (None, ""):
            continue
        value = form_value(f["default"])
        assert server.check_value(f, value) == value


# Pure helpers


@pytest.mark.parametrize(
    ("seconds", "frames"),
    [(0.01, 9), (1, 25), (2.5, 65), (4, 97), (5, 121), (8, 193), (10, 241)],
)
def test_frames_for_gives_8k_plus_1(seconds: float, frames: int) -> None:
    assert tools.frames_for(seconds) == frames
    assert (frames - 1) % 8 == 0


def test_size() -> None:
    assert tools.size({}) == (768, 1344)
    assert tools.size({"size": "1024x576"}) == (1024, 576)


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (None, 3.0, 3.0),
        ("", 3.0, 3.0),
        (None, None, None),
        ("2.5", None, 2.5),
        (7, None, 7.0),
    ],
)
def test_num(value: Any, default: float | None, expected: float | None) -> None:
    assert tools.num({"x": value}, "x", default) == expected


def test_opts_and_field() -> None:
    assert tools.opts([("a", "A")]) == [{"value": "a", "label": "A"}]
    assert tools.field("n", "N", "number", min=1) == {
        "name": "n",
        "label": "N",
        "type": "number",
        "min": 1,
    }


# ffmpeg and ffprobe wrappers, with subprocess.run replaced


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Makes subprocess.run return the given output; returns the list of commands it got."""

    def answer(stdout: str = "", stderr: str = "", code: int = 0) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if kw.get("check") and code:
                raise subprocess.CalledProcessError(code, cmd, stdout, stderr)
            return subprocess.CompletedProcess(cmd, code, stdout, stderr)

        monkeypatch.setattr(tools.subprocess, "run", fake)
        return calls

    return answer


@pytest.mark.parametrize(
    ("stdout", "expected"), [("1\n", True), ("", False), ("\n", False)]
)
def test_has_audio(run: Callable[..., Any], stdout: str, expected: bool) -> None:
    calls = run(stdout)
    assert tools.has_audio(Path("clip.mp4")) is expected
    assert calls[0][0] == "ffprobe"
    assert calls[0][-1] == "clip.mp4"


@pytest.mark.parametrize(
    "probe", ["has_audio", "media_seconds", "video_size", "frame_count", "image_format"]
)
def test_probes_open_only_local_media_files(
    run: Callable[..., Any], probe: str
) -> None:
    calls = run("64,48,rgb24\n")
    try:
        getattr(tools, probe)(Path("in.mp4"))
    except ValueError:
        pass  # the canned answer does not suit every probe; the command is what counts
    cmd = calls[0]
    # the file protocol and the media demuxers only, before the input: no concat list, playlist or network
    assert cmd[3:7] == tools.GUARD
    assert cmd[3:5] == ["-protocol_whitelist", "file"]
    assert "concat" not in cmd[6] and "hls" not in cmd[6]
    assert {"mov", "wav", "png_pipe", "jpeg_pipe", "gif"} <= set(cmd[6].split(","))
    assert cmd[-1] == "in.mp4"


def test_a_probe_that_fails_says_why_in_plain_words(run: Callable[..., Any]) -> None:
    run(
        stderr="[in#0 @ 0x7f8a] in.mp4: Invalid data found when processing input\n",
        code=1,
    )
    with pytest.raises(ValueError) as exc:
        tools.media_seconds(Path("in.mp4"))
    assert str(exc.value) == (
        "Could not read the input file: in.mp4: Invalid data found when processing input"
    )


def test_a_probe_without_a_value(run: Callable[..., Any]) -> None:
    run("N/A\n")
    with pytest.raises(
        ValueError, match="^Could not read the length of the input file.$"
    ):
        tools.media_seconds(Path("in.wav"))
    run("\n")
    with pytest.raises(ValueError, match="^The input file has no video stream.$"):
        tools.video_size(Path("in.wav"))


def test_probes_and_prep_have_time_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: list[float] = []

    def slow(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        limits.append(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(tools.subprocess, "run", slow)
    with pytest.raises(ValueError) as exc:
        tools.media_seconds(Path("in.wav"))
    assert str(exc.value) == (
        "ffprobe took more than 30 s on the input file. Use a shorter or smaller file."
    )
    with pytest.raises(ValueError, match="^ffmpeg took more than 600 s on the input"):
        tools.prep(["ffmpeg", "-i", "in.mp4", "out.mp4"])
    assert limits == [tools.READ_SECONDS, tools.PREP_SECONDS]


def test_a_missing_ffmpeg_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(tools.subprocess, "run", missing)
    with pytest.raises(ValueError) as exc:
        tools.has_audio(Path("in.wav"))
    assert str(exc.value) == (
        "ffprobe was not found. Install it with Homebrew: brew install ffmpeg."
    )


def test_media_seconds(run: Callable[..., Any]) -> None:
    run("12.480000\n")
    assert tools.media_seconds(Path("a.wav")) == 12.48


@pytest.mark.parametrize("stdout", ["704,1280\n", "704,1280,\n"])
def test_video_size(run: Callable[..., Any], stdout: str) -> None:
    run(stdout)
    assert tools.video_size(Path("v.mp4")) == (704, 1280)


def test_frame_count_reads_the_header(run: Callable[..., Any]) -> None:
    calls = run("121\n")
    assert tools.frame_count(Path("v.mp4")) == 121
    assert len(calls) == 1  # no decode of the whole clip
    assert calls[0][calls[0].index("-show_entries") + 1] == "stream=nb_frames"
    assert "-count_frames" not in calls[0]


def test_frame_count_counts_when_the_header_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    def fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kw["timeout"]))
        out = "N/A\n" if len(calls) == 1 else "97\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(tools.subprocess, "run", fake)
    assert tools.frame_count(Path("v.mkv")) == 97
    (header, _), (count, limit) = calls
    assert "stream=nb_frames" in header
    assert "-count_frames" in count and "stream=nb_read_frames" in count
    assert limit == tools.PREP_SECONDS  # a decode of the whole clip


# ffmpeg's error lines, as ffmpeg 9.0.2 writes them with -v error

PRORES_COPY = """\
[mp4 @ 0x7cf0c0c500] Could not find tag for codec prores in stream #0, codec not currently supported in container
[out#0/mp4 @ 0x7cf1024180] Could not write header (incorrect codec parameters ?): Invalid argument
"""
GIF_TO_PNG = """\
[image2 @ 0x7938c0c280] Cannot write more than one file with the same name. Are you missing the -update option or a sequence pattern?
[vost#0:0/png @ 0x7938c5c000] Error submitting a packet to the muxer: Invalid argument
    Last message repeated 1 times
[out#0/image2 @ 0x7939024180] Error muxing a packet
[out#0/image2 @ 0x7939024180] Task finished with error code: -22 (Invalid argument)
[out#0/image2 @ 0x7939024180] Terminating thread with return code -22 (Invalid argument)
"""
HEIC_GRID = """\
[vost#0:0/png @ 0x74c2c6c000] Filtergraph 'scale=64:48' was specified for a stream fed from a complex filtergraph. Simple and complex filtering cannot be used together for the same stream.
Error opening output file fixed_big.png.
Error opening output files: Invalid argument
"""
GIF_CAUSE = "Cannot write more than one file with the same name. Are you missing the -update option or a sequence pattern?"
HEIC_CAUSE = (
    "Filtergraph 'scale=64:48' was specified for a stream fed from a complex filtergraph. Simple and complex "
    "filtering cannot be used together for the same stream."
)
MISSING = """\
[in#0 @ 0x774d018000] Error opening input: No such file or directory
Error opening input file nothere.mp4.
Error opening input files: No such file or directory
"""
NOT_WHITELISTED = """\
[in#0 @ 0x74e4c10000] Format not on whitelist 'mp3,wav'
[in#0 @ 0x74e4c0c000] Error opening input: Invalid argument
Error opening input file list.mp3.
Error opening input files: Invalid argument
"""
NO_STREAM = """\
[fc#0 @ 0x7c82c08000] Stream specifier ':a' in filtergraph description [0:a]anull[x] matches no streams.
Error binding filtergraph inputs/outputs: Invalid argument
"""


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        (
            PRORES_COPY,
            "Could not find tag for codec prores in stream #0, codec not currently supported in container",
        ),
        (
            GIF_TO_PNG,
            GIF_CAUSE,
        ),
        (
            HEIC_GRID,
            HEIC_CAUSE,
        ),
        (MISSING, "Error opening input: No such file or directory"),
        (
            NOT_WHITELISTED,
            "this file type is not supported (use a common image, audio or video file)",
        ),
        (
            NO_STREAM,
            "Stream specifier ':a' in filtergraph description [0:a]anull[x] matches no streams.",
        ),
        ("one\ntwo\n", "two"),
        ("", ""),
    ],
)
def test_ffmpeg_error_names_the_cause(stderr: str, cause: str) -> None:
    assert tools.ffmpeg_error(stderr) == cause


def test_ffmpeg_error_strips_every_prefix_and_is_short() -> None:
    line = "[out#0/wav @ 0x1a2b3c] [aist#0:0/pcm_s16le @ 0xdeadbeef] " + "x" * 400
    assert tools.ffmpeg_error(line) == "x" * 300


EBUR128 = """\
[Parsed_ebur128_0 @ 0x1] t: 0.1  TARGET:-23 LUFS  M:-120.7 S:-120.7  I: -70.0 LUFS  LRA: 0.0 LU
[Parsed_ebur128_0 @ 0x1] t: 0.2  TARGET:-23 LUFS  M: -25.1 S: -25.1  I: -25.1 LUFS  LRA: 0.0 LU
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         {value} LUFS
    Threshold: -33.4 LUFS
"""


@pytest.mark.parametrize(
    ("value", "expected"), [("-23.4", -23.4), ("-70.0", None), ("-91.2", None)]
)
def test_loudness_reads_the_summary(
    run: Callable[..., Any], value: str, expected: float | None
) -> None:
    run(stderr=EBUR128.format(value=value))
    assert tools.loudness(Path("m.mp3")) == expected


def test_loudness_without_audio(run: Callable[..., Any]) -> None:
    run(stderr="Output file does not contain any stream\n", code=1)
    assert tools.loudness(Path("silent.mp4")) is None


def test_prep_passes(run: Callable[..., Any]) -> None:
    calls = run()
    tools.prep(["ffmpeg", "-i", "x"])
    assert calls == [["ffmpeg", "-i", "x"]]


@pytest.mark.parametrize(
    ("stderr", "code", "why"),
    [
        (
            GIF_TO_PNG,
            234,
            GIF_CAUSE,
        ),
        ("", 2, "exit code 2"),
    ],
)
def test_prep_reports_ffmpeg_errors(
    run: Callable[..., Any], stderr: str, code: int, why: str
) -> None:
    run(stderr=stderr, code=code)
    with pytest.raises(ValueError) as exc:
        tools.prep(["ffmpeg"])
    assert str(exc.value) == f"Could not prepare the input file: {why}"


# The extend source check


def test_extend_source_whole_clip(media: FakeMedia) -> None:
    media.seconds["v.mp4"] = 4.5
    assert tools.check_extend_source(Path("v.mp4"), None) == 4.5


def test_extend_source_up_to(media: FakeMedia) -> None:
    media.seconds["v.mp4"] = 4.5
    assert tools.check_extend_source(Path("v.mp4"), 2.0) == 2.0


@pytest.mark.parametrize(
    ("size", "seconds", "end_at", "message"),
    [
        ((700, 1280), 5.0, None, "The video is 700x1280; LTX extends only sizes"),
        ((704, 1290), 5.0, None, "The video is 704x1290; LTX extends only sizes"),
        (
            (704, 1280),
            5.0,
            6.0,
            "'Use the clip up to' is 6.0 s, but the clip is only 5.0 s long.",
        ),
        ((704, 1280), 1.0, None, "The clip must be at least 1.1 s long (it is 1.0 s)."),
    ],
)
def test_extend_source_refusals(
    media: FakeMedia,
    size: tuple[int, int],
    seconds: float,
    end_at: float | None,
    message: str,
) -> None:
    media.size = size
    media.seconds["v.mp4"] = seconds
    with pytest.raises(ValueError) as exc:
        tools.check_extend_source(Path("v.mp4"), end_at)
    assert str(exc.value).startswith(message)


def test_extend_source_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path: Path) -> tuple[int, int]:
        raise subprocess.CalledProcessError(1, ["ffprobe"])

    monkeypatch.setattr(tools, "video_size", fail)
    with pytest.raises(ValueError, match="The file could not be read as a video."):
        tools.check_extend_source(Path("notes.txt"), None)


# validate(): checks run when a job is submitted


def test_validate_names_missing_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    there, missing = tmp_path / "python", tmp_path / "weights"
    there.touch()
    monkeypatch.setattr(tools, "REQUIRES", {"voice.drama3": [there, missing]})
    with pytest.raises(ValueError) as exc:
        tools.validate("voice.drama3", {}, {})
    assert str(exc.value) == (
        f"Not installed: {missing}. Open Models to install it, or set its folder in "
        "Settings > Model paths."
    )


def test_validate_names_the_missing_installs_of_a_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    there, weights = tmp_path / "python", tmp_path / "nemotron"
    there.touch()
    monkeypatch.setattr(tools, "REQUIRES", {"voice.transcribe": [there]})
    monkeypatch.setattr(
        tools,
        "CHOICE_REQUIRES",
        {("voice.transcribe", "model", "nemotron"): [there, weights]},
    )
    tools.validate("voice.transcribe", {"model": "qwen3-asr"}, {})
    with pytest.raises(ValueError, match=f"^Not installed: {weights}\\."):
        tools.validate("voice.transcribe", {"model": "nemotron"}, {})
    # the choice needs its own paths instead of the tool's: here not the default model's environment
    monkeypatch.setattr(tools, "REQUIRES", {"voice.transcribe": [tmp_path / "drama"]})
    weights.mkdir()
    tools.validate("voice.transcribe", {"model": "nemotron"}, {})
    with pytest.raises(ValueError, match="^Not installed: .*drama"):
        tools.validate("voice.transcribe", {"model": "qwen3-asr"}, {})


def test_speakers_need_nemotron_and_at_most_5_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools, "REQUIRES", {})
    monkeypatch.setattr(tools, "CHOICE_REQUIRES", {})
    monkeypatch.setattr(tools, "has_audio", lambda path: True)
    length = {"s": 300.0}
    monkeypatch.setattr(tools, "media_seconds", lambda path: length["s"])
    files = {"audio": [Path("talk.wav")]}
    with pytest.raises(ValueError, match="^Speakers: only with the model Nemotron"):
        tools.validate(
            "voice.transcribe", {"model": "qwen3-asr", "speakers": "true"}, files
        )
    tools.validate("voice.transcribe", {"model": "nemotron", "speakers": "true"}, files)
    length["s"] = 301.0
    with pytest.raises(
        ValueError, match=r"^Speakers: at most 5 min of audio \(it is 5\.0 min\)\."
    ):
        tools.validate(
            "voice.transcribe", {"model": "nemotron", "speakers": "true"}, files
        )
    tools.validate(
        "voice.transcribe", {"model": "nemotron", "speakers": "false"}, files
    )


def test_validate_passes_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(tools, "REQUIRES", {"image.klein": [tmp_path / "bin"]})
    tools.validate("image.klein", {"prompt": "x"}, {})


@pytest.mark.parametrize("text", [None, "", "   "])
def test_validate_drama_reference_needs_a_transcript(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    monkeypatch.setattr(tools, "REQUIRES", {})
    files = {"ref1": [Path("in_ref1_0.wav")]}
    with pytest.raises(ValueError, match="Add the transcript of voice reference 1."):
        tools.validate("voice.drama3", {"ref1_text": text}, files)
    tools.validate("voice.drama3", {"ref1_text": "Hello."}, files)


@pytest.mark.parametrize(
    ("seconds", "ok"), [(1.4, False), (1.5, True), (20.0, True), (20.1, False)]
)
def test_validate_a2v_voice_length(
    monkeypatch: pytest.MonkeyPatch, media: FakeMedia, seconds: float, ok: bool
) -> None:
    monkeypatch.setattr(tools, "REQUIRES", {})
    media.seconds["in_audio_0.wav"] = seconds
    files = {"audio": [Path("in_audio_0.wav")]}
    if ok:
        tools.validate("video.a2v", {}, files)
    else:
        with pytest.raises(ValueError, match="The voice audio must be 1.5-20.0 s long"):
            tools.validate("video.a2v", {}, files)


def test_validate_a2v_unreadable_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path: Path) -> float:
        raise ValueError("could not convert string to float: ''")

    monkeypatch.setattr(tools, "REQUIRES", {})
    monkeypatch.setattr(tools, "media_seconds", fail)
    with pytest.raises(ValueError, match="The voice file could not be read as audio."):
        tools.validate("video.a2v", {}, {"audio": [Path("in_audio_0.bin")]})


def test_validate_extend_checks_the_source(
    monkeypatch: pytest.MonkeyPatch, media: FakeMedia
) -> None:
    monkeypatch.setattr(tools, "REQUIRES", {})
    media.seconds["in_video_0.mp4"] = 2.0
    files = {"video": [Path("in_video_0.mp4")]}
    tools.validate("video.extend", {"end_at": "1.5"}, files)
    with pytest.raises(ValueError, match="'Use the clip up to' is 3.0 s"):
        tools.validate("video.extend", {"end_at": "3"}, files)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("in_video_0.mp4", "The video file has no picture, or it could not be read."),
        ("in_music_0.mp3", "The music file has no sound, or it could not be read."),
    ],
)
def test_validate_mix_needs_a_picture_and_a_sound(
    monkeypatch: pytest.MonkeyPatch, media: FakeMedia, name: str, message: str
) -> None:
    monkeypatch.setattr(tools, "REQUIRES", {})
    files = {"video": [Path("in_video_0.mp4")], "music": [Path("in_music_0.mp3")]}
    tools.validate("edit.mix", {}, files)
    media.codecs[name] = None
    with pytest.raises(ValueError) as exc:
        tools.validate("edit.mix", {}, files)
    assert str(exc.value) == message


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)
def test_stream_codec_skips_a_cover_picture(tmp_path: Path) -> None:
    song, text = tmp_path / "song.mp3", tmp_path / "notes.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", "-f", "lavfi", "-i",
         "testsrc=size=64x48:duration=1", "-map", "0", "-map", "1", "-frames:v", "1", "-c:v", "mjpeg",
         "-disposition:v", "attached_pic", str(song)],
        check=True,
    )  # fmt: skip
    text.write_text("not a video")
    assert tools.stream_codec(song, "a") == "mp3"
    assert (
        tools.stream_codec(song, "V") is None
    )  # an audio file with cover art is not a video
    assert tools.stream_codec(text, "V") is None
