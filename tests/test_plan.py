"""tools.plan(): the exact commands, environment and output names for each tool."""

import json
from pathlib import Path
from typing import Any

import pytest

from app import tools
from tests.conftest import MODELS, FakeMedia

OFFLINE = {"HF_HUB_OFFLINE": "1"}
LTX_ENV = {"HF_HUB_OFFLINE": "1", "LTX2_COMPUTE_DTYPE": "float16"}
LTX = str(MODELS / "ltx" / "bin" / "ltx-2-mlx")
LTX_MODEL = str(MODELS / "ltx-2.5")
MFLUX = MODELS / "mflux" / "bin"


def params(tool: str, **values: Any) -> dict[str, Any]:
    """The parameters as the server stores them: each form field's default, then the given values."""
    fields = tools.BY_ID[tool]["fields"]
    base = {
        f["name"]: f.get("default")
        for f in fields
        if f["type"] not in ("file", "files")
    }
    return base | values


def fit(src: Path, dst: Path, w: int, h: int) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        "-vf",
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}",
        str(dst),
    ]


def test_unknown_tool(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown tool 'image.nope'"):
        tools.plan("image.nope", {}, {}, tmp_path)


# Images


def test_moss_defaults(tmp_path: Path) -> None:
    plan = tools.plan(
        "image.moss", params("image.moss", prompt="a lighthouse"), {}, tmp_path
    )
    assert plan.commands == [
        [
            str(MFLUX / "mflux-generate-moss-image"),
            "-q",
            "8",
            "--width",
            "768",
            "--height",
            "1344",
            "--steps",
            "50",
            "--guidance",
            "4.0",
            "--negative-prompt=" + tools.MOSS_NEGATIVE,
            "--prompt=a lighthouse",
            "--output",
            str(tmp_path / "image.png"),
        ]
    ]
    assert plan.env == OFFLINE
    assert plan.cwd is None


def test_moss_options_and_seed(tmp_path: Path) -> None:
    p = params(
        "image.moss",
        prompt="a lighthouse",
        size="1088x1920",
        seed="42",
        steps="60",
        guidance="5.5",
        negative="blurry",
    )
    [cmd] = tools.plan("image.moss", p, {}, tmp_path).commands
    assert cmd[3:13] == [
        "--width",
        "1088",
        "--height",
        "1920",
        "--steps",
        "60",
        "--guidance",
        "5.5",
        "--negative-prompt=blurry",
        "--prompt=a lighthouse",
    ]
    assert cmd[13:] == ["--seed", "42", "--output", str(tmp_path / "image.png")]


@pytest.mark.parametrize(
    ("seed", "expected"),
    [("0", ["--seed", "0"]), ("4294967295", ["--seed", "4294967295"]), (None, [])],
)
def test_klein_seed(tmp_path: Path, seed: str | None, expected: list[str]) -> None:
    p = params("image.klein", prompt='a sign that reads "PUSH"', seed=seed)
    plan = tools.plan("image.klein", p, {}, tmp_path)
    assert plan.commands == [
        [
            str(MFLUX / "mflux-generate-flux3"),
            "-q",
            "8",
            "--width",
            "768",
            "--height",
            "1344",
            "--steps",
            "16",
            '--prompt=a sign that reads "PUSH"',
            *expected,
            "--output",
            str(tmp_path / "image.png"),
        ]
    ]
    assert plan.env == OFFLINE


def test_klein_edit_passes_references_in_upload_order(tmp_path: Path) -> None:
    refs = [tmp_path / "in_references_0.png", tmp_path / "in_references_1.jpg"]
    p = params(
        "image.klein_edit", prompt="<ref_image_0> in <ref_image_1>", size="1024x1024"
    )
    plan = tools.plan("image.klein_edit", p, {"references": refs}, tmp_path)
    assert plan.commands == [
        [
            str(MFLUX / "mflux-generate-flux3-edit"),
            "-q",
            "8",
            "--width",
            "1024",
            "--height",
            "1024",
            "--image-paths",
            str(refs[0]),
            str(refs[1]),
            "--prompt=<ref_image_0> in <ref_image_1>",
            "--output",
            str(tmp_path / "image.png"),
        ]
    ]


def test_klein_edit_needs_a_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Add at least one reference image."):
        tools.plan(
            "image.klein_edit", params("image.klein_edit", prompt="x"), {}, tmp_path
        )


# Voice and music: the runner gets a JSON job file


def test_drama3_job_file(tmp_path: Path) -> None:
    ref1 = tmp_path / "in_ref1_0.wav"
    p = params("voice.drama3", text="<|speaker:1|> Hello there.", takes="3", seed="7")
    p["ref1_text"] = "  Hello, I am the reference.  "
    plan = tools.plan("voice.drama3", p, {"ref1": [ref1]}, tmp_path)
    spec = tmp_path / "voice_job.json"
    assert plan.commands == [
        [
            str(MODELS / "drama/bin/python"),
            str(tools.APP / "runners/drama3.py"),
            str(spec),
        ]
    ]
    assert plan.env == OFFLINE
    assert json.loads(spec.read_text()) == {
        "text": "<|speaker:1|> Hello there.",
        "takes": 3,
        "seed": 7.0,
        "model": str(MODELS / "drama-3"),
        "temperature": 1.0,
        "top_p": 0.9,
        "top_k": 30,
        "refs": [
            {"audio": str(ref1), "text": "Hello, I am the reference.", "speaker": 1}
        ],
        "out": str(tmp_path),
    }


def test_drama3_two_references_and_no_seed(tmp_path: Path) -> None:
    refs = {"ref0": [tmp_path / "in_ref0_0.wav"], "ref1": [tmp_path / "in_ref1_0.flac"]}
    p = params(
        "voice.drama3",
        text="Hi.",
        ref0_text="zero",
        ref1_text="one",
        temperature="0.8",
        top_p="0.95",
        top_k="50",
    )
    tools.plan("voice.drama3", p, refs, tmp_path)
    spec = json.loads((tmp_path / "voice_job.json").read_text())
    assert spec["seed"] is None
    assert (spec["temperature"], spec["top_p"], spec["top_k"], spec["takes"]) == (
        0.8,
        0.95,
        50,
        2,
    )
    assert spec["refs"] == [
        {"audio": str(refs["ref0"][0]), "text": "zero", "speaker": 0},
        {"audio": str(refs["ref1"][0]), "text": "one", "speaker": 1},
    ]


def test_ace_defaults(tmp_path: Path) -> None:
    plan = tools.plan(
        "music.ace", params("music.ace", caption="dark synth"), {}, tmp_path
    )
    spec = tmp_path / "music_job.json"
    assert plan.commands == [
        [
            str(MODELS / "ace/.venv/bin/python"),
            str(tools.APP / "runners/ace.py"),
            str(spec),
        ]
    ]
    assert plan.env == OFFLINE
    assert plan.cwd == MODELS / "ace"
    assert json.loads(spec.read_text()) == {
        "caption": "dark synth",
        "instrumental": True,
        "lyrics": "",
        "duration": 30.0,
        "bpm": None,
        "key": "",
        "takes": 2,
        "seed": None,
        "out": str(tmp_path),
    }


def test_ace_with_lyrics(tmp_path: Path) -> None:
    p = params(
        "music.ace",
        caption="folk ballad",
        instrumental="false",
        lyrics="[verse]\nhello",
        duration="45",
        bpm="120",
        key="D minor",
        takes="1",
        seed="5",
    )
    tools.plan("music.ace", p, {}, tmp_path)
    spec = json.loads((tmp_path / "music_job.json").read_text())
    assert spec | {"out": ""} == {
        "caption": "folk ballad",
        "instrumental": False,
        "lyrics": "[verse]\nhello",
        "duration": 45.0,
        "bpm": 120.0,
        "key": "D minor",
        "takes": 1,
        "seed": 5.0,
        "out": "",
    }


# Video


def test_t2v_defaults(tmp_path: Path) -> None:
    plan = tools.plan(
        "video.t2v", params("video.t2v", prompt="A fox runs."), {}, tmp_path
    )
    assert plan.commands == [
        [
            LTX,
            "generate",
            "--distilled",
            "--model",
            LTX_MODEL,
            "--prompt=A fox runs.",
            "-W",
            "704",
            "-H",
            "1280",
            "-f",
            "121",
            "--frame-rate",
            "24",
            "-o",
            str(tmp_path / "video.mp4"),
        ]
    ]
    assert plan.env == LTX_ENV


def test_t2v_no_audio_hq_decoder_and_seed(tmp_path: Path) -> None:
    p = params(
        "video.t2v",
        prompt="A fox runs.",
        size="1024x576",
        seconds="2.5",
        audio="false",
        hq_decoder="true",
        seed="123",
    )
    [cmd] = tools.plan("video.t2v", p, {}, tmp_path).commands
    assert cmd[6:] == [
        "-W",
        "1024",
        "-H",
        "576",
        "-f",
        "65",
        "--frame-rate",
        "24",
        "--seed",
        "123",
        "-o",
        str(tmp_path / "video.mp4"),
        "--no-audio",
        "--video-decoder",
        "diffusion",
    ]


def test_i2v_fits_the_start_image(tmp_path: Path, media: FakeMedia) -> None:
    img = tmp_path / "in_image_0.jpg"
    p = params("video.i2v", prompt="She turns.", size="576x1024", seconds="4")
    plan = tools.plan("video.i2v", p, {"image": [img]}, tmp_path)
    start = tmp_path / "start.png"
    assert media.prepped == [fit(img, start, 576, 1024)]
    [cmd] = plan.commands
    assert cmd[:2] == [LTX, "generate"]
    assert cmd[6:12] == ["-W", "576", "-H", "1024", "-f", "97"]
    assert cmd[-4:] == ["-o", str(tmp_path / "video.mp4"), "--image", str(start)]
    assert plan.env == LTX_ENV


def test_i2v_needs_an_image(tmp_path: Path, media: FakeMedia) -> None:
    with pytest.raises(ValueError, match="Add a start image."):
        tools.plan("video.i2v", params("video.i2v", prompt="x"), {}, tmp_path)
    assert media.prepped == []


def test_a2v_pads_the_voice_and_fits_the_image(
    tmp_path: Path, media: FakeMedia
) -> None:
    img, aud = tmp_path / "in_image_0.png", tmp_path / "in_audio_0.wav"
    media.seconds[aud.name] = 3.0
    p = params("video.a2v", prompt="  A woman speaks.  ", seed="11")
    plan = tools.plan("video.a2v", p, {"image": [img], "audio": [aud]}, tmp_path)
    padded, person = tmp_path / "voice_padded.wav", tmp_path / "person.png"
    # 3 s of voice + 0.75 s = 90 frames -> 97 (8k+1); padded to 97 / 24 + 0.3 s
    assert media.prepped == [
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(aud),
            "-af",
            "adelay=250:all=1,apad=whole_dur=4.342",
            "-ar",
            "48000",
            str(padded),
        ],
        fit(img, person, 576, 1024),
    ]
    assert plan.commands == [
        [
            LTX,
            "a2v",
            "--distilled",
            "--model",
            LTX_MODEL,
            "--audio",
            str(padded),
            "-i",
            str(person),
            "--prompt=A woman speaks. " + tools.A2V_STYLE,
            "-W",
            "576",
            "-H",
            "1024",
            "-f",
            "97",
            "--frame-rate",
            "24",
            "--seed",
            "11",
            "-o",
            str(tmp_path / "video.mp4"),
        ]
    ]
    assert plan.env == LTX_ENV


def test_a2v_custom_style(tmp_path: Path, media: FakeMedia) -> None:
    files = {
        "image": [tmp_path / "in_image_0.png"],
        "audio": [tmp_path / "in_audio_0.wav"],
    }
    media.seconds["in_audio_0.wav"] = 1.5
    p = params("video.a2v", prompt="A man speaks.", style=" Close-up. ")
    [cmd] = tools.plan("video.a2v", p, files, tmp_path).commands
    assert "--prompt=A man speaks. Close-up." in cmd


@pytest.mark.parametrize("seconds", [1.4, 20.5])
def test_a2v_refuses_voice_length(
    tmp_path: Path, media: FakeMedia, seconds: float
) -> None:
    files = {
        "image": [tmp_path / "in_image_0.png"],
        "audio": [tmp_path / "in_audio_0.wav"],
    }
    media.seconds["in_audio_0.wav"] = seconds
    with pytest.raises(ValueError, match=r"must be 1.5-20.0 s long \(it is"):
        tools.plan("video.a2v", params("video.a2v", prompt="x"), files, tmp_path)
    assert media.prepped == []


@pytest.mark.parametrize("missing", ["image", "audio"])
def test_a2v_needs_both_files(tmp_path: Path, missing: str) -> None:
    files = {
        "image": [tmp_path / "in_image_0.png"],
        "audio": [tmp_path / "in_audio_0.wav"],
    }
    del files[missing]
    with pytest.raises(ValueError, match="Add a person image and a voice audio file."):
        tools.plan("video.a2v", params("video.a2v", prompt="x"), files, tmp_path)


# Extend


def encode(src: Path, dst: Path, used: str, *, audio: bool) -> list[str]:
    silence = [] if audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        *silence,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0" if audio else "1:a:0",
        "-vf",
        "fps=24,setsar=1",
        "-af",
        "aresample=48000,apad",
        "-t",
        used,
        *tools.VIDEO_ENC,
        str(dst),
    ]


def test_extend_defaults(tmp_path: Path, media: FakeMedia) -> None:
    src = tmp_path / "in_video_0.mp4"
    media.seconds[src.name] = 5.0
    p = params("video.extend", prompt="She keeps walking.")
    plan = tools.plan("video.extend", p, {"video": [src]}, tmp_path)
    source, tail, raw = (
        tmp_path / n for n in ("source.mp4", "tail.mp4", "extended_raw.mp4")
    )
    # 121 frames: the model gets the last 49 (from frame 72)
    assert media.prepped == [
        encode(src, source, "5.000", audio=True),
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            "trim=start_frame=72,setpts=PTS-STARTPTS",
            "-af",
            "atrim=start=3.000000,asetpts=PTS-STARTPTS",
            *tools.VIDEO_ENC,
            str(tail),
        ],
    ]
    join = (
        "[0:a]apad,atrim=end=5.041667[ca];"
        "[1:v]trim=start_frame=49,setpts=PTS-STARTPTS,setsar=1[nv];"
        "[1:a]atrim=start=2.041667,asetpts=PTS-STARTPTS,aresample=48000[na];"
        "[0:v][ca][nv][na]concat=n=2:v=1:a=1[v][a]"
    )
    assert plan.commands == [
        [
            str(MODELS / "ltx/bin/python"),
            str(tools.APP / "runners/ltx_extend.py"),
            "extend",
            "--distilled",
            "--model",
            LTX_MODEL,
            "--prompt=She keeps walking.",
            "--video",
            str(tail),
            "--extend-frames",
            "15",
            "-o",
            str(raw),
        ],
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-i",
            str(raw),
            "-filter_complex",
            join,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(tmp_path / "video.mp4"),
        ],
    ]
    assert plan.env == LTX_ENV
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes == {"video.mp4": "5.0 s of the clip + 5.0 s new"}


def test_extend_silent_source_end_at_and_seed(tmp_path: Path, media: FakeMedia) -> None:
    src = tmp_path / "in_video_0.mov"
    media.seconds[src.name] = 6.0
    media.audio[src.name] = False
    media.frames = 72
    p = params("video.extend", prompt="x", end_at="3", seconds="8", seed="9")
    plan = tools.plan("video.extend", p, {"video": [src]}, tmp_path)
    assert media.prepped[0] == encode(
        src, tmp_path / "source.mp4", "3.000", audio=False
    )
    assert "trim=start_frame=23,setpts=PTS-STARTPTS" in media.prepped[1]
    extend = plan.commands[0]
    assert extend[-7:] == [
        str(tmp_path / "tail.mp4"),
        "--extend-frames",
        "24",
        "--seed",
        "9",
        "-o",
        str(tmp_path / "extended_raw.mp4"),
    ]
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes == {"video.mp4": "3.0 s of the clip + 8.0 s new"}


@pytest.mark.parametrize(
    ("seconds", "latent"), [("1", "3"), ("1.5", "5"), ("5", "15"), ("8", "24")]
)
def test_extend_new_frames(
    tmp_path: Path, media: FakeMedia, seconds: str, latent: str
) -> None:
    media.seconds["in_video_0.mp4"] = 5.0
    p = params("video.extend", prompt="x", seconds=seconds)
    files = {"video": [tmp_path / "in_video_0.mp4"]}
    extend = tools.plan("video.extend", p, files, tmp_path).commands[0]
    assert extend[extend.index("--extend-frames") + 1] == latent


@pytest.mark.parametrize(
    ("frames", "carry", "start"),
    [(25, 25, 0), (30, 25, 5), (48, 41, 7), (49, 49, 0), (121, 49, 72)],
)
def test_extend_carries_8k_plus_1_frames(
    tmp_path: Path, media: FakeMedia, frames: int, carry: int, start: int
) -> None:
    media.seconds["in_video_0.mp4"] = 5.0
    media.frames = frames
    files = {"video": [tmp_path / "in_video_0.mp4"]}
    plan = tools.plan(
        "video.extend", params("video.extend", prompt="x"), files, tmp_path
    )
    assert f"trim=start_frame={start},setpts=PTS-STARTPTS" in media.prepped[1]
    join = plan.commands[1][plan.commands[1].index("-filter_complex") + 1]
    assert f"[1:v]trim=start_frame={carry}," in join


def test_extend_refuses_too_few_frames(tmp_path: Path, media: FakeMedia) -> None:
    media.seconds["in_video_0.mp4"] = 5.0
    media.frames = 24
    files = {"video": [tmp_path / "in_video_0.mp4"]}
    with pytest.raises(
        ValueError, match="only 24 video frames; extending needs at least 25"
    ):
        tools.plan("video.extend", params("video.extend", prompt="x"), files, tmp_path)


def test_extend_needs_a_video(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Add a video."):
        tools.plan("video.extend", params("video.extend", prompt="x"), {}, tmp_path)


# Mix


@pytest.fixture
def mix_files(tmp_path: Path, media: FakeMedia) -> dict[str, list[Path]]:
    video, music = tmp_path / "in_video_0.mp4", tmp_path / "in_music_0.mp3"
    media.seconds[video.name] = 10.0
    media.lufs[video.name] = -20.0
    media.lufs[music.name] = -10.0
    return {"video": [video], "music": [music]}


def mix_command(files: dict[str, list[Path]], graph: str, out: Path) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(files["video"][0]),
        "-stream_loop",
        "-1",
        "-i",
        str(files["music"][0]),
        "-filter_complex",
        graph,
        "-map",
        "0:v",
        "-map",
        "[out]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-t",
        "10.000",
        "-movflags",
        "+faststart",
        str(out / "mixed.mp4"),
    ]


VOICE = "[0:a]aresample=48000,asplit=2[voice][key]"
FINAL = ";[mix]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[out]"


def bed(start: str, gain: str, fade_in: str, fade_out_at: str, fade_out: str) -> str:
    return (
        f"[1:a]atrim=start={start},asetpts=PTS-STARTPTS,atrim=0:10.000,aresample=48000,"
        f"volume={gain}dB,afade=t=in:d={fade_in},afade=t=out:st={fade_out_at}:d={fade_out}"
    )


def test_mix_ducks_music_under_the_voice(
    tmp_path: Path, mix_files: dict[str, list[Path]]
) -> None:
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    # music 14 dB under the -20 LUFS voice: -34 - (-10) = -24 dB
    graph = (
        f"{VOICE};{bed('0.000', '-24.00', '0.50', '8.500', '1.50')}[bed];"
        "[bed][key]sidechaincompress=threshold=0.02:ratio=4:attack=80:release=400[ducked];"
        "[voice][ducked]amix=inputs=2:normalize=0:duration=first[mix]" + FINAL
    )
    assert plan.commands == [mix_command(mix_files, graph, tmp_path)]
    assert plan.env == {}
    assert plan.cwd is None
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "mixed.mp4": "voice -20.0 LUFS, music set 14 dB under it, ducked under speech; "
        "final mix -14 LUFS"
    }


def test_mix_without_ducking(tmp_path: Path, mix_files: dict[str, list[Path]]) -> None:
    p = params(
        "edit.mix",
        duck="false",
        under="20",
        music_start="12.5",
        fade_in="0",
        fade_out="0",
    )
    plan = tools.plan("edit.mix", p, mix_files, tmp_path)
    graph = (
        f"{VOICE};{bed('12.500', '-30.00', '0.00', '10.000', '0.00')}[bed];[key]anullsink;"
        "[voice][bed]amix=inputs=2:normalize=0:duration=first[mix]" + FINAL
    )
    assert plan.commands == [mix_command(mix_files, graph, tmp_path)]
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "mixed.mp4": "voice -20.0 LUFS, music set 20 dB under it; final mix -14 LUFS"
    }


def test_mix_video_without_sound(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.audio["in_video_0.mp4"] = False
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    # no voice: the music goes to -16 LUFS, -16 - (-10) = -6 dB
    graph = bed("0.000", "-6.00", "0.50", "8.500", "1.50") + "[mix]" + FINAL
    assert plan.commands == [mix_command(mix_files, graph, tmp_path)]
    assert json.loads((tmp_path / "notes.json").read_text()) == {
        "mixed.mp4": "no voice in the video: music at -16 LUFS; final mix -14 LUFS"
    }


def test_mix_refuses_silent_music(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.lufs["in_music_0.mp3"] = None
    with pytest.raises(ValueError, match="The music file has no audible sound."):
        tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)


@pytest.mark.parametrize("missing", ["video", "music"])
def test_mix_needs_both_files(
    tmp_path: Path, mix_files: dict[str, list[Path]], missing: str
) -> None:
    del mix_files[missing]
    with pytest.raises(ValueError, match="Add a video and a music file."):
        tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)


def test_transcribe_makes_16k_mono_speech_and_a_job_file(
    tmp_path: Path, media: FakeMedia
) -> None:
    clip = tmp_path / "in_audio_0.mp4"
    plan = tools.plan(
        "voice.transcribe", params("voice.transcribe"), {"audio": [clip]}, tmp_path
    )
    speech, spec = tmp_path / "speech.wav", tmp_path / "transcribe_job.json"
    assert media.prepped == [
        ["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file", "-format_whitelist", tools.SPEECH_DEMUXERS,
         "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", str(speech)]
    ]  # fmt: skip
    assert plan.commands == [
        [
            str(MODELS / "drama/bin/python"),
            str(tools.APP / "runners/transcribe.py"),
            str(spec),
        ]
    ]
    assert plan.env == OFFLINE
    assert json.loads(spec.read_text()) == {
        "audio": str(speech),
        "language": None,
        "out": str(tmp_path),
    }
    tools.plan(
        "voice.transcribe",
        params("voice.transcribe", language="Turkish"),
        {"audio": [clip]},
        tmp_path,
    )
    assert json.loads(spec.read_text())["language"] == "Turkish"
    with pytest.raises(ValueError, match="Add an audio or video file"):
        tools.plan("voice.transcribe", params("voice.transcribe"), {}, tmp_path)


def test_the_transcription_input_is_read_only_as_one_media_file(
    tmp_path: Path, media: FakeMedia
) -> None:
    """The ffmpeg step before a transcription takes the file protocol and the media demuxers only, so a playlist or
    a concat list named talk.mp4 cannot make it read other files."""
    src = tmp_path / "talk.mp4"
    src.write_bytes(b"x")
    tools.plan("voice.transcribe", {}, {"audio": [src]}, tmp_path)
    [cmd] = media.prepped
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    allowed = cmd[cmd.index("-format_whitelist") + 1].split(",")
    assert "mov" in allowed and "wav" in allowed
    assert not {"hls", "concat", "image2", "tee"} & set(allowed)
    assert cmd.index("-format_whitelist") < cmd.index("-i")
