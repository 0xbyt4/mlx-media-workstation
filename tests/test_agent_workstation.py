"""The Workstation agent tools through a fake port: list tools, job status, Library search, create_job with input
files, list and wait for jobs, cancel a job and transcribe speech."""

import threading
import time
from pathlib import Path

import pytest

from app.agent import Context, Registry, Risk, Settings, build_registry
from tests.fake_workstation import FakeWorkstation


@pytest.fixture
def port() -> FakeWorkstation:
    return FakeWorkstation()


@pytest.fixture
def reg(port: FakeWorkstation) -> Registry:
    return build_registry(port, Settings())


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    return Context("c1", None, threading.Event(), tmp_path)


def test_list_tools_shows_ids_fields_and_install_state(
    reg: Registry, ctx: Context
) -> None:
    text = reg.call("list_workstation_tools", {}, ctx).content
    assert "image.klein: FLUX.3 Klein (Image), installed." in text
    assert "prompt (textarea, required)" in text
    assert "size (one of 768x1344 | 1024x1024, default 768x1344)" in text
    assert "steps (number, 1..50, default 4)" in text
    assert "music.ace: ACE-Step (Music), NOT installed." in text
    # file fields are offered with the kinds they take, and the reference forms once
    assert "image (file, media, required)" in text
    assert "references (files, image, up to 10, required)" in text
    assert "A file field takes a file reference" in text and '"work:<path>"' in text


def test_job_status(reg: Registry, ctx: Context) -> None:
    text = reg.call("job_status", {"job_id": "20261103-120000-abcd"}, ctx).content
    assert "status: running" in text
    assert "stage: Denoising" in text and "progress: 42%" in text
    assert "about 31 s left" in text
    done = reg.call("job_status", {"job_id": "20261103-110000-beef"}, ctx).content
    assert "output: image.png (image)" in done
    assert "prompt: A red fox in the snow at dusk" in done  # what the output shows
    r = reg.call("job_status", {"job_id": "nope"}, ctx)
    assert r.status == "error" and "No job" in r.content


def test_search_library(reg: Registry, ctx: Context) -> None:
    text = reg.call("search_library", {"query": "fox"}, ctx).content
    assert "image.png (image), job 20261103-110000-beef" in text
    assert "text: A red fox in the snow at dusk" in text
    assert (
        "No outputs match" in reg.call("search_library", {"query": "zz"}, ctx).content
    )


def test_create_job_queues_and_returns_at_once(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    r = reg.call(
        "create_job",
        {
            "tool": "image.klein",
            "fields": {"prompt": "a fox", "steps": 8.0, "size": "1024x1024"},
        },
        ctx,
    )
    assert r.status == "ok"
    assert "Queued job 20261103-130000-0001 (FLUX.3 Klein)" in r.content
    assert "Do not wait" in r.content
    # values go to the port as form strings
    assert port.created == [
        ("image.klein", {"prompt": "a fox", "steps": "8", "size": "1024x1024"})
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"tool": "image.nope"}, "Unknown tool 'image.nope'"),
        ({"tool": "music.ace"}, "not installed"),
        ({"tool": "video.i2v", "fields": {"prompt": "x"}}, "needs a file in its field"),
        ({"tool": "image.klein", "fields": {"colour": "red"}}, "no field 'colour'"),
        ({"tool": "image.klein", "fields": {"steps": 999}}, "The job was refused"),
    ],
)
def test_create_job_refusals(
    reg: Registry, ctx: Context, port: FakeWorkstation, args: dict, message: str
) -> None:
    r = reg.call("create_job", args, ctx)
    assert r.status == "error" and message in r.content
    assert port.created == []


def test_create_job_explains_itself(reg: Registry) -> None:
    line = reg.get("create_job").describe(
        {"tool": "image.klein", "fields": {"prompt": "a fox"}}
    )
    assert "image.klein" in line and "'a fox'" in line


# ---------- input files, waits, jobs, cancel, transcription ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


@pytest.fixture
def files(tmp_path: Path) -> FakeWorkstation:
    """A port with a finished job's output on disk, a work folder and a granted folder with some files (the secret is
    dummy text)."""
    port = FakeWorkstation(tmp_path / "out", transcribe=True)
    (tmp_path / "out" / "20261103-110000-beef").mkdir(parents=True)
    (tmp_path / "out" / "20261103-110000-beef" / "image.png").write_bytes(PNG + b"lib")
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "chart.png").write_bytes(PNG + b"work")
    granted = tmp_path / "granted"
    (granted / "shots").mkdir(parents=True)
    (granted / "shots" / "face.jpg").write_bytes(b"jpeg face")
    (granted / "talk.wav").write_bytes(b"RIFF talk")
    (granted / ".hidden.png").write_bytes(PNG)
    (granted / "server.pem").write_bytes(b"dummy")
    (tmp_path / "outside.png").write_bytes(PNG)
    (granted / "link.png").symlink_to(tmp_path / "outside.png")
    return port


@pytest.fixture
def fctx(tmp_path: Path) -> Context:
    return Context("c1", tmp_path / "granted", threading.Event(), tmp_path / "ws")


def queue(reg: Registry, ctx: Context, **fields: object):
    return reg.call("create_job", {"tool": "video.i2v", "fields": fields}, ctx)


@pytest.mark.parametrize(
    ("ref", "name", "data"),
    [
        ("library:20261103-110000-beef/image.png", "image.png", PNG + b"lib"),
        ("work:chart.png", "chart.png", PNG + b"work"),
        ("folder:shots/face.jpg", "face.jpg", b"jpeg face"),
    ],
)
def test_create_job_copies_an_input_from_each_source(
    files: FakeWorkstation, fctx: Context, ref: str, name: str, data: bytes
) -> None:
    reg = build_registry(files, Settings())
    r = queue(reg, fctx, prompt="it moves", image=ref)
    assert r.status == "ok", r.content
    assert f"with the input files image = {ref}" in r.content
    assert files.inputs == [{"image": [(name, data)]}]
    # the job remembers the chat, and the page gets the id for its job card
    assert r.job == "20261103-130000-0001"
    assert files.jobs[r.job]["chat"] == "c1"


def test_a_files_field_takes_a_list(files: FakeWorkstation, fctx: Context) -> None:
    reg = build_registry(files, Settings())
    refs = ["library:20261103-110000-beef/image.png", "folder:shots/face.jpg"]
    r = reg.call(
        "create_job",
        {"tool": "image.klein_edit", "fields": {"prompt": "merge", "references": refs}},
        fctx,
    )
    assert r.status == "ok", r.content
    assert [n for n, _ in files.inputs[0]["references"]] == ["image.png", "face.jpg"]


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("face.jpg", "not a file reference"),
        ("disk:/etc/hosts", "not a file reference"),
        ("library:20261103-110000-beef/nope.png", "not an output of a finished job"),
        ("library:20261103-120000-abcd/image.png", "not an output of a finished job"),
        ("folder:.hidden.png", "hidden or secret"),
        ("folder:server.pem", "hidden or secret"),
        ("folder:../outside.png", "outside the granted folder"),
        ("folder:/etc/hosts", "outside the granted folder"),
        ("folder:link.png", "outside the granted folder"),
        ("folder:shots", "a folder, not a file"),
        ("folder:missing.png", "no such file"),
        ("work:../granted/talk.wav", "outside the work folder"),
    ],
)
def test_create_job_refuses_bad_inputs(
    files: FakeWorkstation, fctx: Context, ref: str, message: str
) -> None:
    reg = build_registry(files, Settings())
    r = queue(reg, fctx, prompt="x", image=ref)
    assert r.status == "error" and message in r.content, r.content
    assert files.created == []


def test_folder_inputs_need_a_granted_folder(
    files: FakeWorkstation, tmp_path: Path
) -> None:
    reg = build_registry(files, Settings())
    ctx = Context("c1", None, threading.Event(), tmp_path / "ws")
    r = queue(reg, ctx, prompt="x", image="folder:talk.wav")
    assert r.status == "error" and "No folder is granted" in r.content


def test_work_inputs_need_a_work_folder(files: FakeWorkstation, tmp_path: Path) -> None:
    reg = build_registry(files, Settings())
    ctx = Context("c1", None, threading.Event(), tmp_path / "no-ws")
    r = queue(reg, ctx, prompt="x", image="work:chart.png")
    assert r.status == "error" and "work folder is empty" in r.content


def test_a_file_field_value_must_be_references(
    files: FakeWorkstation, fctx: Context
) -> None:
    reg = build_registry(files, Settings())
    r = queue(reg, fctx, prompt="x", image=5)
    assert r.status == "error" and "give a file reference" in r.content
    r = queue(reg, fctx, prompt=["a", "b"], image="work:chart.png")
    assert r.status == "error" and "give one value" in r.content


def test_the_approval_line_lists_the_input_files(files: FakeWorkstation) -> None:
    line = (
        build_registry(files, Settings())
        .get("create_job")
        .describe(
            {"tool": "video.i2v", "fields": {"prompt": "go", "image": "work:chart.png"}}
        )
    )
    assert "prompt='go'" in line and "Input files: image = work:chart.png." in line


def test_outputs_show_their_file_reference(reg: Registry, ctx: Context) -> None:
    done = reg.call("job_status", {"job_id": "20261103-110000-beef"}, ctx).content
    assert "file reference library:20261103-110000-beef/image.png" in done
    found = reg.call("search_library", {"query": "fox"}, ctx).content
    assert "file reference library:20261103-110000-beef/image.png" in found


def test_list_jobs(reg: Registry, ctx: Context, port: FakeWorkstation) -> None:
    port.jobs["20261103-120000-abcd"]["chat"] = "c1"
    lines = reg.call("list_jobs", {}, ctx).content.splitlines()
    assert lines[0].startswith("- 20261103-120000-abcd FLUX.3 Klein (")
    assert "queued by this chat" in lines[0]
    assert "running; 42%; about 31 s left" in lines[0]
    assert "done; outputs: image.png" in lines[1] and "this chat" not in lines[1]
    port.jobs.clear()
    assert reg.call("list_jobs", {}, ctx).content == "No jobs."


def test_wait_for_job_returns_when_the_job_ends(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    port.finish["20261103-120000-abcd"] = {
        "status": "done",
        "progress": None,
        "outputs": [{"name": "image.png", "kind": "image"}],
    }
    r = reg.call("wait_for_job", {"job_id": "20261103-120000-abcd"}, ctx)
    assert r.status == "ok" and "status: done" in r.content
    assert "Still" not in r.content
    assert port.waits == [("20261103-120000-abcd", 120.0)]
    # a job that has ended is shown at once
    again = reg.call("wait_for_job", {"job_id": "20261103-110000-beef"}, ctx)
    assert "status: done" in again.content and len(port.waits) == 1
    missing = reg.call("wait_for_job", {"job_id": "nope"}, ctx)
    assert missing.status == "error" and "No job" in missing.content


def test_wait_for_job_keeps_time_for_the_answer(
    reg: Registry, port: FakeWorkstation, tmp_path: Path
) -> None:
    soon = Context("c1", None, threading.Event(), tmp_path, time.monotonic() + 30)
    r = reg.call("wait_for_job", {"job_id": "20261103-120000-abcd"}, soon)
    assert "status: running" in r.content and "no time left to wait" in r.content
    assert port.waits == []
    later = Context("c1", None, threading.Event(), tmp_path, time.monotonic() + 200)
    r = reg.call(
        "wait_for_job", {"job_id": "20261103-120000-abcd", "seconds": 300}, later
    )
    ((_, seconds),) = port.waits
    assert 130 < seconds <= 140  # 200 s left minus the reserve for the next steps
    assert "Still running after" in r.content


def test_wait_for_job_names_a_job_that_starts_after_the_answer(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    port.finish["20261103-120000-abcd"] = {"status": "queued", "needs_memory": True}
    r = reg.call("wait_for_job", {"job_id": "20261103-120000-abcd"}, ctx)
    assert r.status == "ok" and "starts after this answer ends" in r.content


def test_wait_for_job_ends_at_stop(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    port.block = True
    threading.Timer(0.1, ctx.abort.set).start()
    t0 = time.monotonic()
    r = reg.call(
        "wait_for_job", {"job_id": "20261103-120000-abcd", "seconds": 300}, ctx
    )
    assert r.status == "cancelled" and time.monotonic() - t0 < 5


def test_wait_for_job_is_capped(reg: Registry, ctx: Context) -> None:
    r = reg.call("wait_for_job", {"job_id": "x", "seconds": 301}, ctx)
    assert r.status == "error" and "at most 300" in r.content


def test_cancel_job_only_cancels_this_chats_jobs(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    other = reg.call("cancel_job", {"job_id": "20261103-120000-abcd"}, ctx)
    assert other.status == "error" and "not queued by this chat" in other.content
    assert "Jobs view" in other.content and port.cancelled == []
    mine = reg.call(
        "create_job", {"tool": "image.klein", "fields": {"prompt": "a"}}, ctx
    )
    assert mine.status == "ok"
    r = reg.call("cancel_job", {"job_id": mine.job}, ctx)
    assert r.status == "ok" and "is cancelled" in r.content
    assert port.cancelled == [mine.job]
    again = reg.call("cancel_job", {"job_id": mine.job}, ctx)
    assert "already cancelled" in again.content and port.cancelled == [mine.job]
    other_chat = Context("c2", None, threading.Event(), ctx.work_dir)
    r = reg.call("cancel_job", {"job_id": mine.job}, other_chat)
    assert r.status == "error" and "not queued by this chat" in r.content
    missing = reg.call("cancel_job", {"job_id": "nope"}, ctx)
    assert missing.status == "error" and "No job" in missing.content
    assert reg.get("cancel_job").risk == Risk.R2


def test_transcribe_is_refused_when_it_is_not_installed(
    reg: Registry, ctx: Context, port: FakeWorkstation
) -> None:
    r = reg.call("transcribe_audio", {"audio": "work:talk.wav"}, ctx)
    assert r.status == "error" and "Speech recognition is not installed" in r.content
    assert "Models" in r.content and port.created == []


def finish_transcript(port: FakeWorkstation, job_id: str, text: str) -> None:
    (port.root / job_id).mkdir(parents=True, exist_ok=True)
    (port.root / job_id / "transcript.txt").write_text(text)
    port.finish[job_id] = {
        "status": "done",
        "outputs": [{"name": "transcript.txt", "kind": "text"}],
    }


def test_transcribe_queues_a_job_and_returns_the_text(
    files: FakeWorkstation, fctx: Context
) -> None:
    reg = build_registry(files, Settings())
    finish_transcript(
        files, "20261103-130000-0001", "Language: German\n\n[0:00-0:03] Guten Tag.\n"
    )
    r = reg.call(
        "transcribe_audio", {"audio": "folder:talk.wav", "language": "german"}, fctx
    )
    assert r.status == "ok", r.content
    assert "[0:00-0:03] Guten Tag." in r.content and "Language: German" in r.content
    assert files.created == [("voice.transcribe", {"language": "German"})]
    assert files.inputs == [{"audio": [("talk.wav", b"RIFF talk")]}]
    assert r.job == "20261103-130000-0001"
    # the transcript is text from outside the app: the chat is tainted after it
    assert reg.get("transcribe_audio").untrusted_output
    assert reg.get("transcribe_audio").risk == Risk.R2


def test_transcribe_still_running_then_read_by_job_id(
    files: FakeWorkstation, fctx: Context
) -> None:
    reg = build_registry(files, Settings())
    r = reg.call("transcribe_audio", {"audio": "folder:talk.wav", "seconds": 5}, fctx)
    assert r.status == "ok" and "is queued" in r.content
    assert "job_id=20261103-130000-0001" in r.content
    assert files.created == [("voice.transcribe", {"language": "auto"})]
    finish_transcript(
        files, "20261103-130000-0001", "Language: English\n\n[0:00-0:02] Hi.\n"
    )
    r = reg.call("transcribe_audio", {"job_id": "20261103-130000-0001"}, fctx)
    assert r.status == "ok" and "[0:00-0:02] Hi." in r.content
    assert len(files.created) == 1  # no second job


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "either audio"),
        ({"audio": "folder:talk.wav", "job_id": "x"}, "either audio"),
        ({"audio": "folder:talk.wav", "language": "Klingon"}, "language: one of auto"),
        ({"job_id": "20261103-110000-beef"}, "not a transcription job"),
        ({"audio": "folder:server.pem"}, "hidden or secret"),
        ({"audio": "talk.wav"}, "not a file reference"),
    ],
)
def test_transcribe_refusals(
    files: FakeWorkstation, fctx: Context, args: dict, message: str
) -> None:
    r = build_registry(files, Settings()).call("transcribe_audio", args, fctx)
    assert r.status == "error" and message in r.content, r.content
    assert files.created == []


def test_transcribe_reports_a_failed_job(files: FakeWorkstation, fctx: Context) -> None:
    reg = build_registry(files, Settings())
    files.finish["20261103-130000-0001"] = {"status": "failed", "error": "No sound."}
    r = reg.call("transcribe_audio", {"audio": "folder:talk.wav"}, fctx)
    assert r.status == "error" and "failed: No sound." in r.content


def test_a_long_job_prompt_is_cut_in_job_status() -> None:
    from app.agent import ws_tools

    text = ws_tools.format_job(
        {"id": "j", "status": "done", "params": {"text": "word " * 200}}
    )
    line = next(x for x in text.splitlines() if x.startswith("prompt: "))
    assert len(line) == len("prompt: ") + ws_tools.MAX_JOB_TEXT + 3 and line.endswith(
        "..."
    )
    assert "prompt:" not in ws_tools.format_job(
        {"id": "j", "status": "done", "params": {}}
    )
