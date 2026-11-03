"""The media tools: media_info and view_image (app/agent/media_tools.py) over the granted folder, the chat's work folder
and the Library. They run the real ffprobe and ffmpeg on small files that ffmpeg makes here, and are skipped when
ffmpeg is not installed."""

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from app.agent import (
    Context,
    Registry,
    Settings,
    build_registry,
    file_tools,
    media_tools,
)
from tests.fake_workstation import FakeWorkstation

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=60)


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("media")
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=sample_rate=48000",
        "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "clip.mp4"),
    )  # fmt: skip
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=sample_rate=44100",
        "-t",
        "2",
        "-ac",
        "2",
        str(d / "tone.wav"),
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=2000x1000",
        "-frames:v",
        "1",
        str(d / "wide.png"),
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240",
        "-frames:v",
        "1",
        str(d / "small.jpg"),
    )
    return d


@pytest.fixture
def ws() -> FakeWorkstation:
    return FakeWorkstation()


@pytest.fixture
def reg(ws: FakeWorkstation) -> Registry:
    return build_registry(ws, Settings())


@pytest.fixture
def folder(tmp_path: Path, media: Path) -> Path:
    root = tmp_path / "granted"
    root.mkdir()
    for f in media.iterdir():
        shutil.copy(f, root / f.name)
    (root / "notes.txt").write_text("text\n")
    (root / ".secret.png").write_bytes((media / "small.jpg").read_bytes())
    (tmp_path / "private.png").write_bytes((media / "wide.png").read_bytes())
    fd = os.open(root, os.O_RDONLY)
    try:
        return Path(file_tools.fd_path(fd))
    finally:
        os.close(fd)


@pytest.fixture
def ctx(folder: Path, tmp_path: Path) -> Context:
    (tmp_path / "chat").mkdir()
    return Context("c1", folder, threading.Event(), tmp_path / "chat" / "ws")


def call(reg: Registry, ctx: Context, name: str, **args: Any):
    return reg.call(name, args, ctx)


def size_of(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


# ---------- media_info ----------


def test_media_info_of_a_video_an_audio_file_and_an_image(
    reg: Registry, ctx: Context
) -> None:
    r = call(reg, ctx, "media_info", path="clip.mp4")
    assert r.status == "ok", r.content
    lines = r.content.splitlines()
    assert lines[0].startswith("clip.mp4: ") and "format mov,mp4" in lines[0]
    assert "duration: 1.00 s" in lines
    assert "video: h264, 320x240, 25 fps" in lines
    assert "audio: aac, 1 channel, 48000 Hz" in lines
    r = call(reg, ctx, "media_info", path="tone.wav")
    assert "audio: pcm_s16le, 2 channels, 44100 Hz" in r.content.splitlines()
    assert "duration: 2.00 s" in r.content
    r = call(reg, ctx, "media_info", path="wide.png")
    assert "video: png, 2000x1000" in r.content.splitlines()
    assert "duration" not in r.content and "fps" not in r.content  # a still


def test_media_info_reads_the_work_folder_and_the_library(
    reg: Registry, ctx: Context, ws: FakeWorkstation, media: Path, tmp_path: Path
) -> None:
    ctx.work_dir.mkdir()
    shutil.copy(media / "tone.wav", ctx.work_dir / "out.wav")
    r = call(reg, ctx, "media_info", path="out.wav", location="work")
    assert r.status == "ok" and r.content.startswith("work folder: out.wav:")
    ws.outputs[("20261103-110000-beef", "image.png")] = media / "wide.png"
    r = call(
        reg,
        ctx,
        "media_info",
        path="20261103-110000-beef/image.png",
        location="library",
    )
    assert r.status == "ok" and "2000x1000" in r.content
    r = call(
        reg,
        ctx,
        "media_info",
        path="20261103-110000-beef/other.png",
        location="library",
    )
    assert r.status == "error" and "no such Library output" in r.content
    # without a folder grant the work folder and the Library still work; the folder does not
    bare = Context("c1", None, threading.Event(), ctx.work_dir)
    assert call(reg, bare, "media_info", path="out.wav", location="work").status == "ok"
    r = call(reg, bare, "media_info", path="clip.mp4")
    assert r.status == "error" and "No folder" in r.content
    # the chat profile has no Library
    chat_only = build_registry(None, Settings())
    r = chat_only.call(
        "media_info", {"path": "j/image.png", "location": "library"}, ctx
    )
    assert r.status == "error" and "no Library" in r.content


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"path": "notes.txt"}, "not a supported type"),
        ({"path": ".secret.png"}, "hidden or secret"),
        ({"path": "../private.png"}, "outside the granted folder"),
        ({"path": "missing.mp4"}, "no such file"),
        ({"path": "../chat.json.png", "location": "work"}, "without '..'"),
        ({"path": "x.png", "location": "nowhere"}, "must be one of"),
    ],
)
def test_media_info_refusals(
    reg: Registry, ctx: Context, args: dict, message: str
) -> None:
    r = reg.call("media_info", args, ctx)
    assert r.status == "error" and message in r.content, r.content


def test_links_in_the_work_folder_are_not_followed(
    reg: Registry, ctx: Context, tmp_path: Path
) -> None:
    ctx.work_dir.mkdir()
    (ctx.work_dir / "link.png").symlink_to(tmp_path / "private.png")
    (ctx.work_dir / "up").symlink_to(tmp_path)
    for path in ("link.png", "up/private.png"):
        for name in ("media_info", "view_image"):
            r = call(reg, ctx, name, path=path, location="work")
            assert r.status == "error", (name, path)
            assert "2000x1000" not in r.content


def test_a_playlist_cannot_make_ffprobe_read_other_files(
    reg: Registry, ctx: Context, folder: Path, tmp_path: Path
) -> None:
    """An HLS playlist or a concat list named like a video would read the files it lists; the demuxer list stops it."""
    (folder / "list.mp4").write_text(
        f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{tmp_path / 'private.png'}\n#EXT-X-ENDLIST\n"
    )
    (folder / "concat.mov").write_text(
        f"ffconcat version 1.0\nfile '{tmp_path / 'private.png'}'\n"
    )
    for path in ("list.mp4", "concat.mov"):
        r = call(reg, ctx, "media_info", path=path)
        assert r.status == "error" and "2000x1000" not in r.content, path


def test_ffprobe_runs_with_an_argument_list_and_a_time_limit(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)
    r = call(reg, ctx, "media_info", path="clip.mp4")
    assert r.status == "error" and "took longer than 20 s" in r.content
    cmd, kw = seen[0]
    assert isinstance(cmd, list) and cmd[0] == "ffprobe" and "shell" not in kw
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert "-format_whitelist" in cmd and cmd[-1].startswith("/dev/fd/")
    assert kw["timeout"] == media_tools.PROBE_SECONDS and kw["pass_fds"]


def test_a_missing_ffprobe_is_named(
    reg: Registry, ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_tools, "FFPROBE", "no-such-ffprobe-here")
    r = call(reg, ctx, "media_info", path="clip.mp4")
    assert r.status == "error" and "is not installed" in r.content


# ---------- view_image ----------


def test_view_image_makes_a_small_png_for_the_model(
    reg: Registry, ctx: Context, folder: Path
) -> None:
    r = call(reg, ctx, "view_image", path="wide.png")
    assert r.status == "ok", r.content
    assert r.content.startswith("wide.png (2000x1000): the image is attached")
    (name,) = r.images
    assert media_tools_name_ok(name)
    copy = ctx.work_dir.parent / name
    assert copy.is_file() and size_of(copy) == (1280, 640)
    assert not list(ctx.work_dir.parent.glob(".view_*"))  # no temp file left
    small = call(reg, ctx, "view_image", path="small.jpg")
    assert size_of(ctx.work_dir.parent / small.images[0]) == (
        320,
        240,
    )  # never enlarged


def media_tools_name_ok(name: str) -> bool:
    from app.chat import IMAGE

    return IMAGE.fullmatch(name) is not None and name.startswith("view_")


def test_view_image_refusals(
    reg: Registry, ctx: Context, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = call(reg, ctx, "view_image", path="clip.mp4")
    assert r.status == "error" and "not a supported type" in r.content
    (folder / "fake.png").write_text("not an image")
    r = call(reg, ctx, "view_image", path="fake.png")
    assert r.status == "error" and "could not read it" in r.content
    monkeypatch.setattr(media_tools, "MAX_IMAGE", 10)
    r = call(reg, ctx, "view_image", path="small.jpg")
    assert r.status == "error" and "up to 10 B" in r.content
    assert not [
        p for p in ctx.work_dir.parent.iterdir() if p.name.startswith((".view", "view"))
    ]
