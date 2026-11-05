"""tools.plan(): the exact commands, environment and output names for each tool."""

import json
import re
import shutil
import subprocess
import sys
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
GUARD = [
    "-protocol_whitelist",
    "file",
    "-format_whitelist",
    tools.SPEECH_DEMUXERS
    + ",image2,png_pipe,jpeg_pipe,webp_pipe,gif,apng,bmp_pipe,tiff_pipe",
]

# an image with alpha laid on white: a copy of the frame turned white, so the size is the upright frame's
WHITE = (
    "[0:v]format=rgba,split[fg][c];[c]format=rgb24,drawbox=c=white:t=fill[bg];"
    "[bg][fg]overlay=format=auto,format=rgb24"
)


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
        *GUARD,
        "-i",
        str(src),
        "-vf",
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}",
        "-frames:v",
        "1",
        "-update",
        "1",
        str(dst),
    ]


def tag(video: Path) -> list[str]:
    """The step after LTX: a stream copy that tags the colours and puts the index first."""
    return [sys.executable, str(tools.APP / "runners/tag_video.py"), str(video)]


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


def test_klein_edit_passes_references_in_upload_order(
    tmp_path: Path, media: FakeMedia
) -> None:
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


@pytest.mark.parametrize("pix_fmt", ["rgba", "pal8", "ya8"])
def test_klein_edit_lays_a_reference_with_alpha_on_white(
    tmp_path: Path, media: FakeMedia, pix_fmt: str
) -> None:
    """mflux reads the references itself and drops alpha: a see-through reference is laid on white first, into an
    in_* file that is never listed as an output; an opaque one is passed as it is."""
    refs = [tmp_path / "in_references_0.png", tmp_path / "in_references_1.jpg"]
    media.pix_fmt[refs[0].name] = pix_fmt
    p = params("image.klein_edit", prompt="x", size="1024x1024")
    [cmd] = tools.plan("image.klein_edit", p, {"references": refs}, tmp_path).commands
    white = tmp_path / "in_references_0_white.png"
    at = cmd.index("--image-paths")
    assert cmd[at + 1 : at + 3] == [str(white), str(refs[1])]
    assert media.prepped == [
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *GUARD,
            "-i",
            str(refs[0]),
            "-filter_complex",
            f"{WHITE},null",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(white),
        ]
    ]


def test_klein_edit_needs_a_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Add at least one reference image."):
        tools.plan(
            "image.klein_edit", params("image.klein_edit", prompt="x"), {}, tmp_path
        )


# Voice and music: the runner gets a JSON job file


def test_drama3_job_file(tmp_path: Path, media: FakeMedia) -> None:
    ref1 = tmp_path / "in_ref1_0.ogg"
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
            {
                "audio": str(tmp_path / "in_ref1_voice.wav"),
                "text": "Hello, I am the reference.",
                "speaker": 1,
            }
        ],
        "out": str(tmp_path),
    }
    # the reference goes through the input whitelist into a WAV before the runner reads it
    assert media.prepped == [
        ["ffmpeg", "-v", "error", "-y", *GUARD, "-i", str(ref1), "-vn", "-c:a", "pcm_s24le",
         str(tmp_path / "in_ref1_voice.wav")]
    ]  # fmt: skip


def test_drama3_two_references_and_no_seed(tmp_path: Path, media: FakeMedia) -> None:
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
        {"audio": str(tmp_path / "in_ref0_voice.wav"), "text": "zero", "speaker": 0},
        {"audio": str(tmp_path / "in_ref1_voice.wav"), "text": "one", "speaker": 1},
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
        ],
        tag(tmp_path / "video.mp4"),
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
    [cmd, _] = tools.plan("video.t2v", p, {}, tmp_path).commands
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
    [cmd, finish] = plan.commands
    assert finish == tag(tmp_path / "video.mp4")
    assert cmd[:2] == [LTX, "generate"]
    assert cmd[6:12] == ["-W", "576", "-H", "1024", "-f", "97"]
    assert cmd[-4:] == ["-o", str(tmp_path / "video.mp4"), "--image", str(start)]
    assert plan.env == LTX_ENV


@pytest.mark.parametrize("pix_fmt", ["rgba", "pal8", "ya8", "yuva420p", "rgba64be"])
def test_i2v_lays_an_image_with_alpha_on_white(
    tmp_path: Path, media: FakeMedia, pix_fmt: str
) -> None:
    img = tmp_path / "in_image_0.png"
    media.pix_fmt[img.name] = pix_fmt
    p = params("video.i2v", prompt="It turns.", size="576x1024")
    tools.plan("video.i2v", p, {"image": [img]}, tmp_path)
    [cmd] = media.prepped
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == (
        f"{WHITE},scale=576:1024:force_original_aspect_ratio=increase:flags=lanczos,crop=576:1024"
    )
    assert "-vf" not in cmd
    assert cmd[-5:] == ["-frames:v", "1", "-update", "1", str(tmp_path / "start.png")]


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
            *GUARD,
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
        ],
        tag(tmp_path / "video.mp4"),
    ]
    assert plan.env == LTX_ENV


def test_a2v_custom_style(tmp_path: Path, media: FakeMedia) -> None:
    files = {
        "image": [tmp_path / "in_image_0.png"],
        "audio": [tmp_path / "in_audio_0.wav"],
    }
    media.seconds["in_audio_0.wav"] = 1.5
    p = params("video.a2v", prompt="A man speaks.", style=" Close-up. ")
    [cmd, _] = tools.plan("video.a2v", p, files, tmp_path).commands
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
        *GUARD,
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
            *GUARD,
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
            *GUARD,
            "-i",
            str(source),
            *GUARD,
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
            "-bsf:v",
            tools.LTX_COLOURS,
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


COPY = ["-c:v", "copy"]


def mix_command(
    files: dict[str, list[Path]], graph: str, out: Path, picture: list[str] = COPY
) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        *GUARD,
        "-i",
        str(files["video"][0]),
        *GUARD,
        "-stream_loop",
        "-1",
        "-i",
        str(files["music"][0]),
        "-filter_complex",
        graph,
        "-map",
        "0:V:0",
        "-map",
        "[out]",
        *picture,
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


def mix_level(out: Path) -> list[str]:
    return [
        sys.executable,
        str(tools.APP / "runners" / "mix_level.py"),
        str(out / "mixed.mp4"),
    ]


VOICE = "[0:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur=10.000,asplit=2[voice][key]"
# the second pass uses the first pass's measurement (FakeMedia.loudnorm_stats)
FINAL = (
    ";[mix]loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-17.50:measured_TP=-6.20:measured_LRA=4.10:"
    "measured_thresh=-27.80:offset=0.30:linear=true,aresample=48000[out]"
)
UNDUCKED = (
    "[bed];[key]anullsink;[voice][bed]amix=inputs=2:normalize=0:duration=first[mix]"
)


def bed(
    start: str, gain: str, fades: str = ",afade=t=in:d=0.50,afade=t=out:st=8.500:d=1.50"
) -> str:
    return (
        f"[1:a]atrim=start={start},asetpts=PTS-STARTPTS,atrim=0:10.000,aresample=48000,"
        f"aformat=channel_layouts=stereo,volume={gain}dB{fades}"
    )


def mix_graph(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def notes(out: Path) -> str:
    return json.loads((out / "notes.json").read_text())["mixed.mp4"]


def test_mix_ducks_music_under_the_voice(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    # music 14 dB under the -20 LUFS voice: -34 - (-10) = -24 dB; the key is scaled by 4 dB to a -16 LUFS voice
    graph = (
        f"{VOICE};{bed('0.000', '-24.00')}[bed];"
        "[bed][key]sidechaincompress=threshold=0.05:ratio=2:attack=80:release=400:level_sc=1.5849[ducked];"
        "[voice][ducked]amix=inputs=2:normalize=0:duration=first[mix]"
    )
    assert plan.commands == [
        mix_command(mix_files, graph + FINAL, tmp_path),
        mix_level(tmp_path),
    ]
    assert plan.env == {}
    assert plan.cwd is None
    # the music is measured only on the part the mix uses
    assert ("in_music_0.mp3", 0.0, 10.0) in media.measured
    # the first pass: the same graph, measured with print_format=json and nothing written
    video, music = mix_files["video"][0], mix_files["music"][0]
    assert media.first_pass == [
        ["ffmpeg", "-hide_banner", "-nostats", *GUARD, "-i", str(video), *GUARD, "-stream_loop", "-1", "-i", str(music),
         "-filter_complex", graph + ";[mix]loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json[out]",
         "-map", "[out]", "-t", "10.000", "-f", "null", "-"]
    ]  # fmt: skip
    # the last step adds the measured final loudness
    assert notes(tmp_path) == "speech found (-20.0 LUFS): music 14 dB under it, ducked"


@pytest.mark.parametrize(
    ("voice", "level_sc"),
    [(-4.0, "0.2512"), (-35.0, "8.9125"), (-39.0, "14.1254")],
)
def test_mix_duck_key_follows_the_voice_level(
    tmp_path: Path,
    media: FakeMedia,
    mix_files: dict[str, list[Path]],
    voice: float,
    level_sc: str,
) -> None:
    media.lufs["in_video_0.mp4"] = voice
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    assert f":level_sc={level_sc}[ducked]" in mix_graph(plan.commands[0])


def test_mix_without_ducking_or_fades(
    tmp_path: Path, mix_files: dict[str, list[Path]]
) -> None:
    p = params(
        "edit.mix",
        duck="false",
        under="20",
        music_start="12.5",
        fade_in="0",
        fade_out="0",
    )
    plan = tools.plan("edit.mix", p, mix_files, tmp_path)
    # a fade of 0 is left out: afade with d=0 would fade over 44100 samples
    graph = f"{VOICE};{bed('12.500', '-30.00', '')}{UNDUCKED}"
    assert plan.commands == [
        mix_command(mix_files, graph + FINAL, tmp_path),
        mix_level(tmp_path),
    ]
    assert (
        notes(tmp_path) == "speech found (-20.0 LUFS): music 20 dB under it, not ducked"
    )


@pytest.mark.parametrize(
    ("lra", "dips", "speech"),
    [
        (2.3, 0.03, False),  # rain on a real LTX clip
        (6.9, 0.40, True),  # a Drama 3 narration
        (5.0, 0.0, True),
        (2.0, 0.15, True),
        (4.9, 0.14, False),
    ],
)
def test_mix_finds_speech_by_its_pauses_or_range(
    tmp_path: Path,
    media: FakeMedia,
    mix_files: dict[str, list[Path]],
    lra: float,
    dips: float,
    speech: bool,
) -> None:
    media.levels["in_video_0.mp4"] = (lra, dips)
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    assert ("sidechaincompress" in mix_graph(plan.commands[0])) is speech


def test_mix_steady_sound_is_not_ducked(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.levels["in_video_0.mp4"] = (2.3, 0.03)
    plan = tools.plan("edit.mix", params("edit.mix", under="20"), mix_files, tmp_path)
    # 6 dB under the -20 LUFS sound, whatever "under" says: -26 - (-10) = -16 dB
    graph = f"{VOICE};{bed('0.000', '-16.00')}{UNDUCKED}"
    assert plan.commands[0] == mix_command(mix_files, graph + FINAL, tmp_path)
    assert (
        notes(tmp_path)
        == "no speech, steady sound (-20.0 LUFS): music 6 dB under the sound"
    )


def test_mix_near_silent_sound_stays_in_the_mix(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.lufs["in_video_0.mp4"] = -63.0
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    # room tone is not a voice: the music goes to -16 LUFS (-6 dB), no ducking, and the sound is kept
    graph = f"{VOICE};{bed('0.000', '-6.00')}{UNDUCKED}"
    assert plan.commands[0] == mix_command(mix_files, graph + FINAL, tmp_path)
    assert (
        notes(tmp_path)
        == "no speech, near-silent sound (-63.0 LUFS): music at -16 LUFS"
    )


def test_mix_video_without_sound(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.audio["in_video_0.mp4"] = False
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    # no sound: the music goes to -16 LUFS, -16 - (-10) = -6 dB
    graph = bed("0.000", "-6.00") + "[mix]"
    assert plan.commands == [
        mix_command(mix_files, graph + FINAL, tmp_path),
        mix_level(tmp_path),
    ]
    assert notes(tmp_path) == "no sound in the video: music at -16 LUFS"


def test_mix_raises_quiet_music_by_at_most_20_db(
    tmp_path: Path, media: FakeMedia, mix_files: dict[str, list[Path]]
) -> None:
    media.lufs["in_music_0.mp3"] = -60.0
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    assert "volume=20.00dB," in mix_graph(
        plan.commands[0]
    )  # not the +26 dB that -34 LUFS needs
    assert notes(tmp_path) == (
        "speech found (-20.0 LUFS): music 14 dB under it, ducked; the music is very quiet: raised by only 20 dB"
    )


@pytest.mark.parametrize(
    ("codec", "picture"),
    [
        ("h264", COPY),
        ("vp9", COPY),
        ("hevc", ["-c:v", "copy", "-tag:v", "hvc1"]),
        ("prores", ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]),
    ],
)
def test_mix_copies_the_picture_only_when_mp4_can_hold_it(
    tmp_path: Path,
    media: FakeMedia,
    mix_files: dict[str, list[Path]],
    codec: str,
    picture: list[str],
) -> None:
    media.codecs["in_video_0.mp4"] = codec
    plan = tools.plan("edit.mix", params("edit.mix"), mix_files, tmp_path)
    assert (
        plan.commands[0][
            plan.commands[0].index("[out]") + 1 : plan.commands[0].index("-c:a")
        ]
        == picture
    )
    assert notes(tmp_path).endswith("; picture (prores) encoded again as H.264") is (
        codec == "prores"
    )


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


# Mix with the real ffmpeg, on a few seconds of synthetic input

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)
# bursts of 0.8 s with 0.8 s pauses, silent first: on and off like speech
BURSTS = "volume='if(gte(mod(t,1.6),0.8),1,0)':eval=frame"


def make_video(
    path: Path, seconds: float, sound: str, sound_seconds: float, af: str = "anull"
) -> None:
    """A small H.264 video with a mono sound track that can be shorter than the picture."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x48:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", f"{sound}:d={sound_seconds}:r=48000", "-af", af, "-ac", "1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )  # fmt: skip


def rms(path: Path, start: float, seconds: float) -> float:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-t", str(seconds), "-i", str(path),
         "-af", "astats=measure_perchannel=none", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr  # fmt: skip
    return float(re.findall(r"RMS level dB: (-?[\d.]+)", err)[-1])


def run_mix(out: Path, files: dict[str, list[Path]], **values: Any) -> dict[str, Any]:
    plan = tools.plan("edit.mix", params("edit.mix", **values), files, out)
    for cmd in plan.commands:
        subprocess.run(cmd, check=True, capture_output=True)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=channels,duration",
         "-of", "json", str(out / "mixed.mp4")],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(probe)["streams"][0]


@needs_ffmpeg
def test_mix_real_ffmpeg_stereo_full_length_at_14_lufs_without_fades(
    tmp_path: Path,
) -> None:
    video, music = tmp_path / "in_video_0.mp4", tmp_path / "in_music_0.wav"
    # 4 s of picture, but only 2 s of mono sound (silent for its first 0.8 s); a tone, not noise, so the peaks let
    # loudnorm keep one steady gain up to -14 LUFS
    make_video(video, 4, "sine=frequency=220", 2, BURSTS)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=1.5",
         "-ac", "2", str(music)],
        check=True,
    )  # fmt: skip
    files = {"video": [video], "music": [music]}
    tools.validate("edit.mix", {}, files)
    stream = run_mix(tmp_path, files, fade_in="0", fade_out="0")
    mixed = tmp_path / "mixed.mp4"
    assert stream["channels"] == 2
    assert float(stream["duration"]) == pytest.approx(4.0, abs=0.05)
    final = tools.loudness(mixed)
    assert final == pytest.approx(-14.0, abs=1.0)
    assert notes(tmp_path).endswith(f"; final mix {final:.1f} LUFS")
    # no fade: the music is as loud in its first 0.1 s as before the first burst, and in the last 0.1 s as after
    # the video's sound ended
    assert rms(mixed, 0.0, 0.1) == pytest.approx(rms(mixed, 0.2, 0.5), abs=1.5)
    assert rms(mixed, 3.85, 0.1) == pytest.approx(rms(mixed, 2.5, 1.0), abs=1.5)


@needs_ffmpeg
@pytest.mark.parametrize(("af", "ducked"), [("anull", False), (BURSTS, True)])
def test_mix_real_ffmpeg_ducks_only_under_speech_like_sound(
    tmp_path: Path, af: str, ducked: bool
) -> None:
    video, music = tmp_path / "in_video_0.mp4", tmp_path / "in_music_0.wav"
    make_video(video, 4, "anoisesrc=c=pink:a=0.1", 4, af)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=4",
         str(music)],
        check=True,
    )  # fmt: skip
    plan = tools.plan(
        "edit.mix", params("edit.mix"), {"video": [video], "music": [music]}, tmp_path
    )
    assert ("sidechaincompress" in mix_graph(plan.commands[0])) is ducked
    assert notes(tmp_path).startswith(
        "speech found" if ducked else "no speech, steady sound"
    )


def test_silent_windows_count_as_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg can print "nan" or "-inf" for a window of exact zeros (TTS gaps): such a window is a pause."""
    lines = [
        f"[Parsed_ebur128_0 @ 0x1] t: {t / 10:.6f}   TARGET:-23 LUFS    M: {m} S: -22.0     I: -22.0 LUFS"
        for t, m in enumerate(
            ["-22.0", "-22.0", "-22.0", "-22.0", "nan", " -inf", "nan", "-22.0"] * 3, 1
        )
    ]
    log = (
        "\n".join(lines)
        + "\n  Integrated loudness:\n    I:         -22.0 LUFS\n    LRA:         2.0 LU\n"
    )
    monkeypatch.setattr(tools, "ebur128", lambda path, *a: log)
    lufs, lra, dips = tools.sound_levels(Path("voice.wav"))
    assert (lufs, lra) == (-22.0, 2.0)
    assert dips == pytest.approx(
        9 / 21
    )  # the windows from 0.4 s on: 9 of 21 are silent


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
        "subtitles": False,
        "aligner": None,
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


def test_transcribe_with_nemotron_runs_in_the_llm_environment(
    tmp_path: Path, media: FakeMedia
) -> None:
    clip, spec = tmp_path / "in_audio_0.mp4", tmp_path / "transcribe_job.json"
    plan = tools.plan(
        "voice.transcribe",
        params("voice.transcribe", model="nemotron"),
        {"audio": [clip]},
        tmp_path,
    )
    assert plan.commands == [
        [
            str(MODELS / "llm/bin/python"),
            str(tools.APP / "runners/transcribe.py"),
            str(spec),
        ]
    ]
    assert plan.env == OFFLINE
    assert json.loads(spec.read_text())["model"] == str(MODELS / "nemotron")
    assert json.loads(spec.read_text())["speakers"] is False
    tools.plan(
        "voice.transcribe",
        params("voice.transcribe", model="nemotron", speakers="true"),
        {"audio": [clip]},
        tmp_path,
    )
    assert json.loads(spec.read_text())["speakers"] is True
    # the default model: the Drama 3 environment and no model in the job file
    tools.plan(
        "voice.transcribe", params("voice.transcribe"), {"audio": [clip]}, tmp_path
    )
    assert "model" not in json.loads(spec.read_text())


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


def test_transcribe_subtitles_and_the_aligner_in_the_job_file(
    tmp_path: Path, media: FakeMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subtitles go to the runner; the aligner only when one is named and in the Hugging Face cache (the job runs
    offline)."""
    clip, spec = tmp_path / "in_audio_0.mp4", tmp_path / "transcribe_job.json"

    def job(**values: Any) -> dict[str, Any]:
        tools.plan(
            "voice.transcribe",
            params("voice.transcribe", **values),
            {"audio": [clip]},
            tmp_path,
        )
        return json.loads(spec.read_text())

    assert (job()["subtitles"], job()["aligner"]) == (False, None)
    assert (job(subtitles="true")["subtitles"], job()["aligner"]) == (True, None)
    name = "mlx-community/Qwen3-ForcedAligner-0.6B-8bit"
    monkeypatch.setattr(tools, "SUB_ALIGNER", name)
    monkeypatch.setattr(tools, "HF_HUB", tmp_path / "hub")
    assert job(subtitles="true")["aligner"] is None  # not downloaded
    (tmp_path / "hub" / "models--mlx-community--Qwen3-ForcedAligner-0.6B-8bit").mkdir(
        parents=True
    )
    assert job(subtitles="true")["aligner"] == name
    assert job(subtitles="false")["aligner"] is None


# Join clips and Add Subtitles with the real ffmpeg, on small synthetic clips


def note(out: Path, name: str) -> str:
    return json.loads((out / "notes.json").read_text())[name]


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


ENTRIES = (
    "stream=codec_name,codec_type,width,height,r_frame_rate,channels,sample_rate,duration,color_primaries,"
    "color_transfer,color_space,color_range:stream_tags=language:stream_disposition=default"
)


def streams(path: Path) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", ENTRIES, "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)["streams"]


def pixel(path: Path, at: float, x: int, y: int) -> tuple[int, ...]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-vf", f"crop=2:2:{x}:{y},scale=1:1,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return tuple(raw)


@pytest.fixture
def clips(tmp_path: Path) -> list[Path]:
    """A 64x48 30 fps clip with mono 44.1 kHz sound (2 s), an 80x80 24 fps clip without sound (1.5 s) and a 40x60
    still image that is all see-through."""
    paths = [
        tmp_path / "in_clips_0.mp4",
        tmp_path / "in_clips_1.mp4",
        tmp_path / "in_clips_2.png",
    ]
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x48:rate=30:duration=2", "-f", "lavfi",
           "-i", "sine=frequency=220:duration=2:sample_rate=44100", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ac", "1", str(paths[0]))  # fmt: skip
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=80x80:rate=24:duration=1.5", "-c:v", "libx264",
           "-pix_fmt", "yuv420p", str(paths[1]))  # fmt: skip
    ffmpeg("-f", "lavfi", "-i", "color=c=black@0.0:size=40x60,format=rgba", "-frames:v", "1", str(paths[2]))  # fmt: skip
    return paths


def run_plan(plan: tools.Plan) -> None:
    for cmd in plan.commands:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr


@needs_ffmpeg
def test_concat_real_ffmpeg_cut_one_size_rate_and_sound(
    tmp_path: Path, clips: list[Path]
) -> None:
    files = {"clips": clips}
    p = params("edit.concat", still_seconds="1.2")
    tools.validate("edit.concat", p, files)
    plan = tools.plan("edit.concat", p, files, tmp_path)
    run_plan(plan)
    joined = tmp_path / "joined.mp4"
    video, audio = streams(joined)
    # the first clip's size and rate; BT.709 primaries and transfer, the BT.601 matrix, limited range (LTX_COLOURS)
    assert (video["width"], video["height"], video["r_frame_rate"]) == (64, 48, "30/1")
    assert (video["color_primaries"], video["color_transfer"]) == ("bt709", "bt709")
    assert (video["color_space"], video["color_range"]) == ("smpte170m", "tv")
    assert float(video["duration"]) == pytest.approx(4.7, abs=0.04)
    # stereo 48 kHz AAC for the whole video, silence for the clips without sound
    assert (audio["codec_name"], audio["channels"], audio["sample_rate"]) == (
        "aac",
        2,
        "48000",
    )
    assert float(audio["duration"]) == pytest.approx(4.7, abs=0.05)
    assert tools.loudness(joined, 2.1, 2.5) is None
    # the see-through still is laid on white and padded with black to the video's shape
    assert min(pixel(joined, 4.0, 31, 23)) > 240 and max(pixel(joined, 4.0, 1, 23)) < 16
    # the index first (+faststart)
    data = joined.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    assert note(tmp_path, "joined.mp4").startswith(
        "3 clips, cuts, 64x48 at 30 fps, 4.7 s; silence for clip 2, 3; final mix "
    )
    assert plan.commands[1][-1] == str(joined)  # the loudness of the encoded file
    assert tools.loudness(joined) == pytest.approx(-14.0, abs=1.0)


@needs_ffmpeg
def test_concat_real_ffmpeg_crossfades_and_a_turned_first_clip(
    tmp_path: Path, clips: list[Path]
) -> None:
    turned = tmp_path / "in_clips_9.mp4"
    ffmpeg("-display_rotation", "90", "-i", str(clips[0]), "-c", "copy", str(turned))
    files = {"clips": [turned, *clips[1:]]}
    p = params("edit.concat", transition="crossfade", fade="0.5", still_seconds="2")
    run_plan(tools.plan("edit.concat", p, files, tmp_path))
    video, audio = streams(tmp_path / "joined.mp4")
    # upright: 48x64; 2 + 1.5 + 2 s with two crossfades of 0.5 s
    assert (video["width"], video["height"]) == (48, 64)
    assert float(video["duration"]) == pytest.approx(4.5, abs=0.04)
    assert float(audio["duration"]) == pytest.approx(4.5, abs=0.05)
    assert note(tmp_path, "joined.mp4").startswith(
        "3 clips, crossfades of 0.5 s, 48x64 at 30 fps"
    )


def test_concat_graph_size_fit_and_inputs(
    tmp_path: Path, media: FakeMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chosen size, the crop fit, a still image looped for its length, and the input whitelist before every
    input."""
    monkeypatch.setattr(tools, "video_shape", lambda path: (64, 48, "30000/1001"))
    media.seconds = {"in_clips_0.mov": 2.0}
    files = {"clips": [tmp_path / "in_clips_0.mov", tmp_path / "in_clips_1.jpg"]}
    p = params("edit.concat", size="1080x1920", fit="crop", still_seconds="2.5")
    plan = tools.plan("edit.concat", p, files, tmp_path)
    cmd = plan.commands[0]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph
    assert "crop=1080:1920" in graph and "pad=1080" not in graph
    assert "fps=30000/1001" in graph
    # the still: made at the size in a prep step, then looped for its length at the video's rate
    still = str(tmp_path / "in_clips_1_still.png")
    assert media.prepped[0][-1] == still
    i = cmd.index(still)
    assert cmd[i - 7 : i] == ["-loop", "1", "-framerate", "30000/1001", "-t", f"{75 / (30000 / 1001):.6f}", "-i"]  # fmt: skip
    for at, arg in enumerate(cmd):
        if arg == "-i":
            assert (
                GUARD[1] == "file"
                and cmd[cmd.index(GUARD[0], max(0, at - 12)) + 1] == "file"
            )
    assert cmd.count("-protocol_whitelist") == cmd.count("-i") == 2
    assert media.first_pass  # loudness in two passes, as in the mix
    assert "measured_I=-17.50" in graph


def test_concat_without_loudness_or_without_any_sound(
    tmp_path: Path, media: FakeMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "video_shape", lambda path: (64, 48, "24/1"))
    media.seconds = {"in_clips_0.mp4": 2.0, "in_clips_1.mp4": 2.0}
    files = {"clips": [tmp_path / "in_clips_0.mp4", tmp_path / "in_clips_1.mp4"]}
    plan = tools.plan(
        "edit.concat", params("edit.concat", loudness="false"), files, tmp_path
    )
    assert len(plan.commands) == 1 and not media.first_pass
    assert "loudnorm" not in plan.commands[0][plan.commands[0].index("-filter_complex") + 1]  # fmt: skip
    media.audio = {"in_clips_0.mp4": False, "in_clips_1.mp4": False}
    plan = tools.plan("edit.concat", params("edit.concat"), files, tmp_path)
    assert len(plan.commands) == 1 and not media.first_pass
    assert note(tmp_path, "joined.mp4").endswith("; silence for clip 1, 2; no sound")


@pytest.mark.parametrize(
    ("names", "values", "error"),
    [
        (["a.mp4"], {}, "Add at least 2 clips"),
        (
            ["a.mp4", "b.png", "c.png"],
            {"still_seconds": "2, 3, 4"},
            "one for each of the 2 still images",
        ),
        (
            ["a.mp4", "b.png"],
            {"still_seconds": "0.2"},
            r"0.2 s is out of range \(0.5-60 s\)",
        ),
        (["a.mp4", "b.png"], {"still_seconds": "two"}, "give numbers of seconds"),
        (
            ["a.mp4", "b.mp4"],
            {"transition": "crossfade", "fade": "1.5"},
            r"at most half the shortest clip \(clip 2 is 2.0 s\)",
        ),
        (
            ["a.mp4", "b.png"],
            {"transition": "crossfade", "fade": "1", "still_seconds": "1.5"},
            "clip 2 is 1.5 s",
        ),
    ],
)
def test_concat_refuses_at_submit(
    tmp_path: Path,
    media: FakeMedia,
    names: list[str],
    values: dict[str, str],
    error: str,
) -> None:
    media.seconds = {"a.mp4": 4.0, "b.mp4": 2.0}
    files = {"clips": [tmp_path / n for n in names]}
    with pytest.raises(ValueError, match=error):
        tools.validate("edit.concat", params("edit.concat", **values), files)


def test_concat_refuses_a_clip_without_a_picture(
    tmp_path: Path, media: FakeMedia
) -> None:
    media.codecs = {"b.mp4": None}
    media.seconds = {"a.mp4": 4.0}
    files = {"clips": [tmp_path / "a.mp4", tmp_path / "b.mp4"]}
    with pytest.raises(ValueError, match="Clip 2 has no picture"):
        tools.validate("edit.concat", params("edit.concat"), files)


SRT = (
    "﻿1\r\n00:00:00,200 --> 00:00:01,000\r\n<font color=red>Merhaba</font> dünya\r\n\r\n"
    "2\r\n00:00:01,100 --> 00:00:03,500\r\n<i>İkinci</i> satır\r\n\r\n"
    "3\r\n00:00:05,000 --> 00:00:06,000\r\nafter the end\r\n"
)


@needs_ffmpeg
def test_subtitle_real_ffmpeg_adds_a_mov_text_track(
    tmp_path: Path, clips: list[Path]
) -> None:
    sub = tmp_path / "in_subtitles_0.srt"
    sub.write_bytes(SRT.encode())
    files = {"video": [clips[0]], "subtitles": [sub]}
    p = params("edit.subtitle", language="tur")
    tools.validate("edit.subtitle", p, files)
    run_plan(tools.plan("edit.subtitle", p, files, tmp_path))
    out = tmp_path / "subtitled.mp4"
    video, audio, text = streams(out)
    assert (video["codec_name"], audio["codec_name"]) == ("h264", "aac")  # copied
    assert text["codec_name"] == "mov_text"
    assert (text["tags"]["language"], text["disposition"]["default"]) == ("tur", 1)
    track = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    # the cue that starts after the 2 s video is left out, the one that runs past its end is cut there
    assert track.replace("\r\n", "\n") == (
        "1\n00:00:00,200 --> 00:00:01,000\nMerhaba dünya\n\n"
        "2\n00:00:01,100 --> 00:00:02,000\n<i>İkinci</i> satır\n\n"
    )
    assert note(tmp_path, "subtitled.mp4") == (
        "2 subtitles, language Turkish; 1 after the video's end left out"
    )
    data = out.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")


@needs_ffmpeg
def test_subtitle_real_ffmpeg_encodes_pcm_sound_and_reads_webvtt(
    tmp_path: Path,
) -> None:
    video, sub = tmp_path / "in_video_0.mov", tmp_path / "in_subtitles_0.vtt"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=64x48:rate=24:duration=1", "-f", "lavfi",
           "-i", "sine=duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(video))  # fmt: skip
    sub.write_text("WEBVTT\n\n00:00.000 --> 00:00.800\n<v Ana>Hi</v>\n")
    files = {"video": [video], "subtitles": [sub]}
    run_plan(
        tools.plan("edit.subtitle", params("edit.subtitle"), files, tmp_path)
    )  # fmt: skip
    _, audio, text = streams(tmp_path / "subtitled.mp4")
    assert audio["codec_name"] == "aac"  # an MP4 does not take PCM
    assert text["tags"]["language"] == "und"
    track = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(tmp_path / "subtitled.mp4"), "-map", "0:s:0", "-f", "srt", "-"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert track.replace("\r\n", "\n") == "1\n00:00:00,000 --> 00:00:00,800\nHi\n\n"


def test_subtitle_refuses_a_bad_file_or_a_video_it_does_not_cover(
    tmp_path: Path, media: FakeMedia
) -> None:
    video, sub = tmp_path / "in_video_0.mp4", tmp_path / "in_subtitles_0.srt"
    files = {"video": [video], "subtitles": [sub]}
    sub.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nİyi\n".encode("cp1254"))
    with pytest.raises(ValueError, match="not UTF-8"):
        tools.validate("edit.subtitle", params("edit.subtitle"), files)
    sub.write_text("1\n00:00:09,000 --> 00:00:10,000\nlate\n")
    tools.validate("edit.subtitle", params("edit.subtitle"), files)
    media.seconds = {"in_video_0.mp4": 5.0}
    with pytest.raises(
        ValueError, match=r"Every subtitle starts after the video ends \(5.0 s\)"
    ):
        tools.plan("edit.subtitle", params("edit.subtitle"), files, tmp_path)
    media.codecs = {"in_video_0.mp4": None}
    with pytest.raises(ValueError, match="no picture"):
        tools.validate("edit.subtitle", params("edit.subtitle"), files)
    with pytest.raises(ValueError, match="Unknown subtitle language"):
        media.codecs = {}
        sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nok\n")
        tools.plan("edit.subtitle", params("edit.subtitle", language="xx"), files, tmp_path)  # fmt: skip
