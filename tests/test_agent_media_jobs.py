"""The server's side of the agent's media tools (app/server.py Workstation): input files copied as an upload is, with
the upload's checks and a type check; the chat that queued a job; list, wait, cancel and output files; and no wait for
a job that needs the chat model's memory. The worker thread never runs here, so a queued job stays queued."""

import shutil
import threading
import time
from pathlib import Path

import pytest

from app import llm, models, server, tools
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
        ("photo.avif", "takes image files"),  # not a media type the app serves
    ):
        with pytest.raises(ValueError, match=message):
            port.create_job("video.i2v", {"prompt": "x"}, {"image": [given(name)]})
    # iPhone photos and GIFs are images: the job converts a HEIC with sips and takes a GIF's first frame
    for name, kept in (
        ("IMG_0001.HEIC", "in_image_0.heic"),
        ("loop.gif", "in_image_0.gif"),
    ):
        job_id = port.create_job("video.i2v", {"prompt": "x"}, {"image": [given(name)]})
        assert server.jobs[job_id]["files"] == {"image": [kept]}
        server.jobs.pop(job_id)
        shutil.rmtree(state / job_id)
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


def test_a_transcript_from_the_library_is_burned_into_a_library_video(
    port: server.Workstation, state: Path, make_job, media: FakeMedia, tmp_path: Path
) -> None:
    """Transcribe's transcript.srt and a finished video reach Add Subtitles by their Library references."""
    srt = "1\n00:00:00,500 --> 00:00:02,000\nMerhaba dünya\n".encode()
    make_job(
        "20261105-100000-aaaa",
        "done",
        outputs=[{"name": "transcript.srt", "kind": "text", "url": ""}],
    )
    (state / "20261105-100000-aaaa" / "transcript.srt").write_bytes(srt)
    make_job(
        "20261105-100000-bbbb",
        "done",
        outputs=[{"name": "video.mp4", "kind": "video", "url": ""}],
    )
    (state / "20261105-100000-bbbb" / "video.mp4").write_bytes(b"video")
    reg = build_registry(port, Settings())
    ctx = Context("c1", None, threading.Event(), tmp_path / "ws")
    fields = {
        "video": "library:20261105-100000-bbbb/video.mp4",
        "subtitles": "library:20261105-100000-aaaa/transcript.srt",
        "burn": "box",
        "burn_place": "top",
    }
    r = reg.call("create_job", {"tool": "edit.subtitle", "fields": fields}, ctx)
    assert r.status == "ok", r.content
    job = server.jobs[r.job]
    assert job["files"] == {
        "video": ["in_video_0.mp4"],
        "subtitles": ["in_subtitles_0.srt"],
    }
    assert (state / r.job / "in_subtitles_0.srt").read_bytes() == srt
    assert (job["params"]["burn"], job["params"]["burn_place"]) == ("box", "top")
    assert server.lane_of(job) == "light"


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


def test_wait_job_lends_the_turn_s_lease_to_a_job_that_needs_the_memory(
    port: server.Workstation, make_job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a turn the wait lends the memory lease, so the job's claim can unload the model and the job runs; the
    turn's next request holds the memory again (make a song, then mix it under a video, in one answer)."""
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    job = make_job("20261103-090000-vvvv", "queued", tool="video.t2v", order=1)
    held_during_wait = []

    def job_runs() -> None:
        with llm.room:
            held_during_wait.append(llm.holding())
        job.update(status="done")

    with llm.session() as lease:
        assert lease.held
        threading.Timer(0.3, job_runs).start()
        t0 = time.monotonic()
        got = port.wait_job(job["id"], 30, threading.Event())
        assert got["status"] == "done" and "needs_memory" not in got
        assert time.monotonic() - t0 < 5
        assert held_during_wait == [False]
        assert not lease.held  # until the turn's next request gets the memory


def test_wait_job_waits_for_a_light_job_behind_a_big_job(
    port: server.Workstation, make_job, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A light job (edit.mix) does not wait for the model jobs, so the wait goes on."""
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    make_job("20261103-090000-vvvv", "queued", tool="video.t2v", order=1)
    mix = make_job("20261103-100000-mmmm", "queued", tool="edit.mix", order=2)
    threading.Timer(0.2, lambda: mix.update(status="done")).start()
    job = port.wait_job(mix["id"], 30, threading.Event())
    assert "needs_memory" not in job and job["status"] == "done"


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


def test_a_field_can_take_file_suffixes_and_several_kinds(
    port: server.Workstation, state: Path, media: FakeMedia
) -> None:
    """Add Subtitles takes a .srt or .vtt file (a text kind, named by its suffix); Join clips takes videos and images."""
    media.seconds = {"in_video_0.mp4": 5.0, "in_clips_0.mp4": 2.0}
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    job_id = port.create_job(
        "edit.subtitle",
        {"language": "eng"},
        {"video": [given("clip.mp4")], "subtitles": [given("Talk.SRT", srt)]},
    )
    assert server.jobs[job_id]["files"] == {
        "video": ["in_video_0.mp4"],
        "subtitles": ["in_subtitles_0.srt"],
    }
    assert server.lane_of(server.jobs[job_id]) == "light"
    for name in ("transcript.txt", "still.png"):
        with pytest.raises(ValueError, match=r"takes \.srt or \.vtt files"):
            port.create_job(
                "edit.subtitle",
                {},
                {"video": [given("clip.mp4")], "subtitles": [given(name, srt)]},
            )
    # a bad subtitle file is refused when the job is made
    with pytest.raises(ValueError, match="has no time line"):
        port.create_job(
            "edit.subtitle",
            {},
            {
                "video": [given("clip.mp4")],
                "subtitles": [given("a.vtt", b"WEBVTT\n\nhi\n")],
            },
        )
    job_id = port.create_job(
        "edit.concat", {}, {"clips": [given("a.mp4"), given("b.png")]}
    )
    assert server.jobs[job_id]["files"] == {
        "clips": ["in_clips_0.mp4", "in_clips_1.png"]
    }
    with pytest.raises(ValueError, match="takes image or video files"):
        port.create_job("edit.concat", {}, {"clips": [given("a.mp4"), given("b.wav")]})
