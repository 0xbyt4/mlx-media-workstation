"""The journeys of a new user, end to end through the HTTP API, as the web UI makes them: the first start without
models, a job from Create to Jobs and the Library, a chat, an agent chat with an approval and a restart of the app
while a page is open. The fakes stand in for the models (tests/fake_llm_worker.py, a plan of short Python
commands, FakeNet), so no test runs a model or reaches the network."""

import json
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import chat, library, llm, models, server, tools
from app.agent import Limits, Settings, build_registry
from app.agent.web_tools import Net
from tests.conftest import TOKEN, app_client
from tests.test_agent_chat import FakeNet, FakeSandbox

REAL_RUN_JOB = (
    server.run_job
)  # the client fixture replaces it, so that no request starts a job
FAKE_LLM = Path(__file__).with_name("fake_llm_worker.py")
PAGE = {
    "Origin": f"http://127.0.0.1:{server.PORT}"
}  # the app's own page sends its Origin with each change
macos_time = pytest.mark.skipif(
    sys.platform != "darwin", reason="run_job uses macOS /usr/bin/time -l"
)


def events(response: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def wait_for(cond: Callable[[], Any], timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def post_job(client: TestClient, tool: str, **data: str) -> Any:
    return client.post("/api/jobs", data={"tool": tool, **data})


def listed(client: TestClient) -> dict[str, dict[str, Any]]:
    return {j["id"]: j for j in client.get("/api/jobs").json()}


def run_first_queued(monkeypatch: pytest.MonkeyPatch, *snippets: str) -> str:
    """What the worker does with the first queued job, with a plan of Python snippets (one command each) instead
    of the model's commands. Returns the job id."""
    with server.lock:
        job = server.queued_jobs()[0]
        job["status"], job["started"] = "running", server.now()
    job_dir = server.OUT / job["id"]

    def plan(tool: str, params: dict, files: dict, out: Path) -> tools.Plan:
        return tools.Plan([[sys.executable, "-c", s] for s in snippets], {}, cwd=out)

    monkeypatch.setattr(tools, "plan", plan)
    REAL_RUN_JOB(job)
    server.ended(job)
    assert job_dir.is_dir()
    return job["id"]


@pytest.fixture
def no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        library, "probe", lambda path: {"width": 4, "height": 4, "duration": None}
    )
    monkeypatch.setattr(library, "probed", {})


@pytest.fixture
def fake_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fake LLM worker and a chat folder in tmp_path; the agent's web tools reach FakeNet only."""
    monkeypatch.setattr(
        llm,
        "PATHS",
        SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=Path("ok")),
    )
    monkeypatch.setattr(llm, "WORKER", [str(FAKE_LLM)])
    monkeypatch.setattr(llm, "installed", lambda: True)
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.log")
    monkeypatch.setattr(llm, "closed", False)
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(chat, "CHATS", tmp_path / "chats")
    net = FakeNet()
    registry = build_registry(
        None,
        Settings(marginalia=False),
        net=Net(net.resolve, net.transport),
        sandbox=FakeSandbox(),
        limits=Limits(),
    )
    monkeypatch.setattr(chat, "registry", lambda: registry)
    yield
    for run in list(chat.live.values()):  # a failed test must not leave a turn waiting
        run.stop = True
        run.abort.set()
        if run.approval is not None:
            run.approval.answered.set()
    llm.close("test over")
    llm.release()


# First start: no model is installed yet


def test_first_start_without_models(
    client: TestClient, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys_info = client.get("/api/system").json()
    assert (sys_info["profile"], sys_info["running"], sys_info["queued"]) == (
        "full",
        None,
        0,
    )
    groups = [t["group"] for t in client.get("/api/tools").json()]
    assert groups == sorted(
        groups, key=["Image", "Voice", "Music", "Video", "Edit", "Document"].index
    )
    # every list a new user opens is empty, and none of them fails
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/library").json() == []
    # a tool whose model is missing refuses the job and names what to install; nothing is queued
    missing = tmp_path / "mlx-media" / "mflux" / "mflux-generate-flux3"
    monkeypatch.setattr(tools, "REQUIRES", {"image.klein": [missing]})
    r = post_job(client, "image.klein", prompt="a lighthouse")
    assert r.status_code == 400
    assert r.json()["detail"].startswith(f"Not installed: {missing}.")
    assert client.get("/api/jobs").json() == []
    assert list(state.iterdir()) == []


# Create, Jobs and the Library


@macos_time
def test_a_job_from_create_to_a_failure_and_back(
    client: TestClient, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = post_job(client, "image.klein", prompt="a lighthouse", seed="7").json()[
        "id"
    ]
    second = post_job(client, "image.moss", prompt="a harbour").json()["id"]
    third = post_job(client, "image.klein", prompt="a storm").json()["id"]
    jobs = listed(client)
    assert [jobs[i]["position"] for i in (first, second, third)] == [1, 2, 3]
    assert client.get("/api/system").json()["queued"] == 3

    # Jobs: move the last job to the top, remove the middle one from the queue
    assert client.post(f"/api/jobs/{third}/move", json={"to": "top"}).json() == {
        "position": 1
    }
    assert client.post(f"/api/jobs/{second}/cancel").json() == {"status": "cancelled"}
    assert client.delete(f"/api/jobs/{first}").status_code == 400  # still queued
    jobs = listed(client)
    assert (jobs[third]["position"], jobs[first]["position"]) == (1, 2)
    assert jobs[second]["status"] == "cancelled"

    # the worker runs the first in line; the model prints a traceback and exits with an error
    model = "; ".join(
        [
            "import sys",
            *(f"print('loading part {i}', flush=True)" for i in range(30)),
            "print('ValueError: the prompt is too long', file=sys.stderr)",
            "sys.exit(3)",
        ]
    )
    assert run_first_queued(monkeypatch, model) == third
    failed = listed(client)[third]
    assert failed["status"] == "failed"
    # the error is the app's sentence, without an exception name
    assert failed["error"] == "The model process exited with code 3. See the log."
    # the log tail shows the model's last lines, not the /usr/bin/time report after them
    tail = failed["log_tail"].splitlines()
    assert tail[-1] == "ValueError: the prompt is too long"
    assert len(tail) == server.TAIL_LINES
    assert not any("footprint" in line or " real " in line for line in tail)
    assert failed["peak_gb"] is not None  # read from the report in job.log
    # "Use these settings" reads the params of the failed job
    assert failed["params"]["prompt"] == "a storm"
    assert listed(client)[first]["position"] == 1

    # the failed job can be deleted with its folder; a deleted job is gone for good
    assert client.delete(f"/api/jobs/{third}").json() == {"status": "deleted"}
    assert not (state / third).exists()
    assert third not in listed(client)
    assert client.post(f"/api/jobs/{third}/cancel").status_code == 404


@macos_time
def test_a_done_job_reaches_the_library(
    client: TestClient, state: Path, monkeypatch: pytest.MonkeyPatch, no_ffprobe: None
) -> None:
    job_id = post_job(client, "image.klein", prompt="a fox in snow").json()["id"]
    write = (
        "from pathlib import Path; Path('image.png').write_bytes(b'png');"
        "Path('notes.txt').write_text('a transcript')"
    )
    run_first_queued(monkeypatch, write)
    done = listed(client)[job_id]
    assert done["status"] == "done" and done["error"] is None
    assert [(o["name"], o["kind"]) for o in done["outputs"]] == [
        ("image.png", "image"),
        ("notes.txt", "text"),
    ]

    items = client.get("/api/library").json()
    assert sorted((i["name"], i["kind"], i["text"]) for i in items) == [
        ("image.png", "image", "a fox in snow"),
        ("notes.txt", "text", "a fox in snow"),
    ]
    # a text output is served as text, so the Jobs and Library views can show it
    r = client.get(f"/files/{job_id}/notes.txt")
    assert r.text == "a transcript" and r.headers["content-type"].startswith(
        "text/plain"
    )

    # delete one output: the Library and the job both lose it, the job stays
    assert client.delete(f"/api/library/{job_id}/image.png").json() == {
        "status": "deleted"
    }
    assert [i["name"] for i in client.get("/api/library").json()] == ["notes.txt"]
    assert [o["name"] for o in listed(client)[job_id]["outputs"]] == ["notes.txt"]
    assert client.delete(f"/api/library/{job_id}/notes.txt").status_code == 200
    assert client.get("/api/library").json() == []
    assert listed(client)[job_id]["status"] == "done"


# Chat


def test_a_chat_from_the_first_message_to_delete(
    client: TestClient, fake_llm: None
) -> None:
    assert client.get("/api/chats").json() == []
    cid = client.post("/api/chats", json={"mode": "workstation"}).json()["id"]
    evs = events(client.post(f"/api/chats/{cid}/messages", data={"text": "hi there"}))
    assert evs[-1]["event"] == "done"
    [row] = client.get("/api/chats").json()
    assert (row["id"], row["title"], row["messages"]) == (cid, "hi there", 2)
    # the mode is fixed once the chat has a message
    r = client.patch(f"/api/chats/{cid}", json={"mode": "general"}, headers=PAGE)
    assert r.status_code == 409

    # Regenerate replaces the last answer
    evs = events(client.post(f"/api/chats/{cid}/messages", data={"regenerate": "true"}))
    assert evs[-1]["event"] == "done"
    assert client.get(f"/api/chats/{cid}").json()["messages"][-1]["content"] == (
        "Hello world"
    )
    assert client.get("/api/chats").json()[0]["messages"] == 2

    # a long answer: Stop keeps the words so far and marks the answer as cut
    t = threading.Thread(
        target=client.post,
        args=(f"/api/chats/{cid}/messages",),
        kwargs={"data": {"text": "SLOW"}},
        daemon=True,
    )
    t.start()
    assert wait_for(
        lambda: (
            chat.live.get(cid) is not None
            and any(e["event"] == "content" for e in chat.live[cid].events)
        )
    )
    assert client.delete(f"/api/chats/{cid}").status_code == 409  # still answering
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    last = client.get(f"/api/chats/{cid}").json()["messages"][-1]
    assert last["finish"] == "cancelled" and last["content"].startswith("tick")

    assert client.delete(f"/api/chats/{cid}").status_code == 200
    assert client.get(f"/api/chats/{cid}").status_code == 404
    assert client.get("/api/chats").json() == []


def test_an_agent_chat_asks_before_it_sends_anything(
    client: TestClient, fake_llm: None
) -> None:
    cid = client.post("/api/chats", json={"mode": "agent"}).json()["id"]
    tools_menu = client.get(f"/api/chats/{cid}/agent").json()
    assert tools_menu["folder"] is None
    # the Tools menu refuses a folder that is not a full path, with a sentence the menu shows
    r = client.patch(f"/api/chats/{cid}/agent", json={"folder": "notes"}, headers=PAGE)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Enter the full path of a folder")

    steps = [[{"name": "web_search", "arguments": {"query": "antikythera"}}], "RESULT"]
    box: dict[str, Any] = {}
    t = threading.Thread(
        target=lambda: box.update(
            r=client.post(
                f"/api/chats/{cid}/messages",
                data={"text": "Look it up. STEPS:" + json.dumps(steps)},
            )
        ),
        daemon=True,
    )
    t.start()
    assert wait_for(lambda: cid in chat.live and chat.live[cid].approval is not None)
    ask = next(e for e in chat.live[cid].events if e["event"] == "approval")
    # the page that comes back to the chat while it waits finds the same question
    assert client.get(f"/api/chats/{cid}").json()["running"]
    r = client.post(
        f"/api/chats/{cid}/approve",
        json={"call_id": ask["id"], "decision": "deny"},
        headers=PAGE,
    )
    assert r.status_code == 200
    t.join(5)
    evs = events(box["r"])
    assert next(e for e in evs if e["event"] == "tool_result")["status"] == "denied"
    assert evs[-1]["event"] == "done" and evs[-1]["finish"] == "stop"
    # a second answer to the same question comes too late
    r = client.post(
        f"/api/chats/{cid}/approve",
        json={"call_id": ask["id"], "decision": "once"},
        headers=PAGE,
    )
    assert r.status_code == 409


# A restart of the app while a page is open


def test_a_restarted_app_signs_the_open_page_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/api/jobs").status_code == 200
    monkeypatch.setattr(server, "token", "ef" * 32)  # the new run's token
    r = client.get("/api/jobs")
    # the page's store shows the signed-out banner for HTTP 401 (web/js/store.js)
    assert r.status_code == 401
    assert r.json()["detail"].startswith("Not signed in")
    assert client.get("/").status_code == 401  # a reload shows the sign-in note
    fresh = app_client()
    r = fresh.get(f"/auth?t={'ef' * 32}&next=/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert fresh.get("/api/jobs").status_code == 200
    assert TOKEN != server.token
