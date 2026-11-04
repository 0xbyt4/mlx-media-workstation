"""The Library endpoints on a temp outputs folder: listing, Show in Finder and deleting one output."""

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import library, server

Job = Callable[..., dict[str, Any]]
REAL_PROBE = library.probe  # the autouse fixture replaces library.probe


@pytest.fixture(autouse=True)
def no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Canned media facts instead of ffprobe; returns the files that were probed."""
    seen: list[Path] = []

    def fake(path: Path) -> dict[str, Any]:
        seen.append(path)
        return {"width": 768, "height": 1344, "duration": 5.04}

    monkeypatch.setattr(library, "probe", fake)
    monkeypatch.setattr(library, "probed", {})
    return seen


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Records `open -R` calls instead of running them."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(library.subprocess, "run", fake_run)
    return calls


def done_job(make_job: Job, state: Path, job_id: str, created: str, **files: str):
    """A finished job whose outputs are `files` (name -> kind), written as small files."""
    outputs = [
        {"url": f"/files/{job_id}/{n}", "kind": k, "name": n, "note": ""}
        for n, k in files.items()
    ]
    job = make_job(job_id, "done", created=created, outputs=outputs)
    for n in files:
        (state / job_id / n).write_bytes(b"x" * 10)
    (state / job_id / "in_image_0.png").write_bytes(b"input")
    return job


# Listing


def test_library_lists_outputs_of_done_jobs_newest_first(
    client: TestClient, make_job: Job, state: Path
) -> None:
    done_job(
        make_job,
        state,
        "20261101-090000-aaaa",
        "2026-11-01T09:00:00",
        **{"image.png": "image"},
    )
    done_job(
        make_job,
        state,
        "20261102-090000-bbbb",
        "2026-11-02T09:00:00",
        **{
            "video.mp4": "video",
            "music.wav": "audio",
        },
    )
    make_job("20261102-100000-cccc", "running")
    make_job("20261102-110000-dddd", "failed")
    items = client.get("/api/library").json()
    assert [(i["job"], i["name"]) for i in items] == [
        ("20261102-090000-bbbb", "video.mp4"),
        ("20261102-090000-bbbb", "music.wav"),
        ("20261101-090000-aaaa", "image.png"),
    ]
    video = items[0]
    assert video == {
        "job": "20261102-090000-bbbb",
        "tool": "image.klein",
        "tool_label": "FLUX.3 Klein",
        "kind": "video",
        "name": "video.mp4",
        "url": "/files/20261102-090000-bbbb/video.mp4",
        "note": "",
        "size": 10,
        "created": "2026-11-02T09:00:00",
        "finished": None,
        "elapsed": None,
        "peak_gb": None,
        "text": "a lighthouse",
        "params": {"prompt": "a lighthouse"},
        "path": str(state / "20261102-090000-bbbb" / "video.mp4"),
        "width": 768,
        "height": 1344,
        "duration": 5.04,
    }
    assert (items[1]["width"], items[1]["duration"]) == (None, 5.04)  # audio
    assert (items[2]["width"], items[2]["duration"]) == (768, None)  # image


def test_library_skips_missing_files_and_symlinks(
    client: TestClient, make_job: Job, state: Path, tmp_path: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-02T09:00:00",
        **{
            "a.png": "image",
            "b.png": "image",
            "c.png": "image",
        },
    )
    (state / job_id / "b.png").unlink()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    (state / job_id / "c.png").unlink()
    (state / job_id / "c.png").symlink_to(outside)
    assert [i["name"] for i in client.get("/api/library").json()] == ["a.png"]


def test_library_main_text_and_cache(
    client: TestClient, make_job: Job, state: Path, no_ffprobe: list[Path]
) -> None:
    job = done_job(
        make_job,
        state,
        "20261102-090000-aaaa",
        "2026-11-02T09:00:00",
        **{"take_1.wav": "audio"},
    )
    job["params"] = {"text": "  ", "caption": "calm piano"}
    assert client.get("/api/library").json()[0]["text"] == "calm piano"
    client.get("/api/library")
    assert len(no_ffprobe) == 1  # the second list uses the cached facts
    (state / job["id"] / "take_1.wav").write_bytes(b"longer file")
    client.get("/api/library")
    assert len(no_ffprobe) == 2  # a changed file is probed again


def test_probe_parses_and_survives_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [
        json.dumps(
            {
                "streams": [
                    {"codec_type": "audio"},
                    {"codec_type": "video", "width": 704, "height": 1280},
                ],
                "format": {"duration": "5.041667"},
            }
        ),
        "not json",
        subprocess.TimeoutExpired("ffprobe", 10),
    ]

    def fake_run(cmd: list[str], **kw: Any) -> SimpleNamespace:
        assert cmd[0] == "ffprobe" and cmd[-1] == "/x/clip.mp4"
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(stdout=a)

    monkeypatch.setattr(library.subprocess, "run", fake_run)
    empty = {"width": None, "height": None, "duration": None}
    assert REAL_PROBE(Path("/x/clip.mp4")) == {
        "width": 704,
        "height": 1280,
        "duration": 5.04,
    }
    assert REAL_PROBE(Path("/x/clip.mp4")) == empty
    assert REAL_PROBE(Path("/x/clip.mp4")) == empty


def png_head(width: int, height: int) -> bytes:
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr + b"crc!"


def le(n: int, size: int = 4) -> bytes:
    return n.to_bytes(size, "little")


def wav_bytes(code: int, rate: int, data: int, declared: int | None = None) -> bytes:
    """A WAV file: format `code` at `rate` bytes a second, a LIST chunk, and `data` bytes of samples."""
    fmt = le(code, 2) + le(1, 2) + le(rate // 2) + le(rate) + le(2, 2) + le(16, 2)
    info = b"INFOISFT\x05\x00\x00\x00test\x00\x00"  # odd length 5: padded to 6
    body = b"WAVE" + b"fmt " + le(len(fmt)) + fmt + b"LIST" + le(len(info)) + info
    body += b"data" + le(data if declared is None else declared) + b"\0" * data
    return b"RIFF" + le(len(body)) + body


def test_header_facts_read_png_and_wav(tmp_path: Path) -> None:
    files = {
        "a.png": png_head(1344, 768),
        "pcm.wav": wav_bytes(1, 48000, 48000 * 5 + 2000),  # 5.04 s
        "float.wav": wav_bytes(3, 64000, 64000 * 2),
        "adpcm.wav": wav_bytes(2, 48000, 48000),  # compressed: its rate is a rough one
        "open.wav": wav_bytes(1, 48000, 4800, declared=0xFFFFFFFF),  # streamed
        "zero.wav": wav_bytes(1, 48000, 4800, declared=0),  # streamed, length left at 0
        "fake.png": b"not a picture at all, just text",
        "clip.mp4": b"\0\0\0\x18ftypisom" + b"\0" * 32,
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    facts = {name: library.header_facts(tmp_path / name) for name in files}
    assert facts["a.png"] == {"width": 1344, "height": 768, "duration": None}
    assert facts["pcm.wav"] == {"width": None, "height": None, "duration": 5.04}
    assert facts["float.wav"]["duration"] == 2.0
    for name in ("adpcm.wav", "open.wav", "zero.wav", "fake.png", "clip.mp4"):
        assert facts[name] is None, name  # ffprobe decides


def test_library_probes_only_what_the_header_does_not_tell(
    client: TestClient, make_job: Job, state: Path, no_ffprobe: list[Path]
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-02T09:00:00",
        **{"image.png": "image", "take.wav": "audio", "notes.txt": "text"},
    )
    (state / job_id / "image.png").write_bytes(png_head(512, 256))
    (state / job_id / "take.wav").write_bytes(wav_bytes(1, 32000, 32000 * 3))
    items = {i["name"]: i for i in client.get("/api/library").json()}
    assert no_ffprobe == []
    assert (items["image.png"]["width"], items["image.png"]["height"]) == (512, 256)
    assert items["take.wav"]["duration"] == 3.0
    assert items["notes.txt"]["duration"] is None


# Show in Finder


def test_reveal_opens_finder_at_the_output(
    client: TestClient, make_job: Job, state: Path, opened: list[list[str]]
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.post("/api/library/reveal", json={"job": job_id, "name": "image.png"})
    assert r.status_code == 200
    assert opened == [["open", "-R", str(state / job_id / "image.png")]]


@pytest.mark.parametrize(
    ("job", "name", "status"),
    [
        ("20261102-090000-aaaa", "in_image_0.png", 400),  # an input
        ("20261102-090000-aaaa", "../other/image.png", 404),
        ("20261102-090000-aaaa", "missing.png", 404),  # not a listed output
        ("20261102-090000-aaaa", ".hidden.png", 404),
        ("../20261102-090000-aaaa", "image.png", 404),
        ("20990101-000000-ffff", "image.png", 404),  # no such job
    ],
)
def test_reveal_refuses_anything_but_a_listed_output(
    client: TestClient,
    make_job: Job,
    state: Path,
    opened: list[list[str]],
    job: str,
    name: str,
    status: int,
) -> None:
    done_job(
        make_job,
        state,
        "20261102-090000-aaaa",
        "2026-11-02T09:00:00",
        **{"image.png": "image"},
    )
    r = client.post("/api/library/reveal", json={"job": job, "name": name})
    assert r.status_code == status
    assert opened == []


def test_reveal_refuses_a_symlinked_output(
    client: TestClient,
    make_job: Job,
    state: Path,
    tmp_path: Path,
    opened: list[list[str]],
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    (state / job_id / "image.png").unlink()
    (state / job_id / "image.png").symlink_to(outside)
    r = client.post("/api/library/reveal", json={"job": job_id, "name": "image.png"})
    assert r.status_code == 404
    assert opened == []


def test_reveal_from_another_site_is_refused(
    client: TestClient, make_job: Job, state: Path, opened: list[list[str]]
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.post(
        "/api/library/reveal",
        json={"job": job_id, "name": "image.png"},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert opened == []


# Deleting one output


def test_delete_output_removes_the_file_and_the_listing(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-02T09:00:00",
        **{
            "take_1.wav": "audio",
            "take_2.wav": "audio",
        },
    )
    r = client.delete(f"/api/library/{job_id}/take_1.wav")
    assert r.status_code == 200
    assert not (state / job_id / "take_1.wav").exists()
    assert (state / job_id / "take_2.wav").exists()
    assert (state / job_id / "in_image_0.png").exists()
    saved = json.loads((state / job_id / "job.json").read_text())
    assert [o["name"] for o in saved["outputs"]] == ["take_2.wav"]
    assert [o["name"] for o in server.jobs[job_id]["outputs"]] == ["take_2.wav"]
    assert [i["name"] for i in client.get("/api/library").json()] == ["take_2.wav"]
    assert client.delete(f"/api/library/{job_id}/take_1.wav").status_code == 404


def test_delete_output_refuses_inputs(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.delete(f"/api/library/{job_id}/in_image_0.png")
    assert r.status_code == 400
    assert (state / job_id / "in_image_0.png").exists()


def test_delete_output_refuses_a_running_job(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    outputs = [{"url": f"/files/{job_id}/a.png", "kind": "image", "name": "a.png"}]
    make_job(job_id, "running", outputs=outputs)
    (state / job_id / "a.png").write_bytes(b"x")
    r = client.delete(f"/api/library/{job_id}/a.png")
    assert r.status_code == 409
    assert (state / job_id / "a.png").exists()


def test_delete_output_from_another_site_is_refused(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261102-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-02T09:00:00", **{"image.png": "image"})
    r = client.delete(
        f"/api/library/{job_id}/image.png", headers={"origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert (state / job_id / "image.png").exists()


# Thumbnails

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg is not installed",
)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def size_of(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    w, h = out.strip().split(",")[:2]
    return int(w), int(h)


@needs_ffmpeg
def test_thumbnails_of_an_image_and_a_video_are_made_once(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261101-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-01T09:00:00",
        **{"image.png": "image", "video.mp4": "video"},
    )
    folder = state / job_id
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=s=768x1344",
        "-frames:v",
        "1",
        str(folder / "image.png"),
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=s=1024x576:d=1",
        "-pix_fmt",
        "yuv420p",
        str(folder / "video.mp4"),
    )
    r = client.get(f"/api/library/thumbs/{job_id}/image.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    thumb = folder / ".thumbs" / "image.png.jpg"
    assert r.content == thumb.read_bytes() and size_of(thumb) == (366, 640)
    made = thumb.stat().st_mtime_ns
    assert client.get(f"/api/library/thumbs/{job_id}/image.png").status_code == 200
    assert thumb.stat().st_mtime_ns == made  # kept, not made again
    r = client.get(f"/api/library/thumbs/{job_id}/video.mp4")
    assert r.status_code == 200 and size_of(folder / ".thumbs" / "video.mp4.jpg") == (
        640,
        360,
    )
    # the folder is never an output, and deleting the output deletes its thumbnail
    assert [o["name"] for o in server.outputs(folder, {})] == ["image.png", "video.mp4"]
    r = client.delete(
        f"/api/library/{job_id}/image.png", headers={"Origin": "http://127.0.0.1:7870"}
    )
    assert r.status_code == 200 and not thumb.exists()


@needs_ffmpeg
def test_two_requests_for_one_new_thumbnail_both_get_it(tmp_path: Path) -> None:
    src = tmp_path / "image.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=1024x576", "-frames:v", "1", str(src))
    dst = tmp_path / ".thumbs" / "image.png.jpg"
    errors: list[Exception] = []

    def make() -> None:
        try:
            library.make_thumb(src, dst, "image")
        except Exception as e:  # noqa: BLE001 - collected for the assert
            errors.append(e)

    threads = [threading.Thread(target=make) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and size_of(dst) == (640, 360)
    assert [p.name for p in dst.parent.iterdir()] == ["image.png.jpg"]  # no temporary file left


@needs_ffmpeg
def test_a_thumbnail_is_made_again_for_a_newer_output(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261101-090000-aaaa"
    done_job(make_job, state, job_id, "2026-11-01T09:00:00", **{"image.png": "image"})
    src = state / job_id / "image.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=64x48", "-frames:v", "1", str(src))
    assert client.get(f"/api/library/thumbs/{job_id}/image.png").status_code == 200
    thumb = state / job_id / ".thumbs" / "image.png.jpg"
    assert size_of(thumb) == (64, 48)  # a small image is not made larger
    os.utime(thumb, ns=(0, 0))
    assert client.get(f"/api/library/thumbs/{job_id}/image.png").status_code == 200
    assert thumb.stat().st_mtime_ns > 0


def test_thumbnails_only_for_listed_image_and_video_outputs(
    client: TestClient, make_job: Job, state: Path
) -> None:
    job_id = "20261101-090000-aaaa"
    done_job(
        make_job,
        state,
        job_id,
        "2026-11-01T09:00:00",
        **{"music.wav": "audio", "broken.png": "image"},
    )
    get = lambda name: client.get(f"/api/library/thumbs/{job_id}/{name}").status_code
    assert get("music.wav") == 404  # audio has no thumbnail
    assert (
        get("broken.png") == 404
    )  # ffmpeg cannot read it: the page shows the output itself
    assert get("in_image_0.png") == 400  # an input
    assert get("other.png") == 404
    assert client.get(f"/api/library/thumbs/nope/{'x.png'}").status_code == 404
    assert not (state / job_id / ".thumbs" / "broken.png.jpg").exists()
