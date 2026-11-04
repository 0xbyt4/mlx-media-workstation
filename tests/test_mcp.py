"""The MCP server (app/mcp_server.py) and its HTTP port (app/agent/http_port.py) against the real app on a free port
(uvicorn in a thread, a temp outputs folder, no worker: a job stays queued until a test changes it), and the endpoints
they use. The MCP client is the SDK's in-process client. No test runs a model or leaves 127.0.0.1."""

import base64
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import mcp_types as types
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client

from app import chat, llm, models, server, tools
from app.agent import Context, Registry, ws_tools
from app.agent.http_port import AppUnavailable, HttpWorkstation
from app.agent.ports import MCP, InputFile
from app.agent.registry import ToolError
from app.mcp_server import build_server, host, open_path
from tests.conftest import TOKEN
from tests.fake_workstation import FakeWorkstation

Job = Callable[..., dict[str, Any]]
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
ORIGIN = {"Origin": f"http://127.0.0.1:{server.PORT}"}


def write_token(out: Path, token: str) -> None:
    path = out / ".server-token"
    path.write_text(token)
    path.chmod(0o600)


@pytest.fixture
def app_port(
    client: TestClient, state: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[int]:
    """The app on a free port of 127.0.0.1, with its token file in the temp outputs folder."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    monkeypatch.setattr(server, "ORIGINS", {f"http://127.0.0.1:{port}"})
    monkeypatch.setattr(server, "SESSION_COOKIE", f"workstation_{port}")
    write_token(state, TOKEN)
    srv = uvicorn.Server(uvicorn.Config(server.app, log_level="warning"))
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    end = time.monotonic() + 10
    while not srv.started and time.monotonic() < end:
        time.sleep(0.02)
    yield port
    srv.should_exit = True
    t.join(10)
    sock.close()


@pytest.fixture
def hp(app_port: int, state: Path) -> HttpWorkstation:
    return HttpWorkstation(app_port, state)


@pytest.fixture
def fixed_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 48 GB Mac with the chat model loaded (18 GB)."""
    mem = {"mem_total_gb": 48.0, "mem_free_gb": 20.5, "swap_used_gb": 0.5}
    monkeypatch.setattr(server, "memory", lambda: mem)
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    status = {
        "installed": True,
        "state": "ready",
        "busy": False,
        "waiting": 0,
        "media_peak_gb": 0.0,
        "model": "Qwen3.8 27B (4-bit)",
        "peak_gb": 18.0,
        "idle_stop_s": 600.0,
    }
    monkeypatch.setattr(llm, "status", lambda: status)


def image(tmp_path: Path, name: str = "ref.png") -> Path:
    path = tmp_path / name
    path.write_bytes(PNG)
    return path


def finish(state: Path, job_id: str, name: str = "image.png") -> None:
    """The job is done with one image output."""
    (state / job_id / name).write_bytes(PNG)
    job = server.jobs[job_id]
    job.update(status="done", outputs=[{"name": name, "kind": "image"}], elapsed=3.0)


def run(coro_fn: Callable[[Client], Any], hp: HttpWorkstation, **kw: Any) -> Any:
    """Connect the SDK's in-process client to the MCP server on this port and run coro_fn(client)."""

    async def main() -> Any:
        async with Client(build_server(hp), **kw) as c:
            return await coro_fn(c)

    return anyio.run(main)


def call(hp: HttpWorkstation, name: str, args: dict[str, Any] | None = None) -> Any:
    return run(lambda c: c.call_tool(name, args or {}), hp)


def text(result: types.CallToolResult) -> str:
    return result.content[0].text


# ---------- the endpoints the port uses ----------


def test_tools_with_their_install_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "installed" not in client.get("/api/tools").json()[0]
    monkeypatch.setattr(tools, "REQUIRES", {"image.klein": [Path("/missing")]})
    by = {t["id"]: t for t in client.get("/api/tools?installed=true").json()}
    assert by["image.klein"]["installed"] is False
    assert by["image.moss"]["installed"] is True


def test_one_job_and_a_limited_list(client: TestClient, make_job: Job) -> None:
    make_job("20261104-100000-aaaa", "done")
    make_job("20261104-110000-bbbb", "queued", chat=MCP, created="2026-11-04T11:00")
    make_job("20261104-090000-cccc", "failed", created="2026-11-04T09:00")
    job = client.get("/api/jobs/20261104-110000-bbbb").json()
    assert job["chat"] == MCP and job["position"] == 1 and job["status"] == "queued"
    assert client.get("/api/jobs/nope").status_code == 404
    ids = [j["id"] for j in client.get("/api/jobs?limit=2").json()]
    assert ids == ["20261104-110000-bbbb", "20261104-090000-cccc"]  # active, newest
    assert len(client.get("/api/jobs").json()) == 3  # without a limit: every job
    assert client.get("/api/jobs?limit=0").status_code == 422


def test_library_search(client: TestClient, make_job: Job, state: Path) -> None:
    make_job("20261104-100000-aaaa", "done", params={"prompt": "a red fox"})
    finish(state, "20261104-100000-aaaa")
    hits = client.get("/api/library/search", params={"q": "FOX"}).json()
    assert [(h["job"], h["name"]) for h in hits] == [
        ("20261104-100000-aaaa", "image.png")
    ]
    assert client.get("/api/library/search", params={"q": "owl"}).json() == []
    assert client.get("/api/library/search").status_code == 422


def test_status_shows_memory_jobs_model_and_fit(
    client: TestClient, make_job: Job, fixed_memory: None
) -> None:
    make_job("20261104-100000-aaaa", "running", started="2026-11-04T10:00:00")
    make_job("20261104-110000-bbbb", "queued", chat=MCP)
    s = client.get("/api/status").json()
    assert s["memory"] == {
        "mem_total_gb": 48.0,
        "mem_free_gb": 20.5,
        "swap_used_gb": 0.5,
        "reserve_gb": 8.0,
    }
    assert s["running"]["id"] == "20261104-100000-aaaa"
    assert [(j["id"], j["chat"], j["position"]) for j in s["queue"]] == [
        ("20261104-110000-bbbb", MCP, 1)
    ]
    assert s["chat_model"]["state"] == "ready" and s["chat_model"]["held_gb"] == 18.0
    fit = {t["id"]: t["fit"] for t in s["tools"]}
    # the queue's rule: peak + 8 GB <= 48 GB alone, and next to the 18 GB model
    assert fit["image.klein"] == "fits"  # 14 + 18 + 8
    assert fit["video.t2v"] == "unloads_chat_model"  # 35 + 18 + 8 > 48, 35 + 8 <= 48
    assert fit["voice.drama3"] == "unloads_chat_model"  # 39 + 8 = 47
    assert fit["edit.mix"] == "fits"


def test_status_fit_with_the_model_off_and_on_a_small_mac(
    client: TestClient, fixed_memory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = llm.status()
    monkeypatch.setattr(llm, "status", lambda: {**loaded, "state": "off"})
    s = client.get("/api/status").json()
    assert s["chat_model"]["held_gb"] == 0.0
    assert {t["fit"] for t in s["tools"]} == {"fits"}
    small = {"mem_total_gb": 32.0, "mem_free_gb": 10.0, "swap_used_gb": 0.0}
    monkeypatch.setattr(server, "memory", lambda: small)
    fit = {t["id"]: t["fit"] for t in client.get("/api/status").json()["tools"]}
    assert fit["voice.drama3"] == "too_big" and fit["image.klein"] == "fits"


def test_a_job_from_mcp_is_marked_and_checked_as_the_agent_s(
    client: TestClient, state: Path
) -> None:
    files = {"references": ("a.png", PNG, "image/png")}
    form = {"tool": "image.klein_edit", "prompt": "a fox", "client": MCP}
    r = client.post("/api/jobs", data=form, files=files, headers=ORIGIN)
    assert r.status_code == 200, r.text
    job = json.loads((state / r.json()["id"] / "job.json").read_text())
    assert job["chat"] == MCP and job["files"] == {
        "references": ["in_references_0.png"]
    }
    # the agent's check: a file of a type the field does not take is refused (a browser upload becomes .bin)
    bad = {"references": ("notes.txt", b"hello", "text/plain")}
    r = client.post("/api/jobs", data=form, files=bad, headers=ORIGIN)
    assert r.status_code == 400 and "takes image files" in r.json()["detail"]
    r = client.post("/api/jobs", data={**form, "client": "x"}, headers=ORIGIN)
    assert r.status_code == 400 and "Unknown client" in r.json()["detail"]
    assert len(server.jobs) == 1 and len(list(state.iterdir())) == 1  # no folder left


# ---------- the HTTP port ----------


def test_port_lists_tools_and_queues_a_job(
    hp: HttpWorkstation, state: Path, tmp_path: Path
) -> None:
    assert {t["id"] for t in hp.list_tools()} == set(tools.BY_ID)
    assert all(t["installed"] for t in hp.list_tools())
    src = image(tmp_path)
    fd = os.open(src, os.O_RDONLY)
    try:
        size = src.stat().st_size
        inp = InputFile("Fox Photo.png", size, ws_tools._copier(fd, size))
        job_id = hp.create_job(
            "image.klein_edit", {"prompt": "a fox"}, {"references": [inp]}, MCP
        )
    finally:
        os.close(fd)
    assert (state / job_id / "in_references_0.png").read_bytes() == PNG
    job = hp.job_status(job_id)
    assert job is not None and job["chat"] == MCP and job["status"] == "queued"
    assert [j["id"] for j in hp.list_jobs(5)] == [job_id]
    assert hp.job_status("nope") is None and hp.job_status("../x") is None


def test_port_refusals_come_back_as_value_errors(hp: HttpWorkstation) -> None:
    with pytest.raises(ValueError, match="Steps must be between 4 and 50"):
        hp.create_job("image.klein", {"prompt": "x", "steps": "999"}, None, MCP)
    with pytest.raises(ValueError, match="Prompt is required"):
        hp.create_job("image.klein", {}, None, MCP)
    with pytest.raises(ValueError, match="No such job"):
        hp.cancel_job("nope")


def test_port_cancel_search_and_outputs(
    hp: HttpWorkstation, make_job: Job, state: Path
) -> None:
    make_job("20261104-100000-aaaa", "queued")
    assert hp.cancel_job("20261104-100000-aaaa") == "cancelled"
    make_job("20261104-110000-bbbb", "done", params={"prompt": "a red fox"})
    finish(state, "20261104-110000-bbbb")
    assert hp.search_library("fox", 5)[0]["name"] == "image.png"
    path = hp.output_file("20261104-110000-bbbb", "image.png")
    assert path == state / "20261104-110000-bbbb" / "image.png"
    assert hp.output_file("20261104-110000-bbbb", "job.json") is None  # not listed
    assert hp.output_file("20261104-100000-aaaa", "image.png") is None  # not done
    # an output swapped for a link is not given out
    path.unlink()
    path.symlink_to(state / "20261104-110000-bbbb" / "job.json")
    assert hp.output_file("20261104-110000-bbbb", "image.png") is None


def test_port_waits_until_the_job_ends(hp: HttpWorkstation, make_job: Job) -> None:
    make_job("20261104-100000-aaaa", "queued")
    threading.Timer(
        0.3, lambda: server.jobs["20261104-100000-aaaa"].update(status="done")
    ).start()
    t0 = time.monotonic()
    job = hp.wait_job("20261104-100000-aaaa", 10, threading.Event())
    assert job is not None and job["status"] == "done"
    assert "needs_memory" not in job  # no chat turn holds a lease for this client
    assert time.monotonic() - t0 < 5


def test_port_wait_ends_at_its_time_or_at_abort(
    hp: HttpWorkstation, make_job: Job
) -> None:
    make_job("20261104-100000-aaaa", "queued")
    t0 = time.monotonic()
    job = hp.wait_job("20261104-100000-aaaa", 1.2, threading.Event())
    assert job is not None and job["status"] == "queued"
    assert 1.1 < time.monotonic() - t0 < 4
    abort = threading.Event()
    threading.Timer(0.2, abort.set).start()
    t0 = time.monotonic()
    hp.wait_job("20261104-100000-aaaa", 30, abort)
    assert time.monotonic() - t0 < 3


def test_port_status(hp: HttpWorkstation, fixed_memory: None) -> None:
    s = hp.system_status()
    assert s["memory"]["mem_total_gb"] == 48.0 and s["running"] is None
    assert s["chat_model"]["label"] == "Qwen3.8 27B (4-bit)"


def test_port_reads_the_new_token_after_a_restart(
    hp: HttpWorkstation, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hp.list_jobs(5) == []
    new = "ab" * 32  # the app started again: a new token in the server and in the file
    monkeypatch.setattr(server, "token", new)
    write_token(state, new)
    assert hp.list_jobs(5) == []


def test_port_with_a_token_the_app_does_not_take(
    hp: HttpWorkstation, state: Path
) -> None:
    write_token(state, "ef" * 32)
    with pytest.raises(AppUnavailable, match="did not take the token") as e:
        hp.list_jobs(5)
    assert "ef" * 32 not in str(e.value) and TOKEN not in str(e.value)


def test_port_when_the_app_does_not_run(state: Path, tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[
            1
        ]  # free again after the with: the connection is refused
    write_token(state, TOKEN)
    with pytest.raises(AppUnavailable, match="does not answer") as e:
        HttpWorkstation(closed, state).list_jobs(5)
    assert "start.py --browser" in str(e.value) and TOKEN not in str(e.value)
    # no token file (the app is closed, or this is another checkout)
    with pytest.raises(AppUnavailable, match="not running"):
        HttpWorkstation(closed, tmp_path).list_jobs(5)


def test_port_token_file_others_can_read_is_not_used(
    hp: HttpWorkstation, state: Path
) -> None:
    (state / ".server-token").chmod(0o644)
    with pytest.raises(AppUnavailable, match="not running"):
        hp.list_jobs(5)


def test_port_ignores_proxies(
    hp: HttpWorkstation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy in the environment would see the cookie: the port never uses one."""
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    port = HttpWorkstation(hp.port, hp.outputs)
    assert port.list_jobs(5) == []


# ---------- the MCP server ----------


def test_the_tools_are_the_shared_ones_with_hints(hp: HttpWorkstation) -> None:
    listed = run(lambda c: c.list_tools(), hp).tools
    shared = ws_tools.build(hp, host(hp))
    assert [t.name for t in listed] == [t.name for t in shared]
    assert "system_status" in [t.name for t in listed]
    for mine, theirs in zip(listed, shared, strict=True):
        assert mine.description == theirs.description
        assert mine.input_schema == theirs.parameters
        assert mine.annotations.open_world_hint is False
    hints = {t.name: t.annotations for t in listed}
    read_only = {n for n, a in hints.items() if a.read_only_hint}
    assert read_only == {
        "list_workstation_tools",
        "job_status",
        "search_library",
        "list_jobs",
        "wait_for_job",
        "system_status",
    }
    assert hints["create_job"].destructive_hint is False
    assert hints["cancel_job"].destructive_hint is True
    assert hints["cancel_job"].idempotent_hint is True
    assert hints["transcribe_audio"].read_only_hint is False
    # the MCP words: local paths, no chat, a longer wait
    by = {t.name: t for t in listed}
    assert "absolute path of a file on this Mac" in by["create_job"].description
    assert "approves" not in by["create_job"].description
    assert "that an MCP client queued" in by["cancel_job"].description
    seconds = by["wait_for_job"].input_schema["properties"]["seconds"]
    assert (seconds["default"], seconds["maximum"]) == (50, 600)


def test_the_in_app_tools_keep_their_words() -> None:
    by = {t.name: t for t in ws_tools.build(FakeWorkstation())}
    assert "The user approves it first." in by["create_job"].description
    assert '"work:<path>"' in by["create_job"].description
    assert by["cancel_job"].description == (
        "Cancel a queued or running media job that this chat queued."
    )
    seconds = by["wait_for_job"].parameters["properties"]["seconds"]
    assert (seconds["default"], seconds["maximum"]) == (120, 300)


def test_system_status_over_mcp(
    hp: HttpWorkstation, make_job: Job, fixed_memory: None
) -> None:
    make_job("20261104-110000-bbbb", "queued", chat=MCP)
    r = call(hp, "system_status")
    assert not r.is_error
    t = text(r)
    assert "Memory: 48 GB in all, 20.5 GB free, 0.5 GB of swap in use." in t
    assert "Chat model: Qwen3.8 27B (4-bit), ready, holds about 18 GB." in t
    assert "Running job: none." in t
    assert (
        "- 20261104-110000-bbbb FLUX.3 Klein (2026-11-02T10:00:00, queued over MCP)"
        in t
    )
    assert "- video.t2v (Text to Video): 35 GB: fits after the chat model unloads" in t
    assert "- edit.mix (" in t and "loads no model, fits now" in t


def test_create_job_with_a_local_file_and_wait_for_it(
    hp: HttpWorkstation, state: Path, tmp_path: Path
) -> None:
    src = image(tmp_path)
    r = call(
        hp,
        "create_job",
        {
            "tool": "image.klein_edit",
            "fields": {"prompt": "a fox", "references": [str(src)]},
        },
    )
    assert not r.is_error, text(r)
    assert "the user sees it in the app's job list" in text(r)
    job_id = text(r).split()[2]
    job = json.loads((state / job_id / "job.json").read_text())
    assert job["chat"] == MCP and job["params"]["prompt"] == "a fox"
    assert (state / job_id / "in_references_0.png").read_bytes() == PNG
    threading.Timer(0.5, lambda: finish(state, job_id)).start()
    r = call(hp, "wait_for_job", {"job_id": job_id, "seconds": 20})
    assert not r.is_error and "status: done" in text(r)
    out = state / job_id / "image.png"
    assert f"output: image.png (image), file {out} (in the app: " in text(r)
    assert f"/files/{job_id}/image.png)" in text(r)
    # a small JPEG preview of the image
    pictures = [c for c in r.content if c.type == "image"]
    assert len(pictures) == 1 and pictures[0].mime_type == "image/jpeg"
    assert base64.b64decode(pictures[0].data)[:3] == b"\xff\xd8\xff"
    # the output is the input of the next job, by its path or its Library reference
    for ref in (str(out), f"library:{job_id}/image.png"):
        args = {"tool": "video.i2v", "fields": {"prompt": "moves", "image": ref}}
        assert not call(hp, "create_job", args).is_error


def test_wait_for_job_returns_progress_at_its_time(
    hp: HttpWorkstation, make_job: Job
) -> None:
    make_job("20261104-100000-aaaa", "queued", chat=MCP)
    r = call(hp, "wait_for_job", {"job_id": "20261104-100000-aaaa", "seconds": 1})
    assert not r.is_error
    assert "status: queued" in text(r) and "place in the queue: 1" in text(r)
    assert "[Still queued after 1 s." in text(r)
    assert "only when they need the result now.]" in text(r)
    assert [c.type for c in r.content] == ["text"]


def test_cancel_only_jobs_queued_over_mcp(hp: HttpWorkstation, make_job: Job) -> None:
    make_job("20261104-100000-aaaa", "queued", chat=MCP)
    make_job("20261104-110000-bbbb", "queued")  # from the app's own page
    make_job("20261104-120000-cccc", "queued", chat="20261104-120000-ab12")  # a chat
    r = call(hp, "cancel_job", {"job_id": "20261104-100000-aaaa"})
    assert not r.is_error and "is cancelled" in text(r)
    for other in ("20261104-110000-bbbb", "20261104-120000-cccc"):
        r = call(hp, "cancel_job", {"job_id": other})
        assert r.is_error and "was not queued over MCP" in text(r)
        assert server.jobs[other]["status"] == "queued"


def test_read_tools_over_mcp(hp: HttpWorkstation, make_job: Job, state: Path) -> None:
    make_job("20261104-110000-bbbb", "done", params={"prompt": "a red fox"})
    finish(state, "20261104-110000-bbbb")
    r = call(hp, "list_workstation_tools")
    assert "image.klein: FLUX.3 Klein (Image), installed." in text(r)
    assert "absolute path of a file on this Mac" in text(r)
    r = call(hp, "search_library", {"query": "fox"})
    assert f"file {state / '20261104-110000-bbbb' / 'image.png'}" in text(r)
    r = call(hp, "list_jobs")
    assert "- 20261104-110000-bbbb FLUX.3 Klein" in text(r)
    r = call(hp, "job_status", {"job_id": "20261104-110000-bbbb"})
    assert "status: done" in text(r) and len(r.content) == 2  # the text and a preview
    r = call(hp, "job_status", {"job_id": "nope"})
    assert r.is_error and "No job with the id nope." in text(r)


def test_refusals_over_mcp(hp: HttpWorkstation, tmp_path: Path) -> None:
    def create(**fields: Any) -> types.CallToolResult:
        return call(hp, "create_job", {"tool": "image.klein_edit", "fields": fields})

    r = call(hp, "create_job", {"tool": "image.nope"})
    assert r.is_error and "Unknown tool 'image.nope'" in text(r)
    args = {"tool": "image.klein", "fields": {"prompt": "x", "steps": 999}}
    r = call(hp, "create_job", args)
    assert r.is_error and "The job was refused: Steps must be between 4 and 50" in text(
        r
    )
    r = create(prompt="x")
    assert r.is_error and "needs a file in its field 'references'" in text(r)
    notes = tmp_path / "notes.txt"
    notes.write_text("hello")
    r = create(prompt="x", references=[str(notes)])
    assert r.is_error and "takes image files; notes.txt is not one" in text(r)
    r = call(hp, "wait_for_job", {"job_id": "x", "seconds": 601})
    assert r.is_error and "must be at most 600" in text(r)
    r = call(hp, "nope")
    assert r.is_error and "Unknown tool 'nope'" in text(r)


@pytest.mark.parametrize(
    "kind, message",
    [
        ("relative", "give the absolute path"),
        ("link", "a link"),
        ("folder", "not a regular file"),
        ("device", "not a regular file"),
        ("fifo", "not a regular file"),
        ("hidden", "hidden or secret files"),
        ("in_hidden", "hidden or secret files"),
        ("missing", "cannot open (No such file or directory)"),
        ("unreadable", "cannot open (Permission denied)"),
        ("library", "not an output of a finished job"),
    ],
)
def test_path_refusals(
    hp: HttpWorkstation, tmp_path: Path, kind: str, message: str
) -> None:
    src = image(tmp_path)
    ref = {
        "relative": "ref.png",
        "folder": str(tmp_path),
        "device": "/dev/null",
        "hidden": str(image(tmp_path, ".ref.png")),
        "missing": str(tmp_path / "gone.png"),
        "library": "library:nope/image.png",
    }.get(kind, "")
    if kind == "link":
        ref = str(tmp_path / "link.png")
        os.symlink(src, ref)
    elif kind == "fifo":
        ref = str(tmp_path / "pipe.png")
        os.mkfifo(ref)
    elif kind == "in_hidden":
        (tmp_path / ".private").mkdir()
        ref = str(image(tmp_path / ".private"))
    elif kind == "unreadable":
        src.chmod(0)
        ref = str(src)
    r = call(
        hp,
        "create_job",
        {"tool": "image.klein_edit", "fields": {"prompt": "x", "references": [ref]}},
    )
    assert r.is_error and message in text(r), text(r)
    assert server.jobs == {}


def test_a_file_over_the_upload_limit_is_refused_before_a_copy(
    hp: HttpWorkstation, tmp_path: Path
) -> None:
    big = tmp_path / "big.mov"
    with big.open("wb") as f:
        f.truncate(200 * 1024 * 1024 + 1)  # sparse: no disk is used
    with pytest.raises(ToolError, match="file too large"):
        open_path(hp, str(big))


def test_outputs_in_a_hidden_checkout_folder_are_taken(tmp_path: Path) -> None:
    """Only the parts below the outputs folder count: a checkout in ~/.apps still takes its own outputs."""
    hidden = tmp_path / ".apps" / "outputs"
    hidden.mkdir(parents=True)
    port = HttpWorkstation(9, hidden)
    (hidden / "20261104-110000-bbbb").mkdir()
    out = image(hidden / "20261104-110000-bbbb", "image.png")
    fd, inp = open_path(port, str(out))
    os.close(fd)
    assert inp.name == "image.png"
    with pytest.raises(ToolError, match="hidden or secret"):
        open_path(port, str(image(tmp_path / ".apps", "other.png")))


def test_open_path_gives_the_real_name_and_size(
    hp: HttpWorkstation, tmp_path: Path
) -> None:
    src = image(tmp_path, "Fox Photo.PNG")
    fd, inp = open_path(hp, str(src))
    try:
        assert (inp.name, inp.size) == ("Fox Photo.PNG", len(PNG))
        inp.write(tmp_path / "copy")
    finally:
        os.close(fd)
    assert (tmp_path / "copy").read_bytes() == PNG


def test_every_tool_tells_when_the_app_does_not_run(tmp_path: Path) -> None:
    hp = HttpWorkstation(9, tmp_path)  # no token file
    names = [t.name for t in run(lambda c: c.list_tools(), hp).tools]
    args = {
        "job_status": {"job_id": "x"},
        "search_library": {"query": "x"},
        "create_job": {"tool": "image.klein"},
        "wait_for_job": {"job_id": "x"},
        "cancel_job": {"job_id": "x"},
        "transcribe_audio": {"job_id": "x"},
    }
    for name in names:
        r = call(hp, name, args.get(name, {}))
        assert r.is_error and "Workstation is not running." in text(r), name
        assert ".venv/bin/python start.py --browser" in text(r)


def test_the_legacy_handshake_works_too(hp: HttpWorkstation) -> None:
    """Clients before the 2026-07-28 protocol connect with initialize (the SDK's in-memory streams)."""

    async def both(c: Client) -> tuple[Any, Any]:
        return await c.list_tools(), await c.call_tool("list_jobs", {})

    listed, r = run(both, hp, mode="legacy")
    assert "create_job" in [t.name for t in listed.tools]
    assert not r.is_error and text(r) == "No jobs."


def test_a_call_the_client_cancels_ends_its_wait(
    hp: HttpWorkstation, make_job: Job
) -> None:
    make_job("20261104-100000-aaaa", "queued", chat=MCP)

    async def cancelled(c: Client) -> None:
        with anyio.move_on_after(0.5):
            await c.call_tool("wait_for_job", {"job_id": "20261104-100000-aaaa"})

    t0 = time.monotonic()
    run(cancelled, hp)  # the client closes only when the server's call has ended
    assert time.monotonic() - t0 < 5


def test_the_server_runs_on_stdio(hp: HttpWorkstation, state: Path) -> None:
    """The real process, as a client starts it: python -m app.mcp_server --port N, over stdin and stdout."""
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server", "--port", str(hp.port)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    async def main() -> Any:
        async with Client(params) as c:
            return await c.list_tools(), await c.call_tool("list_jobs", {})

    # the process reads the token of this checkout's outputs folder, not the temp one: it answers "not running"
    # unless the checkout's token works; both mean that the process ran and spoke MCP on stdio
    listed, r = anyio.run(main)
    assert "system_status" in [t.name for t in listed.tools]
    assert r.content[0].type == "text"


# ---------- memory: a job from MCP follows the queue's rules ----------


def test_a_big_job_from_mcp_unloads_the_chat_model_first(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chat turn holds a lease for an MCP client: the job's claim unloads the loaded model, and the job runs only
    then, so two big models never share the memory."""
    fake = Path(__file__).with_name("fake_llm_worker.py")
    monkeypatch.setattr(
        llm,
        "PATHS",
        SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=Path("ok")),
    )
    monkeypatch.setattr(llm, "WORKER", [str(fake)])
    monkeypatch.setattr(llm, "installed", lambda: True)
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.log")
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    seen: list[str] = []
    monkeypatch.setattr(
        server, "run_job", lambda job: seen.append(llm.status()["state"])
    )
    try:
        assert llm.start() is not None and llm.status()["state"] == "ready"
        r = client.post(
            "/api/jobs",
            data={"tool": "video.t2v", "prompt": "waves", "client": MCP},
            headers=ORIGIN,
        )
        job = server.jobs[r.json()["id"]]
        assert job["chat"] == MCP
        server.run_next(job)  # what the worker does for the first job in the queue
        assert seen == ["off"]  # 35 + 18 + 8 > 48: the model was unloaded first
    finally:
        llm.close("test over")
        llm.release()
        monkeypatch.setattr(llm, "closed", False)


def test_in_app_system_status_through_the_fake_port(tmp_path: Path) -> None:
    reg = Registry(ws_tools.build(FakeWorkstation()))
    ctx = Context("c1", None, threading.Event(), tmp_path)
    r = reg.call("system_status", {}, ctx)
    assert r.status == "ok"
    assert "Chat model: Qwen3.8 27B, ready, answering, holds about 18 GB." in r.content
    assert "Running job:\n- 20261103-120000-abcd FLUX.3 Klein" in r.content
    assert "42%" in r.content and "Queue: empty." in r.content
    assert "- music.ace (ACE-Step): 22 GB: NOT installed." in r.content
    assert "- video.i2v (Image to video): 30 GB: fits after the chat model" in r.content
    # a job this chat queued shows as such
    reg.call("create_job", {"tool": "image.klein", "fields": {"prompt": "x"}}, ctx)
    r = reg.call("system_status", {}, ctx)
    assert "Queue (1 jobs, in the order they run):" in r.content
    assert "queued by this chat): queued; place 1 in the queue" in r.content


def test_start_py_mcp_runs_only_the_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # importing start.py puts Homebrew first in PATH; monkeypatch sets the old PATH again after the test
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    import start
    from app import mcp_server

    seen: list[Any] = []
    monkeypatch.setattr(mcp_server, "main", seen.append)
    monkeypatch.setattr(start, "port_free", lambda: pytest.fail("checked the port"))
    monkeypatch.setattr(server, "start", lambda: pytest.fail("a server started"))
    start.main(["--mcp"])
    assert seen == [[]]  # the defaults: port 7870, this checkout's outputs folder


def test_chat_labels_name_the_new_tool() -> None:
    assert chat.LABELS["system_status"] == "System status"
