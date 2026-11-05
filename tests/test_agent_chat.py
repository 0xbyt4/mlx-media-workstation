"""The agent loop of the chat (app/chat.py Turn) end to end: the fake LLM worker scripts the model's tool calls
(STEPS: in tests/fake_llm_worker.py), the tools are the real ones over a fake Workstation, a fake network and a fake
sandbox, so no test runs a model, reaches the network or starts sandbox-exec."""

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import chat, llm, models, server, tools
from app.agent import Context, Limits, RunResult, Settings, build_registry
from app.agent.net import HttpRequest, HttpResponse
from app.agent.web_tools import Net
from tests.conftest import app_client
from tests.fake_workstation import FakeWorkstation

REAL_REGISTRY = chat.registry  # the tests below replace it with fakes

FAKE = Path(__file__).with_name("fake_llm_worker.py")
ORIGIN = {"Origin": f"http://127.0.0.1:{server.PORT}"}
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeSandbox:
    """Records the code; "PLOT" in it writes plot.png, "WAIT" runs until Stop."""

    name = "fake"

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.walls: list[float] = []  # the wall-clock limit of each run

    def unavailable(self) -> str | None:
        return None

    def run_python(self, code, workdir, limits, abort, read_only=()) -> RunResult:
        self.codes.append(code)
        self.walls.append(limits.wall_s)
        workdir.mkdir(parents=True, exist_ok=True)
        if "WAIT" in code:
            abort.wait(5)
            return RunResult(None, "", "", 0.1, killed="aborted")
        files = []
        if "PLOT" in code:
            (workdir / "plot.png").write_bytes(PNG)
            files.append("plot.png")
        return RunResult(0, "42\n", "", 0.01, files=files)


class FakeShell:
    """Records each command with the folder it may read."""

    name = "fake"

    def __init__(self) -> None:
        self.runs: list[tuple[str, Path | None, float]] = []

    def unavailable(self) -> str | None:
        return None

    def run_shell(self, command, workdir, limits, abort, read_only=None) -> RunResult:
        self.runs.append((command, read_only, limits.wall_s))
        workdir.mkdir(parents=True, exist_ok=True)
        return RunResult(0, "listed\n", "", 0.02)


class FakeNet:
    """Every host resolves to a public address; every request gets one Wikipedia hit."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []

    def resolve(self, host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    def transport(self, req: HttpRequest) -> HttpResponse:
        self.requests.append(req)
        hit = {
            "title": "Antikythera mechanism",
            "snippet": "An ancient Greek hand-powered orrery",
        }
        body = json.dumps({"query": {"search": [hit]}}).encode()
        return HttpResponse(200, {"content-type": "application/json"}, body)


@pytest.fixture(autouse=True)
def fake_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(
        llm,
        "PATHS",
        SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=Path("ok")),
    )
    monkeypatch.setattr(llm, "WORKER", [str(FAKE)])
    monkeypatch.setattr(llm, "installed", lambda: True)
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.log")
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(chat, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(llm, "closed", False)
    yield
    for run in list(chat.live.values()):  # a failed test must not leave a turn waiting
        run.stop = True
        run.abort.set()
        if run.approval is not None:
            run.approval.answered.set()
    llm.close("test over")
    llm.release()


@pytest.fixture
def ws() -> FakeWorkstation:
    return FakeWorkstation()


@pytest.fixture
def box() -> FakeSandbox:
    return FakeSandbox()


@pytest.fixture
def shell() -> FakeShell:
    return FakeShell()


@pytest.fixture
def net() -> FakeNet:
    return FakeNet()


@pytest.fixture(autouse=True)
def reg(monkeypatch, ws, box, shell, net):
    r = build_registry(
        ws,
        Settings(marginalia=False),
        net=Net(net.resolve, net.transport),
        sandbox=box,
        limits=Limits(),
        shell=shell,
    )
    monkeypatch.setattr(chat, "registry", lambda: r)
    return r


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    """These tests check the mode's own tools: the providers (host tools, memory, MCP, skills) have their own tests,
    and test_agent_platform.py checks the app's providers in a chat."""
    monkeypatch.setattr(chat.providers, "load", list)
    monkeypatch.setattr(chat, "provided", {})


@pytest.fixture(autouse=True)
def online(monkeypatch):
    """Every chat starts online unless a test says otherwise: the real check reads this Mac's routing table."""
    monkeypatch.setattr(chat, "online_check", lambda: True)


@pytest.fixture
def client():
    return app_client()


def events(response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def wait_for(cond, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def agent_chat(client) -> str:
    return client.post("/api/chats", json={"mode": "agent"}).json()["id"]


def script(*steps) -> str:
    return "STEPS:" + json.dumps(list(steps))


def call(name, **arguments) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def send(client, cid, text) -> list[dict[str, Any]]:
    return events(client.post(f"/api/chats/{cid}/messages", data={"text": text}))


def start(client, cid, text) -> tuple[threading.Thread, dict[str, Any]]:
    """Send in a thread (the response ends with the answer); box["r"] gets the response."""
    box: dict[str, Any] = {}
    t = threading.Thread(
        target=lambda: box.update(
            r=client.post(f"/api/chats/{cid}/messages", data={"text": text})
        ),
        daemon=True,
    )
    t.start()
    return t, box


def pending(cid, n=1) -> dict[str, Any]:
    """The n-th approval event of the running answer, once it is there."""

    def found():
        run = chat.live.get(cid)
        asks = [e for e in run.events if e["event"] == "approval"] if run else []
        return asks[n - 1] if len(asks) >= n else None

    assert wait_for(lambda: found() is not None and chat.live[cid].approval is not None)
    return found()


def approve(client, cid, call_id, decision, headers=ORIGIN):
    return client.post(
        f"/api/chats/{cid}/approve",
        json={"call_id": call_id, "decision": decision},
        headers=headers,
    )


def saved(client, cid) -> dict[str, Any]:
    return client.get(f"/api/chats/{cid}").json()


def kinds(evs) -> list[str]:
    return [e["event"] for e in evs if e["event"] not in ("content", "reasoning")]


# ---------- a tool turn ----------


def test_a_tool_turn_end_to_end(client):
    cid = agent_chat(client)
    evs = send(client, cid, script([call("list_workstation_tools")], "RESULT"))
    assert kinds(evs) == ["step", "tool_call", "tool_result", "step", "done"]
    tc = next(e for e in evs if e["event"] == "tool_call")
    assert tc["name"] == "list_workstation_tools" and tc["risk"] == 0
    assert tc["label"] == "Media tools"
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok" and "image.klein" in res["content"]
    done = evs[-1]
    assert done["finish"] == "stop" and done["content"].startswith(
        "Result: image.klein"
    )
    assert done["stats"]["steps"] == 2
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["finish"] == "tool_calls"
    assert msgs[1]["tool_calls"][0]["name"] == "list_workstation_tools"
    assert msgs[2]["tool_call_id"] == msgs[1]["tool_calls"][0]["id"]
    assert msgs[2]["status"] == "ok" and "FLUX.3 Klein" in msgs[2]["content"]
    assert msgs[3]["finish"] == "stop"


def test_the_next_turn_sends_the_history_as_the_template_needs_it(client, reg):
    cid = agent_chat(client)
    send(
        client,
        cid,
        script([call("job_status", job_id="20261103-120000-abcd")], "RESULT"),
    )
    done = send(client, cid, "and now?")[-1]
    seen = done["stats"]["seen"]
    sent = seen["messages"]
    assert [m["role"] for m in sent] == [
        "system", "user", "assistant", "tool", "assistant", "user"
    ]  # fmt: skip
    step = sent[2]
    assert step["tool_calls"] == [
        {
            "id": step["tool_calls"][0]["id"],
            "name": "job_status",
            "arguments": {"job_id": "20261103-120000-abcd"},
        }
    ]
    tool = sent[3]
    assert set(tool) == {"role", "tool_call_id", "name", "content"}
    assert tool["tool_call_id"] == step["tool_calls"][0]["id"]
    assert "status: running" in tool["content"]
    assert llm.model_messages(sent) == sent  # already in the template's shape
    # the tools array: the core tools that are on, without run_python (off) and the file tools (no folder); the media
    # tools need no folder (they also read the work folder and the Library); then the bridge to the deferred tools
    names = [t["function"]["name"] for t in seen["tools"]]
    assert names == [
        "update_plan", "list_workstation_tools", "job_status", "search_library", "create_job", "wait_for_job",
        "media_info", "web_search", "fetch_url", "tool_search", "tool_describe", "tool_call",
    ]  # fmt: skip
    system = sent[0]["content"]
    assert "untrusted data" in system and "emoji" in system
    assert "no internet access" not in system


def test_regenerate_drops_the_steps_and_tool_messages(client):
    cid = agent_chat(client)
    send(client, cid, script([call("list_workstation_tools")], "RESULT"))
    client.post(f"/api/chats/{cid}/messages", data={"regenerate": "true"})
    roles = [m["role"] for m in saved(client, cid)["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


# ---------- approvals ----------


def test_allow_once_runs_the_call_and_taints_the_chat(client, net):
    cid = agent_chat(client)
    t, box = start(
        client, cid, script([call("web_search", query="Antikythera")], "RESULT")
    )
    ask = pending(cid)
    assert ask["name"] == "web_search" and ask["chat_scope"] is False
    assert ask["describe"] == "Sends the query 'Antikythera' to en.wikipedia.org."
    assert ask["timeout_s"] > 0 and ask["risk"] == 2
    assert ask["host"] is None  # only fetch_url names one host
    assert not net.requests  # nothing left the Mac before the answer
    assert approve(client, cid, ask["id"], "once").json()["decision"] == "once"
    t.join(5)
    evs = events(box["r"])
    assert kinds(evs) == [
        "step", "tool_call", "approval", "approval_done", "tool_result", "step", "done"
    ]  # fmt: skip
    assert next(e for e in evs if e["event"] == "approval_done")["decision"] == "once"
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok" and "Antikythera mechanism" in res["content"]
    assert res["approval"] == "once"
    assert len(net.requests) == 1
    state = saved(client, cid)["agent"]
    assert state["grants"] == [] and state["tainted"] is True


def test_allow_in_this_chat_skips_the_next_question(client, ws):
    cid = agent_chat(client)
    steps = script(
        [call("create_job", tool="image.klein", fields={"prompt": "a fox"})],
        [call("create_job", tool="image.klein", fields={"prompt": "a hare"})],
        "RESULT",
    )
    t, box = start(client, cid, steps)
    ask = pending(cid)
    assert "Queues a job for image.klein" in ask["describe"]
    approve(client, cid, ask["id"], "chat")
    t.join(5)
    evs = events(box["r"])
    assert [e["event"] for e in evs].count("approval") == 1  # create_job does not taint
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["ok", "ok"]
    assert "Do not wait for it" in results[0]["content"]
    assert ws.created == [
        ("image.klein", {"prompt": "a fox"}),
        ("image.klein", {"prompt": "a hare"}),
    ]
    state = saved(client, cid)["agent"]
    assert state["grants"] == ["create_job"] and state["tainted"] is False


def test_untrusted_content_makes_a_network_tool_ask_again(client):
    cid = agent_chat(client)
    steps = script(
        [call("web_search", query="one")], [call("web_search", query="two")], "RESULT"
    )
    t, box = start(client, cid, steps)
    first = pending(cid)
    # its result taints the chat, so "Allow in this chat" is not offered: it would not hold
    assert first["chat_scope"] is False
    assert approve(client, cid, first["id"], "chat").status_code == 400
    approve(client, cid, first["id"], "once")
    second = pending(cid, 2)
    assert second["chat_scope"] is False and "Untrusted content" in second["reason"]
    assert approve(client, cid, second["id"], "once").status_code == 200
    t.join(5)
    assert events(box["r"])[-1]["finish"] == "stop"
    state = saved(client, cid)["agent"]
    assert state["grants"] == [] and state["tainted"] is True


def test_the_agent_prompt_names_the_offered_and_the_off_tools(client, reg):
    cid = agent_chat(client)
    prompt = chat.system_prompt(saved(client, cid))
    # no folder: the file tools are on but deferred; the core tools that can run are in the array
    assert (
        "You can call tools: update_plan, list_workstation_tools, job_status, search_library, create_job, "
        "wait_for_job, media_info, web_search, fetch_url.\n" in prompt
    )
    # the listing of the deferred tools, one line per group
    assert "More tools are not loaded" in prompt
    assert (
        "- Media jobs: list_jobs, cancel_job, transcribe_audio, ocr_document, system_status\n"
        in prompt
    )
    assert (
        "- Files (need a granted folder): list_files, read_file, search_files, write_file, edit_file, "
        "copy_to_workspace, export_from_workspace\n" in prompt
    )
    assert "- Media files: view_image\n" in prompt and "read_result" not in prompt
    assert (
        "Call a tool listed below by its name. Use tool_search only when no name below fits"
        in prompt
    )
    assert (
        "To put a file that a media job made into the user's folder, call export_from_workspace with its "
        "Library reference as path." in prompt
    )
    assert "You reach the internet only through" in prompt
    assert (
        "These tools are off in this chat: run_python, run_shell. Never call them."
        in prompt
    )
    assert "write a short plan with update_plan" in prompt
    # without export_from_workspace, a media job's file stays in the Library
    client.patch(
        f"/api/chats/{cid}/agent",
        json={"tools": ["create_job", "write_file"]},
        headers=ORIGIN,
    )
    prompt = chat.system_prompt(saved(client, cid))
    assert (
        "A file that a media job made stays in the Library: give the user its Library reference."
        in prompt
    )
    assert "export_from_workspace with its" not in prompt
    client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["job_status"]}, headers=ORIGIN
    )
    prompt = chat.system_prompt(saved(client, cid))
    assert "You can call tools: job_status.\n" in prompt
    assert "More tools are not loaded" not in prompt  # nothing deferred is on
    assert (
        "These tools are off in this chat: update_plan, list_workstation_tools, search_library, create_job, "
        "list_jobs, wait_for_job, cancel_job, transcribe_audio, ocr_document, system_status, list_files, read_file, "
        "search_files, "
        "write_file, edit_file, copy_to_workspace, export_from_workspace, media_info, view_image, web_search, "
        "fetch_url, run_python, run_shell." in prompt
    )
    assert "Tools menu" in prompt and "no internet access in this chat" in prompt
    assert "You reach the internet" not in prompt and "short plan" not in prompt


def test_the_agent_prompt_does_not_change_between_turns(client):
    cid = agent_chat(client)
    send(client, cid, "hi")
    before = chat.system_prompt(saved(client, cid))
    send(client, cid, script([call("job_status", job_id="x")], "RESULT"))
    after = chat.system_prompt(saved(client, cid))
    assert after == before
    assert (
        chat.AGENT_EXAMPLES in before and len(chat.AGENT_EXAMPLES) < 1800
    )  # ~600 tokens


def test_a_denied_call_goes_back_to_the_model(client, net):
    cid = agent_chat(client)
    t, box = start(client, cid, script([call("web_search", query="x")], "RESULT"))
    approve(client, cid, pending(cid)["id"], "deny")
    t.join(5)
    evs = events(box["r"])
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "denied" and res["approval"] == "deny"
    assert evs[-1]["finish"] == "stop"
    assert (
        "The user denied this call" in evs[-1]["content"]
    )  # the model read the denial
    assert not net.requests
    assert saved(client, cid)["agent"]["tainted"] is False


def test_an_unanswered_approval_is_denied_after_the_timeout(client, monkeypatch):
    monkeypatch.setattr(chat, "APPROVAL_SECONDS", 0.3)
    cid = agent_chat(client)
    evs = send(client, cid, script([call("web_search", query="x")], "RESULT"))
    assert next(e for e in evs if e["event"] == "approval")["timeout_s"] == 0
    assert (
        next(e for e in evs if e["event"] == "approval_done")["decision"] == "timeout"
    )
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "denied" and res["approval"] == "timeout"
    assert evs[-1]["finish"] == "stop"


def test_stop_while_waiting_for_approval(client, net):
    cid = agent_chat(client)
    t, box = start(client, cid, script([call("web_search", query="x")], "RESULT"))
    ask = pending(cid)
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    evs = events(box["r"])
    assert (
        next(e for e in evs if e["event"] == "approval_done")["decision"] == "stopped"
    )
    assert evs[-1]["event"] == "done" and evs[-1]["finish"] == "cancelled"
    assert approve(client, cid, ask["id"], "once").status_code == 409
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[2]["status"] == "cancelled" and msgs[3]["finish"] == "cancelled"
    assert not net.requests and not saved(client, cid)["running"]


def test_stop_while_a_tool_runs(client, box):
    cid = agent_chat(client)
    client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["run_python"]}, headers=ORIGIN
    )
    t, resp = start(client, cid, script([call("run_python", code="WAIT")], "RESULT"))
    approve(client, cid, pending(cid)["id"], "once")
    assert wait_for(lambda: box.codes == ["WAIT"])
    client.post(f"/api/chats/{cid}/stop")
    t.join(5)
    evs = events(resp["r"])
    assert next(e for e in evs if e["event"] == "tool_result")["status"] == "cancelled"
    assert evs[-1]["finish"] == "cancelled"


def test_resume_replays_the_steps_and_a_pending_approval(client):
    cid = agent_chat(client)
    t, box = start(client, cid, script([call("web_search", query="x")], "RESULT"))
    ask = pending(cid)
    replay = []
    with client.stream("GET", f"/api/chats/{cid}/stream") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                replay.append(json.loads(line[6:]))
                if replay[-1]["event"] == "approval":
                    break
    assert kinds(replay) == ["step", "tool_call", "approval"]
    assert replay[-1]["id"] == ask["id"]
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    assert events(box["r"])[-1]["finish"] == "stop"


def test_approve_needs_the_app_s_origin(client):
    cid = agent_chat(client)
    t, _ = start(client, cid, script([call("web_search", query="x")], "RESULT"))
    ask = pending(cid)
    assert approve(client, cid, ask["id"], "once", headers={}).status_code == 403
    evil = {"Origin": "http://evil.example"}
    assert approve(client, cid, ask["id"], "once", headers=evil).status_code == 403
    assert approve(client, cid, "call_other", "once").status_code == 409
    assert chat.live[cid].approval is not None  # still waiting
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    assert approve(client, cid, ask["id"], "once").status_code == 409  # answered
    r = client.patch(f"/api/chats/{cid}/agent", json={"tools": []})
    assert r.status_code == 403


# ---------- limits and bad calls ----------


def test_max_steps(client, monkeypatch):
    assert chat.MAX_STEPS == 16 and chat.TURN_SECONDS == 600.0
    monkeypatch.setattr(chat, "MAX_STEPS", 3)
    cid = agent_chat(client)
    steps = script(*[[call("job_status", job_id=f"j{i}")] for i in range(5)])
    evs = send(client, cid, steps)
    assert [(e["n"], e["max"], e["last"]) for e in evs if e["event"] == "step"] == [
        (1, 3, False), (2, 3, False), (3, 3, True)
    ]  # fmt: skip
    # j0, j1: no such job. The last step cannot call tools, so it answers with a text.
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["error", "error"]
    done = evs[-1]
    assert done["finish"] == "steps" and done["content"] == "FINAL"
    assert "limit of 3 steps" in done["note"]
    assert done["stats"]["steps"] == 3 and done["stats"]["tool_calls"] == 2
    seen = done["stats"]["seen"]  # the last request
    assert seen["tool_choice"] == "none"
    assert [t["function"]["name"] for t in seen["tools"]][:2] == [
        "update_plan", "list_workstation_tools"
    ]  # fmt: skip
    note = seen["messages"][-1]
    assert note["role"] == "user" and "limit of 3 steps" in note["content"][0]["text"]
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs].count("tool") == 2
    assert msgs[-1]["finish"] == "steps" and msgs[-1]["content"] == "FINAL"
    assert msgs[-1]["nudge"] == note["content"][0]["text"]
    # the next turn sees the note where the last step saw it, so the prompt cache still fits
    sent = send(client, cid, "and now?")[-1]["stats"]["seen"]["messages"]
    assert sent[: len(seen["messages"])] == seen["messages"]
    assert [m["role"] for m in sent[len(seen["messages"]) :]] == ["assistant", "user"]
    assert sent[1]["role"] == "user" and sent[-1]["role"] == "user"


def test_without_tools_the_last_step_is_a_normal_one(client, monkeypatch):
    monkeypatch.setattr(chat, "MAX_STEPS", 1)
    cid = agent_chat(client)
    client.patch(f"/api/chats/{cid}/agent", json={"tools": []}, headers=ORIGIN)
    evs = send(client, cid, "hi")
    assert [e["last"] for e in evs if e["event"] == "step"] == [False]
    done = evs[-1]
    assert done["finish"] == "stop" and done["note"] == ""
    assert done["content"] == "Hello world"  # no tools: the fake answers without them


def test_a_call_in_the_last_step_does_not_run(client, monkeypatch):
    """The worker bans the call marker in the last step; if a call came anyway, it is skipped, not run."""
    monkeypatch.setattr(chat, "MAX_STEPS", 1)
    real = chat.Turn.generate

    def ignore_the_ban(self, messages, images, schemas, last=False):
        assert last and messages[-1]["role"] == "user"  # the app's note
        return real(self, messages[:-1], images, schemas, False)

    monkeypatch.setattr(chat.Turn, "generate", ignore_the_ban)
    cid = agent_chat(client)
    evs = send(client, cid, script([call("list_workstation_tools")]))
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "skipped" and "limit of 1 steps" in res["content"]
    assert evs[-1]["finish"] == "steps" and evs[-1]["content"] == ""


def test_tool_cards_say_whether_the_tool_leaves_this_mac(client):
    """The tool_call and tool_result events and the saved tool message carry the tool's network flag, so the page
    labels a local R2 tool (create_job) "Uses this Mac", not "Leaves this Mac"."""
    cid = agent_chat(client)
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data["agent"]["grants"] = ["web_search", "create_job"]
    path.write_text(json.dumps(data))
    job = call("create_job", tool="image.klein", fields={"prompt": "a fox"})
    evs = send(client, cid, script([job, call("web_search", query="fox")], "RESULT"))
    for kind in ("tool_call", "tool_result"):
        assert [(e["name"], e["network"]) for e in evs if e["event"] == kind] == [
            ("create_job", False),
            ("web_search", True),
        ]
    tools = [m for m in saved(client, cid)["messages"] if m["role"] == "tool"]
    assert [m["network"] for m in tools] == [False, True]


def test_a_repeated_call_points_to_the_first_result(client):
    cid = agent_chat(client)
    steps = script(
        [call("list_workstation_tools")], [call("list_workstation_tools")], "RESULT"
    )
    evs = send(client, cid, steps)
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["ok", "repeat"]
    assert results[1]["content"] == chat.REPEAT and results[1]["seconds"] is None
    done = evs[-1]
    assert done["finish"] == "stop" and done["content"] == "Result: " + chat.REPEAT


def test_a_third_identical_call_ends_the_answer(client):
    cid = agent_chat(client)
    same = [call("list_workstation_tools")]
    evs = send(client, cid, script(same, same, same, "RESULT"))
    results = [e["status"] for e in evs if e["event"] == "tool_result"]
    assert results == ["ok", "repeat", "skipped"]
    assert evs[-1]["finish"] == "repeat" and "three times" in evs[-1]["note"]


def test_repeats_within_one_step(client):
    cid = agent_chat(client)
    same = call("update_plan", items=[{"step": "Look", "status": "pending"}])
    evs = send(client, cid, script([same, same, call("list_workstation_tools")]))
    assert [e["status"] for e in evs if e["event"] == "tool_result"] == [
        "ok", "repeat", "ok"
    ]  # fmt: skip
    assert evs[-1]["finish"] == "stop"
    evs = send(client, cid, script([same, same, same, call("list_workstation_tools")]))
    assert [e["status"] for e in evs if e["event"] == "tool_result"] == [
        "ok", "repeat", "skipped", "skipped"
    ]  # fmt: skip
    assert evs[-1]["finish"] == "repeat"


def test_polling_calls_run_again(client, ws):
    """A tool whose result can change (Tool.repeatable) is outside the repeat rule: each wait and each status read
    runs and shows the job as it is now."""
    cid = agent_chat(client)
    jid = ws.create_job("image.klein", {"prompt": "x"}, None, cid)
    wait = [call("wait_for_job", job_id=jid, seconds=1)]
    evs = send(client, cid, script(wait, wait, wait, "RESULT"))
    assert [e["status"] for e in evs if e["event"] == "tool_result"] == ["ok"] * 3
    assert len(ws.waits) == 3 and evs[-1]["finish"] == "stop"
    ws.finish[jid] = {"status": "done"}
    status = [call("job_status", job_id=jid)]
    steps = script(status, [call("wait_for_job", job_id=jid, seconds=1)], status)
    results = [e for e in send(client, cid, steps) if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["ok"] * 3
    assert "status: queued" in results[0]["content"]
    assert "status: done" in results[2]["content"]


def test_only_an_ok_result_counts_as_made(client):
    """An identical call after an error runs again once (an error can pass), with a note; the third identical
    failure is not run and ends the answer. An ok result in between starts the count again."""
    cid = agent_chat(client)
    bad = [call("update_plan", items=[{"step": "Look", "status": "bogus"}])]
    evs = send(client, cid, script(bad, bad, bad, "RESULT"))
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["error", "error", "skipped"]
    assert chat.FAILED_AGAIN not in results[0]["content"]
    assert results[1]["content"].endswith(chat.FAILED_AGAIN)
    assert evs[-1]["finish"] == "repeat" and evs[-1]["note"] == chat.NOTE_FAILED
    good = [call("update_plan", items=[{"step": "Look", "status": "pending"}])]
    evs = send(client, cid, script(bad, bad, good, bad, "RESULT"))
    statuses = [e["status"] for e in evs if e["event"] == "tool_result"]
    assert statuses == ["error", "error", "ok", "error"] and evs[-1]["finish"] == "stop"


def test_a_malformed_call_written_again_ends_the_answer(client):
    """The same text that does not parse, three times: the third is not run. The model sees its own text in the
    next request (not an empty call), so it can fix it."""
    cid = agent_chat(client)
    bad = {"raw": "<function=job_status>\n<parameter=job_id>\nx\n"}
    evs = send(client, cid, script([bad], [bad], [bad], "RESULT"))
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["error", "error", "skipped"]
    assert chat.FAILED_AGAIN in results[1]["content"]
    assert evs[-1]["finish"] == "repeat" and evs[-1]["note"] == chat.NOTE_FAILED
    sent, _ = chat.model_input(saved(client, cid), 256)
    first = next(m for m in sent if m.get("tool_calls"))["tool_calls"][0]
    assert first["name"] == "job_status" and first["arguments"] == {"job_id": "x"}


def test_a_call_that_changes_state_clears_the_repeat_rule(client, ws):
    """After create_job ran, an earlier identical call runs again (its result may be stale). The state-changing
    call itself still counts: the same job is not queued twice."""
    cid = agent_chat(client)
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data["agent"]["grants"] = ["create_job"]
    path.write_text(json.dumps(data))
    look = [call("update_plan", items=[{"step": "Look", "status": "pending"}])]
    job = [call("create_job", tool="image.klein", fields={"prompt": "a fox"})]
    evs = send(client, cid, script(look, job, look, job, "RESULT"))
    assert [e["status"] for e in evs if e["event"] == "tool_result"] == [
        "ok", "ok", "ok", "repeat"
    ]  # fmt: skip
    assert ws.created == [("image.klein", {"prompt": "a fox"})]


# ---------- the plan ----------


def plan(*items) -> dict[str, Any]:
    return call("update_plan", items=[{"step": s, "status": st} for s, st in items])


def test_the_plan_is_sent_live_and_saved(client):
    cid = agent_chat(client)
    steps = script(
        [plan(("Look up  the year", "in_progress"), ("Compare", "pending"))],
        [plan(("Look up  the year", "done"), ("Compare", "done"))],
        "RESULT",
    )
    evs = send(client, cid, steps)
    assert "approval" not in kinds(evs)  # R0: it never asks
    plans = [e for e in evs if e["event"] == "plan"]
    assert [p["items"] for p in plans] == [
        [
            {"step": "Look up the year", "status": "in_progress"},
            {"step": "Compare", "status": "pending"},
        ],
        [
            {"step": "Look up the year", "status": "done"},
            {"step": "Compare", "status": "done"},
        ],
    ]
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["id"] for r in results] == [p["id"] for p in plans]
    assert results[0]["content"] == (
        "Plan saved: 0 of 2 done. Current step: Look up the year"
    )
    assert "Every step is done" in results[1]["content"]
    tools = [m for m in saved(client, cid)["messages"] if m["role"] == "tool"]
    assert [t["plan"] for t in tools] == [p["items"] for p in plans]
    assert tools[0]["label"] == "Plan"
    assert saved(client, cid)["agent"]["tainted"] is False


def test_a_bad_plan_goes_back_to_the_model(client):
    cid = agent_chat(client)
    steps = script(
        [plan(("a", "in_progress"), ("b", "in_progress"))],
        [plan(("a", "started"))],
        [call("update_plan", items=[])],
        "RESULT",
    )
    evs = send(client, cid, steps)
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == ["error"] * 3
    assert "More than one item is in_progress" in results[0]["content"]
    assert "must be one of" in results[1]["content"]
    assert "The plan is empty" in results[2]["content"]
    assert not [e for e in evs if e["event"] == "plan"]
    tools = [m for m in saved(client, cid)["messages"] if m["role"] == "tool"]
    assert not any("plan" in t for t in tools)


# ---------- a call in the reasoning ----------


def test_a_call_written_in_the_reasoning_runs(client):
    cid = agent_chat(client)
    steps = script(
        {
            "think": "The user wants the tools.",
            "think_calls": [call("list_workstation_tools")],
        },
        "RESULT",
    )
    evs = send(client, cid, steps)
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["name"] == "list_workstation_tools" and res["status"] == "ok"
    assert evs[-1]["finish"] == "stop" and evs[-1]["content"].startswith("Result: ")
    step = saved(client, cid)["messages"][1]
    assert step["reasoning"] == "The user wants the tools."
    assert step["tool_calls"][0]["name"] == "list_workstation_tools"


def test_malformed_unknown_and_disabled_calls_go_back_to_the_model(client, box):
    cid = agent_chat(client)
    bad = {"raw": "<function=web_search>\n<parameter=query>\nx\n</parameter>\n"}
    steps = script(
        [
            bad,
            call("no_such_tool"),
            call("run_python", code="print(1)"),
            call("read_file", path="a.txt"),
            call("job_status", wrong="x"),
        ],
        "RESULT",
    )
    evs = send(client, cid, steps)
    results = [e for e in evs if e["event"] == "tool_result"]
    assert [r["status"] for r in results] == [
        "error",
        "refused",
        "refused",
        "refused",
        "error",
    ]
    assert "Invalid tool call" in results[0]["content"]
    assert "There is no tool 'no_such_tool'" in results[1]["content"]
    assert "off in this chat" in results[2]["content"]
    assert "No folder is granted" in results[3]["content"]
    assert "Invalid arguments for job_status" in results[4]["content"]
    assert evs[-1]["finish"] == "stop" and not box.codes
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] + ["tool"] * 5 + [
        "assistant"
    ]


def test_a_model_error_ends_the_answer_and_saves_the_steps(client, monkeypatch):
    cid = agent_chat(client)

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat.Turn, "call", broken)
    evs = send(client, cid, script([call("list_workstation_tools")]))
    assert evs[-1] == {"event": "error", "message": "boom"}
    msgs = saved(client, cid)["messages"]
    assert msgs[-1]["finish"] == "error" and not saved(client, cid)["running"]


# ---------- folder, tools and the workspace ----------


def test_folder_grant_validation(client, tmp_path, monkeypatch):
    # the temp folder is under /private/var, which a real grant refuses (test_shared_and_other_folders_are_refused)
    monkeypatch.setattr(chat, "SHARED_FOLDERS", ())
    cid = agent_chat(client)
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    notes = home / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("first note")
    (home / ".hidden").mkdir()
    (home / "file.txt").write_text("x")

    def grant(path):
        return client.patch(
            f"/api/chats/{cid}/agent", json={"folder": path}, headers=ORIGIN
        )

    for bad in (
        "notes",
        str(home / "missing"),
        str(home / "file.txt"),
        "/",
        "/Users",
        str(home),
        str(home.parent),
        "/usr/bin",
        "/System/Library",
        str(home / "Library"),
        str(home / ".hidden"),
    ):
        r = grant(bad)
        assert r.status_code == 400, bad
        assert r.json()["detail"]
    os.symlink(home, tmp_path / "link")
    assert grant(str(tmp_path / "link")).status_code == 400  # resolved: the home folder
    r = grant(str(notes) + "/.")
    assert r.status_code == 200 and r.json()["folder"] == str(notes.resolve())
    # the file tools are offered now and read inside the folder
    done = send(client, cid, script([call("read_file", path="a.txt")], "RESULT"))[-1]
    assert "first note" in done["content"]
    step = saved(client, cid)["messages"][1]
    seen = step["stats"]["seen"]
    assert "read_file" in [t["function"]["name"] for t in seen["tools"]]
    assert str(notes.resolve()) in seen["messages"][0]["content"]
    assert saved(client, cid)["agent"]["folder"] == str(notes.resolve())
    r = client.patch(f"/api/chats/{cid}/agent", json={"folder": None}, headers=ORIGIN)
    assert r.json()["folder"] is None
    # another letter case is stored as the disk spells it, so the file tools' check matches
    upper = Path(str(notes).replace("/notes", "/NOTES"))
    if upper.exists():  # a case-insensitive file system
        assert grant(str(upper)).json()["folder"] == str(notes.resolve())


def test_folders_with_programs_or_code_are_refused(client, tmp_path, monkeypatch):
    """Programs (/Applications, ~/Applications, /opt), bundles (.app, .framework, ...) and Python's code folders are
    never granted: a file written there can run."""
    monkeypatch.setattr(
        chat, "SHARED_FOLDERS", ()
    )  # the temp folder is under /private/var
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cid = agent_chat(client)
    made = [
        home / "Applications" / "Tools",
        home / "Dev" / "Thing.app" / "Contents",
        home / "Dev" / "Kit.framework",
        home / "Dev" / "venv" / "lib" / "site-packages" / "pkg",
        home / "Dev" / "dist-packages",
    ]
    for path in made:
        path.mkdir(parents=True)
    existing = [
        p for p in ("/opt/homebrew", "/Applications/Utilities") if Path(p).is_dir()
    ]
    for bad in [str(p) for p in made] + existing:
        r = client.patch(
            f"/api/chats/{cid}/agent", json={"folder": bad}, headers=ORIGIN
        )
        assert r.status_code == 400, bad
        detail = r.json()["detail"]
        assert "programs or code" in detail or "system folder" in detail, detail
    (home / "Dev" / "notes").mkdir()
    r = client.patch(
        f"/api/chats/{cid}/agent",
        json={"folder": str(home / "Dev" / "notes")},
        headers=ORIGIN,
    )
    assert r.status_code == 200


def test_shared_and_other_folders_are_refused(client, tmp_path, monkeypatch):
    """/var, /tmp and what is inside them, other users' home folders and the app's own folder are never granted."""
    cid = agent_chat(client)

    def grant(path):
        return client.patch(
            f"/api/chats/{cid}/agent", json={"folder": str(path)}, headers=ORIGIN
        )

    inside_tmp = tmp_path / "work"  # pytest's temp folder: under /private/var/folders
    inside_tmp.mkdir()
    for bad in ("/var", "/private/var", "/tmp", "/private/tmp", "/var/tmp", inside_tmp):
        r = grant(bad)
        assert r.status_code == 400, bad
    assert "shared folder" in grant(inside_tmp).json()["detail"]
    others = [
        p for p in Path("/Users").iterdir() if p.is_dir() and p != Path.home()
    ]  # /Users/Shared at least
    for other in others:
        r = grant(other)
        assert r.status_code == 400, other
        assert "not in your home folder" in r.json()["detail"]
    for own in (chat.ROOT, chat.ROOT / "app", chat.ROOT / "docs"):
        r = grant(own)
        assert r.status_code == 400, own
        # a checkout under /private/tmp meets the shared-folder rule first
        assert any(
            m in r.json()["detail"] for m in ("app's own folder", "shared folder")
        )
    # the folder that holds the app is refused too; when it is the home folder, that rule answers first
    assert grant(chat.ROOT.parent).status_code == 400
    # the checks hold in another letter case too (APFS ignores it)
    if Path("/LIBRARY").exists():
        assert "system folder" in grant("/LIBRARY/Preferences").json()["detail"]
        assert grant("/PRIVATE/VAR/folders").status_code == 400


def test_tools_menu_state(client, reg):
    cid = agent_chat(client)
    view = client.get(f"/api/chats/{cid}/agent").json()
    by = {t["name"]: t for t in view["tools"]}
    # the app's tools, then the providers' tools a first turn would freeze (app/agent/providers.py)
    assert list(by)[: len(reg.names())] == reg.names()
    if "run_shortcut" in by:  # host tools: /usr/bin/shortcuts exists
        assert by["run_shortcut"]["enabled"] is False
        assert by["run_shortcut"]["label"] == "Run a shortcut"
    assert by["run_python"]["enabled"] is False and by["web_search"]["enabled"] is True
    assert "Asks before every call" in by["run_python"]["risk_text"]
    assert by["run_shell"]["enabled"] is False and by["run_shell"]["risk"] == 3
    assert by["run_shell"]["label"] == "Shell"
    assert "Asks before every call" in by["run_shell"]["risk_text"]
    assert "granted folder without changing it" in by["run_shell"]["risk_text"]
    assert "Sends text off this Mac" in by["web_search"]["risk_text"]
    assert by["web_search"]["network"] is True and by["web_search"]["risk"] == 2
    assert view["limits"]["steps"] == chat.MAX_STEPS
    r = client.patch(
        f"/api/chats/{cid}/agent",
        json={"tools": ["web_search", "run_python"]},
        headers=ORIGIN,
    )
    assert r.json()["enabled"] == ["web_search", "run_python"]
    bad = client.patch(f"/api/chats/{cid}/agent", json={"tools": ["x"]}, headers=ORIGIN)
    assert bad.status_code == 400
    # revoke a chat-wide approval
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data["agent"]["grants"] = ["web_search", "create_job"]
    path.write_text(json.dumps(data))
    r = client.patch(
        f"/api/chats/{cid}/agent", json={"revoke": ["web_search"]}, headers=ORIGIN
    )
    assert r.json()["grants"] == ["create_job"]
    general = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    assert client.get(f"/api/chats/{general}/agent").status_code == 409


def test_shell_asks_every_time_and_shows_its_folders(
    client, shell, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        chat, "SHARED_FOLDERS", ()
    )  # the temp folder is under /private/var
    home = tmp_path / "home"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cid = agent_chat(client)
    r = client.patch(
        f"/api/chats/{cid}/agent",
        json={"tools": ["run_shell"], "folder": str(home / "notes")},
        headers=ORIGIN,
    )
    folder = r.json()["folder"]
    steps = script(
        [call("run_shell", command="ls -l | wc -l", timeout_s=30)],
        [call("run_shell", command="rm -rf x")],
        "RESULT",
    )
    t, resp = start(client, cid, steps)
    ask = pending(cid)
    assert (
        ask["name"] == "run_shell" and ask["risk"] == 3 and ask["chat_scope"] is False
    )
    assert ask["arguments"] == {"command": "ls -l | wc -l", "timeout_s": 30}
    assert ask["cwd"] == str(chat.CHATS / cid / "ws") and ask["read_only"] == folder
    assert "at most 30 s" in ask["describe"]
    assert approve(client, cid, ask["id"], "chat").status_code == 400
    approve(client, cid, ask["id"], "once")
    ask = pending(cid, 2)  # the next call asks again
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    assert shell.runs == [("ls -l | wc -l", Path(folder), 30.0)]
    results = [e for e in events(resp["r"]) if e["event"] == "tool_result"]
    assert results[0]["status"] == "ok"
    assert results[0]["content"].startswith(
        "exit code: 0 (0.02 s)\nstdout (7 characters):\nlisted\n"
    )
    assert results[1]["status"] == "denied"
    assert saved(client, cid)["agent"]["tainted"] is True


def test_code_runs_end_before_the_answer_does(reg, box, shell, tmp_path):
    """run_shell and run_python never run into the answer's last minute (ANSWER_RESERVE): the time limit is cut,
    and the result says so; with no time left the call does not run."""
    soon = time.monotonic() + 60 + 30.5  # 30 s left before the reserve
    ctx = Context("c1", None, threading.Event(), tmp_path / "ws", soon)
    r = reg.call("run_shell", {"command": "ls", "timeout_s": 300}, ctx)
    assert r.status == "ok" and shell.runs[-1][2] == 30.0
    assert r.content.startswith(
        "exit code: 0 (0.02 s)\nnote: the time limit was cut from 300 s to 30 s"
    )
    r = reg.call("run_python", {"code": "print(1)"}, ctx)
    assert r.status == "ok" and box.walls[-1] == 30.0
    assert "\nnote: the time limit was cut from 60 s to 30 s" in r.content
    # enough time: the limits stay, and no note
    later = Context(
        "c1", None, threading.Event(), tmp_path / "ws", time.monotonic() + 600
    )
    r = reg.call("run_shell", {"command": "ls", "timeout_s": 120}, later)
    assert shell.runs[-1][2] == 120.0 and "note:" not in r.content
    # no time left
    late = Context(
        "c1", None, threading.Event(), tmp_path / "ws", time.monotonic() + 30
    )
    runs = len(shell.runs)
    r = reg.call("run_shell", {"command": "ls"}, late)
    assert (
        r.status == "error" and "no time left" in r.content and len(shell.runs) == runs
    )
    assert reg.call("run_python", {"code": "1"}, late).status == "error"


def test_a_chat_with_immutable_files_is_deleted_whole(client):
    """Code in the sandbox can make a file of the work folder immutable (chflags uchg) or a folder unreadable. Deleting
    the chat still removes all of it, and the chat is never half deleted."""
    cid = agent_chat(client)
    ws = chat.CHATS / cid / "ws"
    (ws / "locked").mkdir(parents=True)
    (ws / "locked" / "f.txt").write_text("x")
    (ws / "closed").mkdir()
    (ws / "closed" / "g.txt").write_text("y")
    flagged = [ws / "locked" / "f.txt", ws / "locked"]
    try:
        for path in flagged:
            os.chflags(path, stat.UF_IMMUTABLE)
        os.chmod(ws / "closed", 0)
        r = client.delete(f"/api/chats/{cid}", headers=ORIGIN)
        assert r.status_code == 200 and r.json() == {"status": "deleted"}
        assert list(chat.CHATS.iterdir()) == []
        assert client.get("/api/chats").json() == []
        assert client.get(f"/api/chats/{cid}").status_code == 404
    finally:  # a failed test must not leave files that pytest cannot remove
        for path in flagged:
            if path.exists():
                os.chflags(path, 0)
        if (ws / "closed").exists():
            os.chmod(ws / "closed", 0o700)


def test_python_files_are_served_from_the_workspace_only(client, box):
    cid = agent_chat(client)
    client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["run_python"]}, headers=ORIGIN
    )
    t, resp = start(client, cid, script([call("run_python", code="PLOT")], "RESULT"))
    ask = pending(cid)
    assert ask["chat_scope"] is False and ask["risk"] == 3
    assert approve(client, cid, ask["id"], "chat").status_code == 400
    approve(client, cid, ask["id"], "once")
    t.join(5)
    res = next(e for e in events(resp["r"]) if e["event"] == "tool_result")
    assert res["files"] == [
        {"path": "plot.png", "url": f"/api/chats/{cid}/ws/plot.png", "kind": "image"}
    ]
    r = client.get(res["files"][0]["url"])
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    # the workspace's own policy stays, with the server's frame rule merged in
    assert r.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; frame-ancestors 'none'"
    )
    assert r.headers["x-frame-options"] == "DENY"
    ws_dir = chat.CHATS / cid / "ws"
    (ws_dir / "page.html").write_text("<script>alert(1)</script>")
    r = client.get(f"/api/chats/{cid}/ws/page.html")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    (ws_dir / "out.csv").write_text("a,b\n1,2\n")
    assert (
        client.get(f"/api/chats/{cid}/ws/out.csv")
        .headers["content-type"]
        .startswith("text/plain")
    )
    (ws_dir / "sub").mkdir()
    (ws_dir / "sub" / "x.txt").write_text("deep")
    assert client.get(f"/api/chats/{cid}/ws/sub/x.txt").text == "deep"
    (chat.CHATS / cid / "secret.txt").write_text("outside")
    os.symlink(chat.CHATS / cid / "secret.txt", ws_dir / "link.txt")
    os.symlink(chat.CHATS / cid, ws_dir / "up")
    (ws_dir / ".hidden").write_text("x")
    for bad in (
        "link.txt",
        "up/secret.txt",
        "..%2Fsecret.txt",
        ".hidden",
        "sub",
        "missing.png",
    ):
        assert client.get(f"/api/chats/{cid}/ws/{bad}").status_code == 404, bad
    assert client.get(f"/api/chats/{cid}/ws/../chat.json").status_code == 404


# ---------- the Workstation port and the other modes ----------


def test_the_server_port_queues_a_job_without_waiting(client, state, monkeypatch):
    """create_job through the server's real port: the job is queued (the worker thread never runs in tests) and the
    turn ends at once, before the job could start."""
    monkeypatch.setattr(tools, "REQUIRES", {})
    monkeypatch.setattr(server, "run_job", lambda job: pytest.fail("a job ran"))
    port = server.Workstation()
    r = build_registry(port, Settings(marginalia=False), sandbox=FakeSandbox())
    monkeypatch.setattr(chat, "registry", lambda: r)
    claims = []
    monkeypatch.setattr(llm, "claim", lambda *a, **kw: claims.append(a) or True)
    cid = agent_chat(client)
    steps = script(
        [
            call(
                "create_job", tool="image.klein", fields={"prompt": "a fox", "steps": 6}
            )
        ],
        "RESULT",
    )
    t, box = start(client, cid, steps)
    approve(client, cid, pending(cid)["id"], "once")
    t.join(5)
    assert not t.is_alive() and events(box["r"])[-1]["finish"] == "stop"
    (job,) = server.jobs.values()
    assert job["status"] == "queued" and job["params"]["prompt"] == "a fox"
    assert job["params"]["steps"] == "6" and not claims
    assert port.job_status(job["id"])["status"] == "queued"
    assert port.job_status("nope") is None
    with pytest.raises(ValueError, match="Steps must be between"):
        port.create_job("image.klein", {"prompt": "x", "steps": "999"})
    with pytest.raises(ValueError, match="needs an uploaded file"):
        port.create_job("video.i2v", {"prompt": "x"})
    with pytest.raises(ValueError, match="Unknown tool"):
        port.create_job("nope", {})
    assert (
        len(server.jobs) == 1 and len(list(state.iterdir())) == 1
    )  # no half-made job folders
    listed = {t["id"]: t for t in port.list_tools()}
    assert listed["image.klein"]["installed"] is True


def test_the_server_port_searches_the_library(state, make_job):
    make_job(
        "20261103-100000-aaaa",
        "done",
        outputs=[{"name": "image.png", "kind": "image", "url": ""}],
    )
    (state / "20261103-100000-aaaa" / "image.png").write_bytes(PNG)
    port = server.Workstation()
    (hit,) = port.search_library("LIGHTHOUSE", 5)
    assert hit["job"] == "20261103-100000-aaaa" and hit["text"] == "a lighthouse"
    assert set(hit) == {"job", "tool", "tool_label", "kind", "name", "created", "text"}
    assert port.search_library("nothing like this", 5) == []
    # any word matches, in any order and any case
    assert port.search_library("Lighthouse  A", 5) == [hit]
    assert port.search_library("lighthouse storm", 5) == [hit]
    assert port.search_library("20261103-100000-aaaa", 5) == [hit]  # by the job id too


def test_library_search_puts_the_most_matched_words_first(state, make_job):
    for jid, prompt in (
        ("20261103-100000-aaaa", "a lighthouse in the rain at dusk"),
        ("20261103-110000-bbbb", "a lighthouse at noon"),
        ("20261103-120000-cccc", "a cat"),
    ):
        make_job(
            jid,
            "done",
            params={"prompt": prompt},
            outputs=[{"name": "image.png", "kind": "image", "url": ""}],
        )
        (state / jid / "image.png").write_bytes(PNG)
    port = server.Workstation()
    found = [h["job"] for h in port.search_library("rain lighthouse extended", 5)]
    assert found == [
        "20261103-100000-aaaa",
        "20261103-110000-bbbb",
    ]  # two words, then one; the cat has none
    found = [h["job"] for h in port.search_library("lighthouse", 1)]
    assert found == ["20261103-110000-bbbb"]  # a tie: the newest


def test_the_chat_profile_has_no_workstation_tools(monkeypatch):
    monkeypatch.setattr(chat, "registry", REAL_REGISTRY)
    monkeypatch.setattr(chat, "registries", {})
    monkeypatch.setattr(chat, "settings", Settings())
    monkeypatch.setattr(
        chat.config,
        "APP",
        chat.config.App(**{**chat.config.APP_DEFAULTS, "profile": "chat"}),
    )
    names = chat.registry().names()
    assert "create_job" not in names and "web_search" in names
    monkeypatch.setattr(chat.config, "APP", chat.config.App(**chat.config.APP_DEFAULTS))
    assert "create_job" in chat.registry().names()


def test_other_modes_do_not_get_tools(client):
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    evs = send(client, cid, script([call("list_workstation_tools")]))
    assert kinds(evs) == ["done"]
    assert evs[-1]["content"] == "Hello world" and "tool_calls" not in evs[-1]
    assert "agent" not in saved(client, cid)


def test_model_input_counts_the_tools_and_shortens_old_results_first(monkeypatch):
    monkeypatch.setattr(chat, "CONTEXT_TOKENS", 4000)
    big = "r" * 3000  # 1000 tokens
    calls = [{"id": "c1", "name": "web_search", "arguments": {"query": "x"}}]
    msgs = [
        {"role": "user", "content": "one", "images": []},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "",
            "tool_calls": calls,
            "finish": "tool_calls",
        },
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": big},
        {
            "role": "assistant",
            "content": "answer one",
            "reasoning": "",
            "finish": "stop",
        },
        {"role": "user", "content": "two", "images": []},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "",
            "tool_calls": [{**calls[0], "id": "c2"}],
        },
        {"role": "tool", "tool_call_id": "c2", "name": "web_search", "content": big},
    ]
    c = {"id": "c", "mode": "agent", "agent": chat.default_agent(), "messages": msgs}
    full, _ = chat.model_input(c, 256)
    assert [m["role"] for m in full][1:] == [m["role"] for m in msgs]
    assert full[3]["content"] == big  # it fits without schemas
    # schemas that leave room for every message but one big result
    system = len(chat.system_prompt(c)) // 3
    total = sum(chat.tokens(m) for m in msgs)
    # (with the old result shortened, the messages fill less than TRIM_TO of that room)
    room = 4000 - 256 - system - (total - 100)
    schemas = [{"type": "function", "function": {"name": "f", "description": ""}}]
    schemas[0]["function"]["description"] = "d" * (3 * room - len(json.dumps(schemas)))
    trimmed = {**c}
    short, _ = chat.model_input(trimmed, 256, schemas)
    assert [m["role"] for m in short] == [m["role"] for m in full]  # nothing dropped
    assert short[3]["content"] == chat.SHORTENED and short[-1]["content"] == big
    assert trimmed["window"] == {"start": 0, "short": 3}  # the next turns keep the note
    # a result over MAX_RESULT goes in capped
    long = {**msgs[-1], "content": "z" * 30000}
    capped, _ = chat.model_input({**c, "messages": msgs[4:6] + [long]}, 256)
    assert len(capped[-1]["content"]) == chat.MAX_RESULT
    # when shortening is not enough, whole turns go: the newest turn stays with its calls and results
    newest = sum(chat.tokens(m) for m in msgs[4:])
    monkeypatch.setattr(chat, "CONTEXT_TOKENS", 256 + system + newest + 5)
    least, _ = chat.model_input(c, 256)
    assert [m["role"] for m in least] == ["system", "user", "assistant", "tool"]


def test_the_newest_step_s_results_stay_whole(monkeypatch):
    """Also in the last step (the app's note follows the results) and under a stored window that points past them
    (a regenerated or unsaved answer)."""
    monkeypatch.setattr(chat, "CONTEXT_TOKENS", 4000)
    calls = [{"id": "c", "name": "web_search", "arguments": {"query": "x"}}]

    def step(n, content):
        return [
            {"role": "assistant", "content": "", "reasoning": "", "tool_calls": calls},
            {
                "role": "tool",
                "tool_call_id": "c",
                "name": "web_search",
                "content": content,
            },
        ]

    msgs = [{"role": "user", "content": "one", "images": []}]
    for n in range(5):
        msgs += step(n, "o" * 900)
    msgs += step(5, "FRESH" + "n" * 4200)
    note = {"role": "user", "content": "no more tools", "images": [], "app": True}
    c = {
        "id": "c",
        "mode": "agent",
        "agent": chat.default_agent(),
        "messages": msgs + [note],
    }
    sent, _ = chat.model_input(c, 256)
    assert (
        sent[-2]["content"].startswith("FRESH")
        and sent[-1]["content"][0]["text"] == "no more tools"
    )
    assert sent[3]["content"] == chat.SHORTENED  # an older result made the room
    assert (
        c["window"]["short"] <= len(msgs) - 1
    )  # the newest result is not in the window
    stale = {**c, "messages": msgs, "window": {"start": 0, "short": len(msgs) + 5}}
    sent, _ = chat.model_input(stale, 256)
    assert sent[-1]["content"].startswith("FRESH")


def test_a_fifo_in_the_workspace_does_not_block_the_server(client):
    """The code can make a FIFO; opening it without O_NONBLOCK would wait for a writer for ever."""
    cid = agent_chat(client)
    ws_dir = chat.CHATS / cid / "ws"
    ws_dir.mkdir(parents=True)
    os.mkfifo(ws_dir / "pipe.txt")
    box: dict[str, Any] = {}
    t = threading.Thread(
        target=lambda: box.update(r=client.get(f"/api/chats/{cid}/ws/pipe.txt")),
        daemon=True,
    )
    t.start()
    t.join(5)
    if t.is_alive():  # unblock the server thread so the test run can end
        with open(ws_dir / "pipe.txt", "wb"):
            pass
        t.join(5)
        pytest.fail("GET of a FIFO blocked")
    assert box["r"].status_code == 404


def test_the_approval_card_shows_the_real_host_and_hidden_characters(client, net):
    """A look-alike host shows as IDNA on its own line; bidi, zero-width and control characters show as \\uXXXX."""
    cid = agent_chat(client)
    url = "https://\u0430pple.com/a\u202eb\u200bc\x00"  # a Cyrillic a, an override, a zero-width space, NUL
    t, _ = start(client, cid, script([call("fetch_url", url=url)], "RESULT"))
    ask = pending(cid)
    assert ask["host"] == "xn--pple-43d.com"
    assert ask["describe"] == (
        "Downloads https://\u0430pple.com/a\\u202eb\\u200bc\\u0000 (the site sees this Mac's IP address)."
    )
    assert ask["arguments"] == {
        "url": url
    }  # the page escapes the shown arguments itself
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    assert not net.requests


# ---------- waiting for memory, late answers, the app closing ----------


def test_a_long_media_job_before_the_turn_does_not_use_up_its_time(client, monkeypatch):
    """The turn waits for a big job longer than its own limit; the wait does not count, so the answer completes."""
    monkeypatch.setattr(chat, "TURN_SECONDS", 1.0)
    cid = agent_chat(client)
    assert llm.claim(35.0)  # a big media job holds the memory
    threading.Timer(1.5, llm.release).start()  # and runs longer than the turn limit
    evs = send(client, cid, script([call("list_workstation_tools")], "RESULT"))
    assert "waiting" in kinds(evs)
    assert evs[-1]["event"] == "done" and evs[-1]["finish"] == "stop", evs[-1]
    assert evs[-1]["stats"]["seconds"] >= 1.5  # the whole answer, the wait included
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok"


def test_the_lease_is_held_after_a_wait_longer_than_its_limit():
    """A session's limit counts from the moment it holds the memory, not from the with-block's start."""
    assert llm.claim(35.0)
    with llm.session(limit=1.0) as s:
        assert not s.held and s.end is None
        threading.Timer(1.5, llm.release).start()
        evs = list(llm.generate([{"role": "user", "content": "hi"}]))
        assert [e["event"] for e in evs][:2] == ["started", "waiting"]
        assert s.held and s.alive()
        with llm.room:
            assert llm.holding()  # a new job's claim waits for this lease
        assert s.end is not None and s.end > time.time()


def test_stop_ends_a_turn_that_waits_for_memory(client):
    """A turn stuck behind a media job ends at Stop; nothing runs, and the answer is saved as stopped."""
    assert llm.claim(35.0)
    cid = agent_chat(client)
    t, _ = start(client, cid, script([call("list_workstation_tools")], "RESULT"))
    assert wait_for(
        lambda: (
            chat.live.get(cid) is not None
            and any(e["event"] == "waiting" for e in chat.live[cid].events)
        )
    )
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    assert not t.is_alive()
    last = saved(client, cid)["messages"][-1]
    assert last["finish"] == "cancelled"
    assert not any(m["role"] == "tool" for m in saved(client, cid)["messages"])


def test_an_answer_that_comes_just_after_the_timeout_counts(client, net, monkeypatch):
    """approve() answers in the moment between the approval's timeout and the turn's lock: the page got HTTP 200, so
    the call runs with that answer (the server and the page agree)."""
    results: list[Any] = []

    class LateEvent(threading.Event):
        def wait(
            self, timeout=None
        ):  # the user's click arrives just as the wait times out
            results.append(approve(client, cid, chat.live[cid].approval.id, "once"))
            return False

    class LateApproval(chat.Approval):
        def __init__(self, call_id, chat_scope):
            super().__init__(call_id, chat_scope)
            self.answered = LateEvent()

    monkeypatch.setattr(chat, "Approval", LateApproval)
    cid = agent_chat(client)
    evs = send(client, cid, script([call("web_search", query="late")], "RESULT"))
    assert [r.status_code for r in results] == [200]
    assert next(e for e in evs if e["event"] == "approval_done")["decision"] == "once"
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok" and res["approval"] == "once"
    assert len(net.requests) == 1
    # an answer after the turn took the lock finds nothing to approve
    ask = next(e for e in evs if e["event"] == "approval")
    assert approve(client, cid, ask["id"], "once").status_code == 409


def test_shutdown_ends_running_turns_and_saves_them(client, state):
    """Closing the app stops a turn that waits for approval, as Stop does: the call is cancelled, the turn ends and the partial
    answer is saved, before the shutdown returns."""
    cid = agent_chat(client)
    t, _ = start(client, cid, script([call("web_search", query="x")], "SLOW"))
    ask = pending(cid)
    started = time.monotonic()
    server.shutdown()
    assert time.monotonic() - started < 6
    assert cid not in chat.live
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    tool = msgs[2]
    assert tool["tool_call_id"] == ask["id"] and tool["status"] == "cancelled"
    assert msgs[-1]["finish"] in ("cancelled", "error")
    t.join(5)


# Failures around an answer: it always ends, and the page hears why


def test_a_failed_final_save_still_ends_the_answer(client, monkeypatch):
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    real = chat.save
    writes = []

    def full_disk(c):
        writes.append(1)
        if len(writes) >= 2:  # 1: the user message; 2: the answer
            raise OSError(28, "No space left on device")
        real(c)

    monkeypatch.setattr(chat, "save", full_disk)
    evs = events(client.post(f"/api/chats/{cid}/messages", data={"text": "hi"}))
    assert evs[-1]["event"] == "error" and "could not be saved" in evs[-1]["message"]
    assert cid not in chat.live
    monkeypatch.setattr(
        chat, "save", real
    )  # the disk has room again: the chat takes a new message
    assert (
        events(client.post(f"/api/chats/{cid}/messages", data={"text": "again"}))[-1][
            "event"
        ]
        == "done"
    )


def test_a_python_that_cannot_run_gives_an_error(client, monkeypatch, tmp_path):
    bad = tmp_path / "python"
    bad.write_text("not a program")  # it exists, but without the exec bit
    monkeypatch.setattr(
        llm, "PATHS", SimpleNamespace(qwen_python=bad, qwen_model=Path("ok"))
    )
    llm.stop("fresh")
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    evs = events(client.post(f"/api/chats/{cid}/messages", data={"text": "hi"}))
    assert evs[-1]["event"] == "error" and "could not start" in evs[-1]["message"]
    assert saved(client, cid)["messages"][-1]["finish"] == "error"


def test_stop_while_the_model_loads_ends_the_answer_at_once(
    client, monkeypatch, tmp_path
):
    slow = tmp_path / "slow_worker.py"
    slow.write_text(
        f"import runpy, time\ntime.sleep(4)\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
    )
    monkeypatch.setattr(llm, "WORKER", [str(slow)])
    llm.stop("fresh")
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    t, _ = start(client, cid, "hello")
    assert wait_for(lambda: chat.live.get(cid) and chat.live[cid].rid)
    t0 = time.monotonic()
    client.post(f"/api/chats/{cid}/stop")
    t.join(10)
    assert time.monotonic() - t0 < 2
    assert saved(client, cid)["messages"][-1]["finish"] == "cancelled"


# ---------- media jobs in the chat ----------


def test_a_queued_job_reaches_the_page_and_the_saved_chat(client, ws, monkeypatch):
    """create_job's result carries the job id (the page's job card); wait_for_job gets the answer's deadline, so it
    leaves time for the next steps."""
    monkeypatch.setattr(chat, "TURN_SECONDS", 100.0)
    cid = agent_chat(client)
    steps = script(
        [call("create_job", tool="image.klein", fields={"prompt": "a fox"})],
        [call("wait_for_job", job_id="20261103-130000-0001")],
        "RESULT",
    )
    t, box = start(client, cid, steps)
    approve(client, cid, pending(cid)["id"], "once")
    t.join(5)
    results = [e for e in events(box["r"]) if e["event"] == "tool_result"]
    assert results[0]["job"] == "20261103-130000-0001"
    assert "job" not in results[1]  # a wait shows no second card
    assert "status: queued" in results[1]["content"]
    ((job_id, seconds),) = ws.waits
    assert job_id == "20261103-130000-0001" and 30 < seconds <= 40
    tool = next(m for m in saved(client, cid)["messages"] if m["role"] == "tool")
    assert tool["job"] == "20261103-130000-0001"
    assert ws.jobs[job_id]["chat"] == cid
