"""The HTTP API with FastAPI's TestClient on a temp outputs folder; no job ever runs."""

import json
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import server, tools
from tests.conftest import FakeMedia

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
    }


def test_page_files_are_revalidated(client: TestClient) -> None:
    for path in ("/", "/js/main.js", "/css/tokens.css"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
