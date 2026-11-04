"""Input images and LTX videos with the real ffmpeg, ffprobe and sips, on tiny synthetic files: HEIC and animated GIF
inputs, alpha laid on white, the input whitelist, frame counts, the colour tags after LTX and the extend splice.
Each test is skipped when a binary it needs is missing."""

import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from app import assist, chat, tools
from app.runners import tag_video

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg is not installed",
)
needs_sips = pytest.mark.skipif(
    not Path(tools.SIPS).exists(), reason="sips (macOS) is not here"
)
pytestmark = needs_ffmpeg


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def facts(path: Path, entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def stream(path: Path) -> dict:
    return facts(path, "stream=width,height,pix_fmt,nb_frames")["streams"][0]


def pixel(path: Path) -> tuple[int, int, int]:
    """The RGB value of the image's centre pixel."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "crop=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    return raw[0], raw[1], raw[2]


@pytest.fixture
def gif(tmp_path: Path) -> Path:
    """An animated GIF of 5 frames, uploaded from the page (an unknown suffix is kept as .bin)."""
    path = tmp_path / "anim.gif"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=96x64:r=5:d=1", str(path))
    upload = tmp_path / "in_image_0.bin"
    path.rename(upload)
    return upload


@pytest.fixture
def heic(tmp_path: Path) -> Path:
    """A HEIC that sips makes: above 512 px it is a tiled grid, like an iPhone photo, which ffmpeg cannot scale."""
    if not Path(tools.SIPS).exists():
        pytest.skip("sips (macOS) is not here")
    png, path = tmp_path / "photo.png", tmp_path / "in_image_0.heic"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=1200x900", "-frames:v", "1", str(png))
    subprocess.run(
        [tools.SIPS, "-s", "format", "heic", str(png), "--out", str(path)],
        capture_output=True,
        check=True,
    )
    return path


@pytest.fixture
def see_through(tmp_path: Path) -> Path:
    """A PNG of half-transparent red."""
    path = tmp_path / "in_image_0.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red@0.5:s=64x64,format=rgba", "-frames:v", "1", str(path))  # fmt: skip
    return path


# Input images


def test_an_animated_gif_gives_its_first_frame(tmp_path: Path, gif: Path) -> None:
    dst = tmp_path / "start.png"
    tools.fit_image(gif, dst, 64, 128)
    assert stream(dst) | {"nb_frames": None} == {
        "width": 64,
        "height": 128,
        "pix_fmt": "rgb24",
        "nb_frames": None,
    }


@needs_sips
def test_a_heic_photo_is_converted_with_sips(tmp_path: Path, heic: Path) -> None:
    assert tools.is_heif(heic)
    # without the conversion ffmpeg fails on the tiled grid
    with pytest.raises(ValueError, match="Simple and complex filtering cannot be used"):
        tools.prep(["ffmpeg", "-v", "error", "-y", "-i", str(heic), "-vf", "scale=64:48", str(tmp_path / "x.png")])  # fmt: skip
    dst = tmp_path / "person.png"
    tools.fit_image(heic, dst, 128, 128)
    assert stream(dst)["width"] == 128 and stream(dst)["height"] == 128
    converted = tmp_path / "in_image_0.heic.png"  # in_*: never listed as an output
    assert stream(converted)["width"] == 1200


@needs_sips
def test_a_heic_is_found_by_its_content(tmp_path: Path, heic: Path) -> None:
    upload = heic.rename(tmp_path / "in_image_0.bin")
    assert tools.readable_image(upload, tmp_path) == tmp_path / "in_image_0.bin.png"


@needs_sips
def test_a_damaged_heic_is_refused_in_plain_words(tmp_path: Path) -> None:
    bad = tmp_path / "in_image_0.heic"
    bad.write_bytes(b"\0\0\0\x18ftypheic" + b"\0" * 20)
    with pytest.raises(ValueError, match="^Could not convert the HEIC image: "):
        tools.readable_image(bad, tmp_path)


def test_other_images_are_used_as_they_are(
    tmp_path: Path, see_through: Path, gif: Path
) -> None:
    assert not tools.is_heif(see_through) and not tools.is_heif(gif)
    assert tools.readable_image(see_through, tmp_path) == see_through
    assert not tools.is_heif(tmp_path / "missing.png")


def test_klein_edit_gets_heic_references_as_png(
    tmp_path: Path, heic: Path, see_through: Path
) -> None:
    p = {"prompt": "x", "size": "768x1344"}
    files = {"references": [see_through, heic]}
    [cmd] = tools.plan("image.klein_edit", p, files, tmp_path).commands
    refs = cmd[cmd.index("--image-paths") + 1 : cmd.index("--image-paths") + 3]
    # the see-through PNG is laid on white first (mflux drops alpha); the HEIC from sips is opaque
    white = tmp_path / "in_image_0_white.png"
    assert refs == [str(white), str(tmp_path / "in_image_0.heic.png")]
    assert stream(white)["pix_fmt"] == "rgb24"
    r, g, b = pixel(white)
    assert r == 255 and abs(g - 128) <= 2 and abs(b - 128) <= 2


@needs_sips
def test_a_heic_can_become_a_jpeg(tmp_path: Path, heic: Path) -> None:
    """The chat stores an iPhone photo as JPEG (far smaller than a PNG of a photo)."""
    jpeg = tools.readable_image(heic, tmp_path, "jpeg")
    assert jpeg == tmp_path / "in_image_0.heic.jpg"
    assert jpeg.read_bytes()[:3] == b"\xff\xd8\xff" and stream(jpeg)["width"] == 1200


# EXIF orientation: a phone stores the pixels as the sensor saw them and an Orientation tag that turns them upright

# a TIFF block with one tag: Orientation (0x0112) = 6, turn 90 degrees clockwise to show
ORIENTATION_6 = b"MM\x00\x2a" + struct.pack(">IHHHIHHI", 8, 1, 0x0112, 3, 1, 6, 0, 0)
RED, BLUE = "red", "blue"


def turned(path: Path, see_through: bool = False) -> Path:
    """An image stored 400x200 (left half red, right half blue) with Orientation 6: upright it is 200x400 with red
    on top. A .jpg gets the tag in an APP1 segment, a .png (see-through: half alpha) in an eXIf chunk."""
    alpha, fmt = ("@0.5", ",format=rgba") if see_through else ("", "")
    graph = f"color=c=red{alpha}:s=200x200{fmt}[a];color=c=blue{alpha}:s=200x200{fmt}[b];[a][b]hstack"
    ffmpeg("-f", "lavfi", "-i", graph, "-frames:v", "1", str(path))
    data = path.read_bytes()
    if path.suffix == ".png":
        chunk = b"eXIf" + ORIENTATION_6
        tag = (
            struct.pack(">I", len(ORIENTATION_6))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk))
        )
        path.write_bytes(data[:33] + tag + data[33:])  # after the signature and IHDR
    else:
        app1 = b"Exif\x00\x00" + ORIENTATION_6
        at = 4 + struct.unpack(">H", data[4:6])[0]  # after the JFIF segment
        path.write_bytes(
            data[:at]
            + b"\xff\xe1"
            + struct.pack(">H", len(app1) + 2)
            + app1
            + data[at:]
        )
    return path


def upright(path: Path) -> tuple[int, int, str, str]:
    """Width, height and the colour names at the top and the bottom centre, as the app's ffmpeg calls see the image
    (ffmpeg turns it upright by its EXIF orientation)."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "ppm", "-"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    _, w, h, _, px = out.split(maxsplit=4)
    w, h = int(w), int(h)

    def name(y: int) -> str:
        r, _, b = px[(y * w + w // 2) * 3 : (y * w + w // 2) * 3 + 3]
        return RED if r > b else BLUE

    return w, h, name(2), name(h - 3)


def test_a_turned_photo_comes_out_upright(tmp_path: Path) -> None:
    src = turned(tmp_path / "in_image_0.jpg")
    assert stream(src)["width"] == 400  # stored sideways
    dst = tmp_path / "start.png"
    tools.fit_image(src, dst, 200, 400)
    assert upright(dst) == (200, 400, RED, BLUE)


def test_a_turned_see_through_image_is_laid_on_white_upright(
    tmp_path: Path,
) -> None:
    """The white goes under the upright frame: a white of the stored size (400x200) cut the turned frame."""
    src = turned(tmp_path / "in_image_0.png", see_through=True)
    dst = tmp_path / "start.png"
    tools.fit_image(src, dst, 200, 400)
    assert upright(dst) == (200, 400, RED, BLUE)
    assert pixel(dst) != (255, 255, 255)
    white = tools.on_white(src, tmp_path)
    assert upright(white) == (200, 400, RED, BLUE)
    assert stream(white)["pix_fmt"] == "rgb24"


@needs_sips
def test_a_turned_iphone_photo_keeps_its_orientation(tmp_path: Path) -> None:
    """sips keeps the Orientation tag (it does not turn the pixels); every reader of the converted file turns it
    upright: ffmpeg (jobs), PIL's exif_transpose (mlx-vlm and mflux) and the browser."""
    heic = tmp_path / "in_image_0.heic"
    subprocess.run(
        [tools.SIPS, "-s", "format", "heic", str(turned(tmp_path / "photo.jpg")), "--out", str(heic)],
        capture_output=True,
        check=True,
    )  # fmt: skip
    png = tools.readable_image(heic, tmp_path)
    assert stream(png)["width"] == 400 and upright(png) == (200, 400, RED, BLUE)
    jpeg = tmp_path / "chat.jpg"
    jpeg.write_bytes(chat.heif_to_jpeg(heic.read_bytes()))
    assert stream(jpeg)["width"] == 400 and upright(jpeg) == (200, 400, RED, BLUE)


# Alpha


def test_an_image_with_alpha_is_laid_on_white(
    tmp_path: Path, see_through: Path
) -> None:
    assert tools.image_filter(see_through, "null")[0] == "-filter_complex"
    dst = tmp_path / "start.png"
    tools.fit_image(see_through, dst, 64, 64)
    assert stream(dst)["pix_fmt"] == "rgb24"
    r, g, b = pixel(dst)
    # half red on white: (255, 128, 128) give or take rounding; dropping alpha would give pure red
    assert r == 255 and abs(g - 128) <= 2 and abs(b - 128) <= 2


@pytest.mark.parametrize(
    "name", ["in_image_0.jpg", "in_image_0.jpeg", "in_image_0.png"]
)
def test_an_opaque_photo_keeps_the_simple_filter(tmp_path: Path, name: str) -> None:
    """A photo-sized file: ffmpeg opens a .jpg over 2 KB with image2 (by its name), not jpeg_pipe."""
    src = tmp_path / name
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=1024x768", "-frames:v", "1", str(src))
    assert src.stat().st_size > 20_000
    assert tools.image_filter(src, "null") == ["-vf", "null"]
    dst = tmp_path / "start.png"
    tools.fit_image(src, dst, 704, 1280)
    assert (stream(dst)["width"], stream(dst)["height"]) == (704, 1280)


def test_the_error_names_the_refused_format() -> None:
    err = (
        "[image2 @ 0x7798c38000] Format not on whitelist 'png_pipe,jpeg_pipe'\n"
        "/tmp/in_image_0.jpg: Invalid argument\n"
    )
    assert tools.ffmpeg_error(err) == (
        "this file type is not supported (use a common image, audio or video file)"
    )


def test_alpha_is_read_from_the_pixel_format() -> None:
    """ALPHA names exactly the pixel formats that ffmpeg flags as having alpha (pal8 counts: a palette can hold
    transparent colours)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_pixel_formats", "-of", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for f in json.loads(out)["pixel_formats"]:
        has_alpha = f["flags"]["alpha"] == 1 or f["name"] == "pal8"
        assert bool(tools.ALPHA.match(f["name"])) is has_alpha, f["name"]


# Prompt help stills


@pytest.mark.parametrize("kind", ["gif", "heic", "see_through"])
def test_prompt_help_reads_the_same_images(
    tmp_path: Path, request: pytest.FixtureRequest, kind: str
) -> None:
    src = request.getfixturevalue(kind)
    dst = assist.still(src, tmp_path / "still.jpg", video=False)
    assert dst.read_bytes().startswith(b"\xff\xd8")
    if kind == "see_through":
        r, g, b = pixel(dst)
        assert (
            r > 240 and 110 < g < 150 and 110 < b < 150
        )  # on white (a JPEG is not exact)


def test_prompt_help_reads_the_last_frame_of_a_video(tmp_path: Path) -> None:
    clip = (
        tmp_path / "in_video_0"
    )  # as the server stores an upload for prompt help: no suffix
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=1", "-c:v", "libx264", "-f", "mp4", str(clip))  # fmt: skip
    dst = assist.still(clip, tmp_path / "still.jpg", video=True)
    assert stream(dst)["width"] == 64


def test_prompt_help_names_the_cause_without_ffmpeg_prefixes(tmp_path: Path) -> None:
    bad = tmp_path / "in_image_0"
    bad.write_bytes(b"not an image")
    with pytest.raises(assist.AssistError) as exc:
        assist.still(bad, tmp_path / "still.jpg", video=False)
    assert str(exc.value).startswith("Could not read the input file: ")
    assert "@ 0x" not in str(exc.value)


# The input whitelist


def test_a_concat_list_with_a_media_name_is_not_opened(tmp_path: Path) -> None:
    secret = tmp_path / "other.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=d=0.5", str(secret))
    fake = tmp_path / "in_audio_0.mp3"
    fake.write_text(f"ffconcat version 1.0\nfile '{secret}'\n")
    with pytest.raises(ValueError, match="^Could not read the input file: "):
        tools.media_seconds(fake)
    with pytest.raises(ValueError, match="^Could not prepare the input file: "):
        tools.prep(["ffmpeg", "-v", "error", "-y", *tools.GUARD, "-i", str(fake), str(tmp_path / "out.wav")])  # fmt: skip
    # the same media file opens with the guard
    assert 0.4 < tools.media_seconds(secret) < 0.6


# Frame counts


def test_frame_count_reads_the_mp4_header(tmp_path: Path) -> None:
    clip = tmp_path / "source.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip))  # fmt: skip
    assert tools.frame_count(clip) == 36


def test_frame_count_counts_a_stream_without_a_header_count(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mkv"  # Matroska stores no frame count
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=1", "-c:v", "libx264", str(clip))  # fmt: skip
    assert stream(clip).get("nb_frames") is None
    assert tools.frame_count(clip) == 24


# Colour tags and index after LTX


def ltx_like(path: Path) -> None:
    """H.264 + AAC with no colour tags and the index at the end, as ltx-2-mlx writes it."""
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=0.5", "-f", "lavfi", "-i", "sine=d=0.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    )  # fmt: skip


def frame_md5(path: Path) -> str:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout  # fmt: skip


def colours(path: Path) -> dict:
    keys = "color_range,color_space,color_primaries,color_transfer"
    return {k: v for k, v in facts(path, f"stream={keys}")["streams"][0].items()}


def test_the_tag_step_tags_colours_and_moves_the_index(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    ltx_like(video)
    data = video.read_bytes()
    assert data.find(b"moov") > data.find(b"mdat")
    before = frame_md5(video)
    done = subprocess.run(
        tools.finish_video(video), capture_output=True, text=True, check=False
    )
    assert done.returncode == 0
    assert "tagged" in done.stdout
    assert colours(video) == {
        "color_range": "tv",
        "color_space": "smpte170m",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
    }
    data = video.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")  # faststart
    assert frame_md5(video) == before  # a stream copy: the same pixels
    assert [p.name for p in tmp_path.iterdir()] == ["video.mp4"]


def test_the_tag_step_and_the_splice_write_the_same_tags() -> None:
    assert tag_video.TAGS == tools.LTX_COLOURS


def test_the_tag_step_keeps_the_file_when_it_fails(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=0.5", "-c:v", "mpeg4", str(video))  # fmt: skip
    data = video.read_bytes()
    why = tag_video.tag(video)
    assert why is not None and "not supported by the bitstream filter" in why
    assert "@ 0x" not in why
    assert video.read_bytes() == data
    assert [p.name for p in tmp_path.iterdir()] == ["video.mp4"]
    # the step itself never fails the job
    done = subprocess.run(
        [sys.executable, str(tools.APP / "runners/tag_video.py"), str(tmp_path / "gone.mp4")],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert done.returncode == 0 and "kept gone.mp4 as it is" in done.stdout


def test_the_extend_splice_tags_the_colours(tmp_path: Path) -> None:
    """The extend prep and splice on a silent 64x64 clip (the lavfi silence is an input the app makes: the guard
    must not block it), with a fake model output."""
    src = tmp_path / "in_video_0.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x64:r=24:d=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src))  # fmt: skip
    plan = tools.plan(
        "video.extend", {"prompt": "x", "seconds": "1"}, {"video": [src]}, tmp_path
    )
    ext, splice = plan.commands
    new = 8 * int(ext[ext.index("--extend-frames") + 1])
    tail = tmp_path / "tail.mp4"
    carry = int(stream(tail)["nb_frames"])
    ffmpeg(
        "-i", str(tail), "-f", "lavfi", "-i", "testsrc=s=64x64:r=24", "-f", "lavfi", "-i", "sine=r=48000",
        "-filter_complex", f"[1:v]trim=end_frame={new},setpts=PTS-STARTPTS[n];[0:v][n]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-t", f"{(carry + new) / 24:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(tmp_path / "extended_raw.mp4"),
    )  # fmt: skip
    subprocess.run(splice, capture_output=True, check=True)
    out = tmp_path / "video.mp4"
    assert colours(out) == {
        "color_range": "tv",
        "color_space": "smpte170m",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
    }
    assert int(stream(out)["nb_frames"]) == 36 + new
