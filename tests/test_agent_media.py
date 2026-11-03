"""The server's side of the agent's media tools (app/server.py Workstation): input files copied as an upload is, with
the upload's checks and a type check; the chat that queued a job; list, wait, cancel and output files; and no wait for
a job that needs the chat model's memory. The worker thread never runs here, so a queued job stays queued."""

import threading
import time
from pathlib import Path

import pytest

from app import models, server, tools
from app.agent import Context, Settings, build_registry
from app.agent.ports import InputFile
from tests.conftest import FakeMedia

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


@pytest.fixture
def port(state: Path, monkeypatch: pytest.MonkeyPatch) -> server.Workstation:
    monkeypatch.setattr(tools, "REQUIRES", {})
    monkeypatch.setattr(server, "run_job", lambda job: pytest.fail("a job ran"))
    return server.Workstation()


def given(name: str, data: bytes = PNG, size: int | None = None) -> InputFile:
    return InputFile(
        name, len(data) if size is None else size, lambda p: p.write_bytes(data)
    )


def test_create_job_copies_the_inputs_like_an_upload(
    port: server.Workstation, state: Path
) -> None:
    job_id = port.create_job(
        "video.i2v", {"prompt": "it moves"}, {"image": [given("My Shot.PNG")]}, "c1"
    )
    job = server.jobs[job_id]
    assert job["files"] == {"image": ["in_image_0.png"]}
    assert (state / job_id / "in_image_0.png").read_bytes() == PNG
    assert job["chat"] == "c1" and job["params"]["prompt"] == "it moves"
    assert port.job_status(job_id)["chat"] == "c1"
    # a job from the page has no chat
    other = port.create_job("image.klein", {"prompt": "x"})
    assert "chat" not in server.jobs[other] and port.job_status(other)["chat"] is None


def test_the_input_type_must_match_the_field(
    port: server.Workstation, state: Path, media: FakeMedia
) -> None:
    for name, message in (
        ("notes.txt", "Start image takes image files; notes.txt is not one."),
        ("clip.mp4", "takes image files"),
        ("photo.heic", "takes image files"),  # not a media type the app serves
    ):
        with pytest.raises(ValueError, match=message):
            port.create_job("video.i2v", {"prompt": "x"}, {"image": [given(name)]})
    # a field that takes audio or video
    assert port.create_job("voice.transcribe", {}, {"audio": [given("talk.mp4")]})
    with pytest.raises(ValueError, match="takes audio or video files"):
        port.create_job("voice.transcribe", {}, {"audio": [given("still.png")]})
    assert len(list(state.iterdir())) == 1  # no half-made job folders


def test_the_upload_checks_apply(port: server.Workstation, state: Path) -> None:
    with pytest.raises(ValueError, match="file too large"):
        port.create_job(
            "video.i2v",
            {"prompt": "x"},
            {"image": [given("big.png", size=server.MAX_UPLOAD + 1)]},
        )
    with pytest.raises(ValueError, match="at most 1 file"):
        port.create_job(
            "video.i2v", {"prompt": "x"}, {"image": [given("a.png"), given("b.png")]}
        )
    with pytest.raises(ValueError, match="no file field 'video'"):
        port.create_job("video.i2v", {"prompt": "x"}, {"video": [given("a.mp4")]})
    with pytest.raises(ValueError, match="needs an uploaded file"):
        port.create_job("video.i2v", {"prompt": "x"})
    with pytest.raises(ValueError, match="Prompt is required"):
        port.create_job("video.i2v", {}, {"image": [given("a.png")]})
    assert server.jobs == {} and list(state.iterdir()) == []


def test_a_library_output_reaches_a_job_through_the_agent_tool(
    port: server.Workstation, state: Path, make_job, tmp_path: Path
) -> None:
    make_job(
        "20261103-100000-aaaa",
        "done",
        outputs=[{"name": "image.png", "kind": "image", "url": ""}],
    )
    (state / "20261103-100000-aaaa" / "image.png").write_bytes(PNG)
    reg = build_registry(port, Settings())
    ctx = Context("c1", None, threading.Event(), tmp_path / "ws")
    r = reg.call(
        "create_job",
        {
            "tool": "video.i2v",
            "fields": {
                "prompt": "go",
                "image": "library:20261103-100000-aaaa/image.png",
            },
        },
        ctx,
    )
    assert r.status == "ok", r.content
    assert (state / r.job / "in_image_0.png").read_bytes() == PNG
    assert server.jobs[r.job]["chat"] == "c1"
    # only a listed output of a done job is a Library file
    assert port.output_file("20261103-100000-aaaa", "image.png") == (
        state / "20261103-100000-aaaa" / "image.png"
    )
    assert port.output_file("20261103-100000-aaaa", "in_image_0.png") is None
    assert port.output_file("20261103-100000-aaaa", "../job.json") is None
    assert port.output_file(r.job, "image.png") is None  # queued
    assert port.output_file("nope", "image.png") is None


def test_list_jobs_shows_the_active_ones_and_the_newest_finished(
    port: server.Workstation, make_job
) -> None:
    for n in range(5):
        make_job(f"20261103-10000{n}-aaaa", "done", created=f"2026-11-03T10:00:0{n}")
    make_job("20261103-090000-bbbb", "queued", created="2026-11-03T09:00:00")
    ids = [j["id"] for j in port.list_jobs(3)]
    assert ids == [
        "20261103-100004-aaaa",
        "20261103-100003-aaaa",
        "20261103-090000-bbbb",
    ]


def test_wait_job_returns_when_the_job_ends_or_at_stop(
    port: server.Workstation, make_job
) -> None:
    job = make_job("20261103-100000-aaaa", "running", started="2026-11-03T10:00:00")
    threading.Timer(0.2, lambda: job.update(status="done")).start()
    t0 = time.monotonic()
    assert port.wait_job(job["id"], 30, threading.Event())["status"] == "done"
    assert time.monotonic() - t0 < 5
    job["status"] = "running"
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    t0 = time.monotonic()
    assert port.wait_job(job["id"], 30, stop)["status"] == "running"
    assert time.monotonic() - t0 < 5
    t0 = time.monotonic()
    assert port.wait_job(job["id"], 0.3, threading.Event())["status"] == "running"
    assert 0.25 < time.monotonic() - t0 < 5
    assert port.wait_job("nope", 30, threading.Event()) is None


def test_wait_job_does_not_wait_for_a_job_that_needs_the_model_s_memory(
    port: server.Workstation, make_job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Such a job waits for the turn's memory lease, so a wait inside the turn could only time out."""
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    make_job("20261103-090000-vvvv", "queued", tool="video.t2v", order=1)
    make_job("20261103-100000-aaaa", "queued", tool="voice.transcribe", order=2)
    t0 = time.monotonic()
    job = port.wait_job("20261103-100000-aaaa", 30, threading.Event())
    assert job["needs_memory"] is True and time.monotonic() - t0 < 1
    assert port.wait_job("20261103-090000-vvvv", 30, threading.Event())["needs_memory"]
    # alone in the queue, the small job fits next to the model: the wait goes on
    server.jobs.pop("20261103-090000-vvvv")
    job = port.wait_job("20261103-100000-aaaa", 0.3, threading.Event())
    assert "needs_memory" not in job and job["status"] == "queued"


def test_cancel_job_through_the_port(port: server.Workstation, make_job) -> None:
    make_job("20261103-100000-aaaa", "queued")
    assert port.cancel_job("20261103-100000-aaaa") == "cancelled"
    assert server.jobs["20261103-100000-aaaa"]["status"] == "cancelled"
    assert port.cancel_job("20261103-100000-aaaa") == "cancelled"
    with pytest.raises(ValueError, match="No such job"):
        port.cancel_job("nope")


def test_the_transcribe_tool_needs_a_sound_track(
    port: server.Workstation, media: FakeMedia
) -> None:
    media.audio["in_audio_0.mp4"] = False
    with pytest.raises(ValueError, match="no sound track"):
        port.create_job("voice.transcribe", {}, {"audio": [given("silent.mp4")]})
    tool = {t["id"]: t for t in port.list_tools()}["voice.transcribe"]
    assert tool["group"] == "Voice" and tool["installed"] is True
