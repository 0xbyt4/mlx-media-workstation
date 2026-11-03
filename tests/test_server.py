"""The HTTP API with FastAPI's TestClient on a temp outputs folder; no job ever runs."""

import json
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config, server, tools
from tests.conftest import FakeMedia, MacEffects

Job = Callable[..., dict[str, Any]]
JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


def post_job(client: TestClient, tool: str, data: dict[str, str], **kw: Any) -> Any:
    return client.post("/api/jobs", data={"tool": tool, **data}, **kw)


def saved(state: Path, job_id: str) -> dict[str, Any]:
    return json.loads((state / job_id / "job.json").read_text())


# Reading


def test_tools_endpoint(client: TestClient) -> None:
    r = client.get("/api/tools")
    assert r.status_code == 200
    assert r.json() == json.loads(json.dumps(tools.TOOLS))
    assert r.headers["x-content-type-options"] == "nosniff"


def test_page_is_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_other_host_names_are_refused(client: TestClient) -> None:
    assert client.get("/api/tools", headers={"host": "evil.example"}).status_code == 400
    assert (
        client.get("/api/tools", headers={"host": "localhost:7870"}).status_code == 200
    )


def test_no_page_may_show_the_app_in_a_frame(client: TestClient) -> None:
    """Every response, refusals included, forbids framing (clickjacking of the approval buttons)."""
    responses = [
        client.get("/"),
        client.get("/js/main.js"),
        client.get("/api/tools"),
        client.get("/api/jobs/missing"),
        client.get("/api/tools", headers={"host": "evil.example"}),
        client.post("/api/jobs", headers={"origin": "http://evil.example"}),
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 404, 400, 403]
    for r in responses:
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"


# Creating jobs


def test_create_job(client: TestClient, state: Path) -> None:
    r = post_job(client, "image.moss", {"prompt": "a\r\nlighthouse", "seed": "7"})
    assert r.status_code == 200
    job_id = r.json()["id"]
    assert JOB_ID.match(job_id)
    job = saved(state, job_id)
    assert job["status"] == "queued"
    assert job["tool_label"] == "MOSS-Image"
    assert job["params"] == {
        "prompt": "a\nlighthouse",  # browsers send CRLF
        "size": "768x1344",
        "seed": "7",
        "negative": tools.MOSS_NEGATIVE,
        "steps": 50,
        "guidance": 4.0,
    }
    assert server.wake.is_set()
    [listed] = client.get("/api/jobs").json()
    assert listed["id"] == job_id
    assert (listed["status"], listed["position"], listed["outputs"]) == (
        "queued",
        1,
        [],
    )


@pytest.mark.parametrize(
    ("tool", "data", "name"),
    [
        ("image.moss", {"prompt": "x", "negative": ""}, "negative"),
        ("video.a2v", {"prompt": "x", "style": ""}, "style"),
    ],
)
def test_cleared_text_field_stays_empty(
    client: TestClient,
    state: Path,
    media: FakeMedia,
    tool: str,
    data: dict[str, str],
    name: str,
) -> None:
    media.seconds["in_audio_0.wav"] = 3.0
    files = None
    if tool == "video.a2v":
        files = {
            "image": ("p.png", b"x", "image/png"),
            "audio": ("v.wav", b"x", "audio/wav"),
        }
    r = post_job(client, tool, data, files=files)
    assert r.status_code == 200
    assert saved(state, r.json()["id"])["params"][name] == ""


def test_queue_positions(client: TestClient) -> None:
    ids = [
        post_job(client, "image.klein", {"prompt": str(i)}).json()["id"]
        for i in range(3)
    ]
    positions = {j["id"]: j["position"] for j in client.get("/api/jobs").json()}
    assert positions == {ids[0]: 1, ids[1]: 2, ids[2]: 3}


@pytest.mark.parametrize(
    ("tool", "data", "detail"),
    [
        ("image.moss", {}, "Prompt is required"),
        ("image.moss", {"prompt": "   "}, "Prompt is required"),
        (
            "image.moss",
            {"prompt": "x", "steps": "5"},
            "Steps must be between 10 and 100",
        ),
        (
            "image.moss",
            {"prompt": "x", "guidance": "abc"},
            "Guidance (CFG) must be a number",
        ),
        ("image.moss", {"prompt": "x", "size": "1x1"}, "Size: unknown option"),
        (
            "image.moss",
            {"prompt": "x", "seed": "-3"},
            "Seed must be between 0 and 4294967295",
        ),
        (
            "video.t2v",
            {"prompt": "x", "seconds": "11"},
            "Length (s) must be between 1 and 10",
        ),
        (
            "video.t2v",
            {"prompt": "x", "audio": "on"},
            "Generate sound must be true or false",
        ),
        ("video.i2v", {"prompt": "x"}, "Start image is required"),
        ("voice.drama3", {"text": "\r\n"}, "Script is required"),
        ("nope", {}, "Unknown tool 'nope'"),
    ],
)
def test_create_job_refuses_bad_fields(
    client: TestClient, state: Path, tool: str, data: dict[str, str], detail: str
) -> None:
    r = post_job(client, tool, data)
    assert (r.status_code, r.json()) == (400, {"detail": detail})
    assert list(state.iterdir()) == []  # no half-made job folder
    assert server.jobs == {}


def test_create_job_refuses_a_missing_model(
    client: TestClient, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "mflux" / "mflux-generate-flux3"
    monkeypatch.setattr(tools, "REQUIRES", {"image.klein": [missing]})
    r = post_job(client, "image.klein", {"prompt": "x"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith(f"Not installed: {missing}.")
    assert list(state.iterdir()) == []


def test_create_job_reports_unexpected_errors(
    client: TestClient, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tools, "validate", broken)
    r = post_job(client, "image.klein", {"prompt": "x"})
    assert (r.status_code, r.json()) == (
        500,
        {"detail": "Could not create the job: OSError: disk full"},
    )
    assert list(state.iterdir()) == []


def test_create_job_while_closing(client: TestClient, state: Path) -> None:
    server.stopping.set()
    r = post_job(client, "image.klein", {"prompt": "x"})
    assert (r.status_code, r.json()) == (503, {"detail": "The app is closing"})
    assert list(state.iterdir()) == []


# Uploads


@pytest.mark.parametrize(
    ("upload", "stored"),
    [
        ("Photo.JPG", "in_image_0.jpg"),
        ("start.webp", "in_image_0.webp"),
        ("page.html", "in_image_0.bin"),
        ("../../evil.png", "in_image_0.png"),
        ("no_suffix", "in_image_0.bin"),
    ],
)
def test_uploads_get_fixed_names(
    client: TestClient, state: Path, upload: str, stored: str
) -> None:
    files = {"image": (upload, b"image bytes", "image/jpeg")}
    r = post_job(client, "video.i2v", {"prompt": "x"}, files=files)
    assert r.status_code == 200
    job_id = r.json()["id"]
    assert saved(state, job_id)["files"] == {"image": [stored]}
    assert (state / job_id / stored).read_bytes() == b"image bytes"
    assert sorted(p.name for p in (state / job_id).iterdir()) == sorted(
        ["job.json", stored]
    )


def test_multi_file_field_keeps_the_order(client: TestClient, state: Path) -> None:
    files = [
        ("references", ("b.png", b"0", "image/png")),
        ("references", ("a.jpg", b"1", "image/jpeg")),
    ]
    r = post_job(client, "image.klein_edit", {"prompt": "x"}, files=files)
    job_id = r.json()["id"]
    assert saved(state, job_id)["files"] == {
        "references": ["in_references_0.png", "in_references_1.jpg"]
    }
    assert (state / job_id / "in_references_1.jpg").read_bytes() == b"1"


@pytest.mark.parametrize(
    ("tool", "name", "count", "detail"),
    [
        ("video.i2v", "image", 2, "Start image: at most 1 file(s)"),
        ("image.klein_edit", "references", 11, "Reference images: at most 10 file(s)"),
    ],
)
def test_too_many_files(
    client: TestClient, state: Path, tool: str, name: str, count: int, detail: str
) -> None:
    files = [(name, (f"{i}.png", b"x", "image/png")) for i in range(count)]
    r = post_job(client, tool, {"prompt": "x"}, files=files)
    assert (r.status_code, r.json()) == (400, {"detail": detail})
    assert list(state.iterdir()) == []


def test_file_too_large(
    client: TestClient, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "MAX_UPLOAD", 4)
    files = {"image": ("a.png", b"12345", "image/png")}
    r = post_job(client, "video.i2v", {"prompt": "x"}, files=files)
    assert (r.status_code, r.json()) == (
        400,
        {"detail": "Start image: file too large (max 200 MB)"},
    )
    assert list(state.iterdir()) == []


def test_reference_without_transcript(client: TestClient, state: Path) -> None:
    files = {"ref0": ("voice.wav", b"RIFF", "audio/wav")}
    r = post_job(client, "voice.drama3", {"text": "Hello."}, files=files)
    assert (r.status_code, r.json()) == (
        400,
        {"detail": "Add the transcript of voice reference 0."},
    )
    assert list(state.iterdir()) == []


# Same-origin rule for writes


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example", "null", "http://127.0.0.1:7871", "https://127.0.0.1:7870"],
)
def test_cross_site_writes_are_refused(
    client: TestClient, make_job: Job, state: Path, origin: str
) -> None:
    make_job("j1", "done")
    headers = {"origin": origin}
    refused = {"detail": "Cross-site request refused"}
    r = post_job(client, "image.klein", {"prompt": "x"}, headers=headers)
    assert (r.status_code, r.json()) == (403, refused)
    r = client.post("/api/jobs/j1/cancel", headers=headers)
    assert (r.status_code, r.json()) == (403, refused)
    r = client.delete("/api/jobs/j1", headers=headers)
    assert (r.status_code, r.json()) == (403, refused)
    assert (state / "j1").is_dir()
    assert client.get("/api/jobs", headers=headers).status_code == 200


@pytest.mark.parametrize("origin", ["http://127.0.0.1:7870", "http://localhost:7870"])
def test_own_page_may_write(client: TestClient, origin: str) -> None:
    r = post_job(client, "image.klein", {"prompt": "x"}, headers={"origin": origin})
    assert r.status_code == 200


def test_oversized_request_is_refused(client: TestClient, state: Path) -> None:
    headers = {"content-length": str(server.MAX_REQUEST + 1)}
    r = client.post("/api/jobs", content=b"tool=image.klein", headers=headers)
    assert (r.status_code, r.json()) == (413, {"detail": "Request too large"})
    assert list(state.iterdir()) == []


# Cancel and delete


@pytest.mark.parametrize(("method", "path"), [("POST", "/cancel"), ("DELETE", "")])
def test_unknown_job(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, f"/api/jobs/20260101-000000-0000{path}")
    assert (r.status_code, r.json()) == (404, {"detail": "No such job"})


def test_cancel_queued_job(client: TestClient, make_job: Job, state: Path) -> None:
    make_job("j1", "queued")
    assert client.post("/api/jobs/j1/cancel").json() == {"status": "cancelled"}
    job = saved(state, "j1")
    assert (job["status"], job["error"]) == ("cancelled", "Cancelled.")
    assert client.post("/api/jobs/j1/cancel").json() == {"status": "cancelled"}


def test_cancel_finished_job_changes_nothing(
    client: TestClient, make_job: Job, state: Path
) -> None:
    make_job("j1", "done")
    assert client.post("/api/jobs/j1/cancel").json() == {"status": "done"}
    assert saved(state, "j1")["status"] == "done"


def test_cancel_running_job_stops_its_process_group(
    client: TestClient, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[int] = []
    done = threading.Event()

    def stop(pgid: int) -> None:
        stopped.append(pgid)
        done.set()

    monkeypatch.setattr(server, "stop_group", stop)
    make_job("j1", "running")
    server.procs["j1"] = SimpleNamespace(pid=4242)
    assert client.post("/api/jobs/j1/cancel").json() == {"status": "cancelled"}
    assert done.wait(5)
    assert stopped == [4242]


def test_delete_finished_job(client: TestClient, make_job: Job, state: Path) -> None:
    make_job("j1", "failed")
    assert client.delete("/api/jobs/j1").json() == {"status": "deleted"}
    assert not (state / "j1").exists()
    assert server.jobs == {}


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_needs_a_cancel_first(
    client: TestClient, make_job: Job, state: Path, status: str
) -> None:
    make_job("j1", status)
    r = client.delete("/api/jobs/j1")
    assert (r.status_code, r.json()) == (400, {"detail": "Cancel the job first"})
    assert (state / "j1").is_dir()


def test_delete_waits_for_the_process(
    client: TestClient, make_job: Job, state: Path
) -> None:
    make_job("j1", "cancelled")
    server.procs["j1"] = SimpleNamespace(pid=4243)
    assert client.delete("/api/jobs/j1").status_code == 400
    assert (state / "j1").is_dir()


# Output files


@pytest.fixture
def files_job(make_job: Job, state: Path, tmp_path: Path) -> Path:
    make_job("j1", "done")
    job_dir = state / "j1"
    (job_dir / "image.png").write_bytes(b"\x89PNG result")
    (job_dir / ".hidden.png").write_bytes(b"hidden")
    (job_dir / "job.log").write_text("log")
    (tmp_path / "secret.png").write_bytes(b"secret")
    (job_dir / "link.png").symlink_to(tmp_path / "secret.png")
    return job_dir


def test_output_file_is_served(client: TestClient, files_job: Path) -> None:
    r = client.get("/files/j1/image.png")
    assert r.status_code == 200
    assert r.content == b"\x89PNG result"
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/files/j1/job.json",
        "/files/j1/job.log",
        "/files/j1/.hidden.png",
        "/files/j1/missing.png",
        "/files/j1/link.png",
        "/files/j1/..%2F..%2Fsecret.png",
        "/files/j1/%2e%2e",
        "/files/j1/..%5C..%5Csecret.png",
        "/files/..%2Fj1/image.png",
        "/files/%2e%2e/secret.png",
        "/files/unknown/image.png",
        "/files/j1/../../secret.png",
    ],
)
def test_files_outside_the_outputs_are_not_served(
    client: TestClient, files_job: Path, path: str
) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert b"secret" not in r.content


# System info


def fake_system(
    monkeypatch: pytest.MonkeyPatch, vm: str, memsize: str, swap: str
) -> None:
    answers = {"vm_stat": vm, "hw.memsize": memsize, "vm.swapusage": swap}

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, answers[cmd[-1]], "")

    monkeypatch.setattr(server.subprocess, "run", run)


VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                            900000.
Pages inactive:                          200000.
Pages speculative:                        10000.
"""


def test_system(
    client: TestClient, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_system(
        monkeypatch,
        VM_STAT,
        "51539607552\n",
        "total = 2048.00M  used = 1536.00M  free = 512.00M  (encrypted)\n",
    )
    make_job("j1", "running")
    make_job("j2", "queued")
    make_job("j3", "queued")
    make_job("j4", "done")
    assert client.get("/api/system").json() == {
        "mem_total_gb": 48.0,
        "mem_free_gb": 4.7,  # (100000 + 200000 + 10000) pages of 16 KB
        "swap_used_gb": 1.5,
        "running": "j1",
        "queued": 2,
        "profile": "full",
    }


def test_system_without_answers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_system(monkeypatch, "", "", "")
    assert client.get("/api/system").json() == {
        "mem_total_gb": 0.0,
        "mem_free_gb": 0.0,
        "swap_used_gb": 0.0,
        "running": None,
        "queued": 0,
        "profile": "full",
    }


def test_system_reports_the_profile(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_system(monkeypatch, "", "", "")
    assert client.get("/api/system").json()["profile"] == "full"
    monkeypatch.setattr(config, "APP", replace(config.APP, profile="chat"))
    assert client.get("/api/system").json()["profile"] == "chat"


def test_page_files_are_revalidated(client: TestClient) -> None:
    for path in ("/", "/js/main.js", "/css/tokens.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


# Queue order


def move(client: TestClient, job_id: str, to: str) -> object:
    return client.post(f"/api/jobs/{job_id}/move", json={"to": to})


def queue(client: TestClient) -> list[str]:
    queued = [j for j in client.get("/api/jobs").json() if j["status"] == "queued"]
    return [j["id"] for j in sorted(queued, key=lambda j: j["position"])]


def test_move_changes_the_queue_order(client: TestClient, state: Path) -> None:
    a, b, c = (
        post_job(client, "image.klein", {"prompt": str(i)}).json()["id"]
        for i in range(3)
    )
    assert queue(client) == [a, b, c]
    assert move(client, c, "up").json() == {"position": 2}
    assert queue(client) == [a, c, b]
    assert move(client, a, "down").json() == {"position": 2}
    assert queue(client) == [c, a, b]
    assert move(client, b, "top").json() == {"position": 1}
    assert queue(client) == [b, c, a]
    assert move(client, b, "bottom").json() == {"position": 3}
    assert queue(client) == [c, a, b]
    # at the ends nothing changes
    assert move(client, c, "up").json() == {"position": 1}
    assert move(client, b, "down").json() == {"position": 3}
    assert queue(client) == [c, a, b]
    # the order is in job.json, so the list read back from the folders has it too
    server.jobs.clear()
    for job_id in (a, b, c):
        server.jobs[job_id] = saved(state, job_id)
    assert [j["id"] for j in server.queued_jobs()] == [c, a, b]


def test_new_jobs_go_after_moved_ones(client: TestClient) -> None:
    a, b = (
        post_job(client, "image.klein", {"prompt": str(i)}).json()["id"]
        for i in range(2)
    )
    move(client, b, "top")
    c = post_job(client, "image.klein", {"prompt": "c"}).json()["id"]
    assert queue(client) == [b, a, c]


def test_jobs_without_an_order_sort_by_creation_first(
    client: TestClient, make_job: Job
) -> None:
    make_job("new", "queued", created="2026-11-02T09:00:00", order=1)
    make_job("old2", "queued", created="2026-11-01T10:00:02")
    make_job("old1", "queued", created="2026-11-01T10:00:01")
    assert queue(client) == ["old1", "old2", "new"]
    assert move(client, "new", "top").json() == {"position": 1}
    assert queue(client) == ["new", "old1", "old2"]


@pytest.mark.parametrize("status", ["running", "done", "failed", "cancelled"])
def test_move_refuses_jobs_that_are_not_queued(
    client: TestClient, make_job: Job, status: str
) -> None:
    make_job("j1", status)
    r = move(client, "j1", "up")
    assert (r.status_code, r.json()) == (409, {"detail": "Only a queued job can move"})


def test_move_unknown_job_or_direction(client: TestClient, make_job: Job) -> None:
    assert move(client, "nope", "up").status_code == 404
    make_job("j1", "queued")
    assert move(client, "j1", "sideways").status_code == 422
    assert client.post("/api/jobs/j1/move", json={}).status_code == 422


def test_move_needs_the_app_origin(client: TestClient, make_job: Job) -> None:
    make_job("j1", "queued")
    r = client.post(
        "/api/jobs/j1/move",
        json={"to": "up"},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403


# Keep awake


def test_keep_awake_follows_the_queue(
    client: TestClient, mac_effects: MacEffects
) -> None:
    job_id = post_job(client, "image.klein", {"prompt": "x"}).json()["id"]
    assert mac_effects.awake.held
    client.post(f"/api/jobs/{job_id}/cancel")
    assert not mac_effects.awake.held


def test_keep_awake_off(
    client: TestClient, mac_effects: MacEffects, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        server.config,
        "APP",
        server.config.App(**{**server.config.APP_DEFAULTS, "keep_awake": False}),
    )
    post_job(client, "image.klein", {"prompt": "x"})
    assert mac_effects.awake.calls == [False]


# The session: other programs and users on this Mac cannot use the API


def test_without_the_session_everything_is_refused(client) -> None:
    bare = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    assert bare.get("/api/jobs").status_code == 401
    assert bare.get("/api/chats").status_code == 401
    assert (
        bare.post(
            "/api/chats",
            json={"mode": "general"},
            headers={"Origin": next(iter(server.ORIGINS))},
        ).status_code
        == 401
    )
    page = bare.get("/")
    assert page.status_code == 401 and "Workstation.app" in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    bare.cookies.set(server.SESSION_COOKIE, "0" * 64)  # a guess
    assert bare.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs").status_code == 200


def test_the_sign_in_link_sets_the_session(monkeypatch) -> None:
    bare = TestClient(
        server.app, base_url=f"http://127.0.0.1:{server.PORT}", follow_redirects=False
    )
    assert bare.get("/auth", params={"t": "0" * 64}).status_code == 403
    r = bare.get("/auth", params={"t": server.token, "next": "/?profile=chat#/chat"})
    assert r.status_code == 303 and r.headers["location"] == "/?profile=chat#/chat"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert bare.get("/api/jobs").status_code == 200
    for elsewhere in (
        "//evil.example/",
        "https://evil.example/",
        "/\\evil.example",
        "/a\r\nSet-Cookie: x=1",
    ):
        r = bare.get("/auth", params={"t": server.token, "next": elsewhere})
        assert r.headers["location"] == "/", elsewhere
    assert (
        bare.get("/auth", params={"t": "\u00e9"}).status_code == 403
    )  # not ASCII: refused, not a crash
    r = bare.get("/auth", params={"t": server.token, "next": "/\u2603"})
    assert r.headers["location"] == "/"
    monkeypatch.setattr(server, "token", None)  # before start(): no token, no session
    assert bare.get("/auth", params={"t": ""}).status_code == 403
    assert bare.get("/api/jobs").status_code == 401


def test_start_py_check_answers_only_the_proof_without_the_session() -> None:
    bare = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    nonce = "ab" * 16
    data = bare.get("/api/system", params={"nonce": nonce}).json()
    assert set(data) == {"proof", "profile"} and data["proof"] == server.proof(nonce)
    assert bare.get("/api/system").status_code == 401
