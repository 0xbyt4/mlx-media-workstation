"""A workstation chat runs the Workstation tools itself (app/chat.py workstation_registry): the fake LLM worker scripts
the model's tool calls (STEPS: in tests/fake_llm_worker.py), the tools are the real ones over a fake Workstation. The
chat has no file, web, shell or Python tool, and the chat profile has no tools at all."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import chat, config, llm, models
from app.agent import Registry, plan_tool, ws_tools
from tests.conftest import app_client
from tests.fake_workstation import FakeWorkstation
from tests.test_agent_chat import (
    ORIGIN,
    approve,
    call,
    events,
    kinds,
    pending,
    saved,
    script,
    send,
    start,
)

FAKE = Path(__file__).with_name("fake_llm_worker.py")
# the tools of a workstation chat: every tool of app/agent/ws_tools.py, so a new Workstation tool joins them
WS_TOOLS = [t.name for t in ws_tools.build(FakeWorkstation())]


def test_the_workstation_tools_are_the_media_side_only():
    assert {
        "list_workstation_tools", "job_status", "search_library", "create_job", "list_jobs", "wait_for_job",
        "cancel_job", "transcribe_audio",
    } <= set(WS_TOOLS)  # fmt: skip
    others = {"update_plan", "list_files", "read_file", "search_files", "write_file", "edit_file",
              "copy_to_workspace", "export_from_workspace", "media_info", "view_image", "web_search", "fetch_url",
              "run_python", "run_shell"}  # fmt: skip
    assert not others & set(WS_TOOLS)


def profile(monkeypatch, name: str) -> None:
    monkeypatch.setattr(
        config, "APP", config.App(**{**config.APP_DEFAULTS, "profile": name})
    )


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
    # an agent registry that a workstation chat must never use
    monkeypatch.setattr(
        chat, "registry", lambda: pytest.fail("the agent registry was used")
    )
    profile(monkeypatch, "full")
    yield
    for run in list(chat.live.values()):  # a failed test must not leave a turn waiting
        run.stop = True
        run.abort.set()
        if run.approval is not None:
            run.approval.answered.set()
    llm.close("test over")
    llm.release()


@pytest.fixture(autouse=True)
def ws(monkeypatch) -> FakeWorkstation:
    port = FakeWorkstation()
    monkeypatch.setattr(chat, "port", port)
    return port


@pytest.fixture
def client():
    return app_client()


def workstation_chat(client) -> str:
    return client.post("/api/chats", json={"mode": "workstation"}).json()["id"]


def test_a_workstation_chat_offers_only_the_workstation_tools(client):
    cid = workstation_chat(client)
    evs = send(client, cid, script([call("list_workstation_tools")], "RESULT"))
    assert kinds(evs) == ["step", "tool_call", "tool_result", "step", "done"]
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok" and "image.klein" in res["content"]
    seen = evs[-1]["stats"]["seen"]
    assert [t["function"]["name"] for t in seen["tools"]] == WS_TOOLS
    system = seen["messages"][0]["content"]
    assert (
        "You can run the app's media tools with these tool calls: "
        + ", ".join(WS_TOOLS)
        in system
    )
    assert "queue it with create_job" in system and "write prompt blocks" in system
    assert "```prompt music.ace lyrics" in system  # the prompt blocks stay
    assert "starts only after your answer ends" in system  # the memory rule
    msgs = saved(client, cid)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.parametrize(
    "name, args",
    [
        ("run_python", {"code": "print(1)"}),
        ("run_shell", {"command": "ls"}),
        ("web_search", {"query": "fox"}),
        ("read_file", {"path": "notes.txt"}),
        ("update_plan", {"items": []}),
    ],
)
def test_a_workstation_chat_has_no_other_agent_tool(client, name, args):
    cid = workstation_chat(client)
    evs = send(client, cid, script([call(name, **args)], "RESULT"))
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "refused"
    assert (
        f"There is no tool {name!r}" in res["content"]
        and "create_job" in res["content"]
    )


def test_create_job_asks_first_and_queues_the_job(client, ws):
    cid = workstation_chat(client)
    steps = script(
        [
            call(
                "create_job", tool="image.klein", fields={"prompt": "a fox", "steps": 6}
            )
        ],
        "RESULT",
    )
    t, box = start(client, cid, steps)
    ask = pending(cid)
    assert ask["name"] == "create_job" and ask["risk"] == 2 and ask["chat_scope"]
    assert ws.created == []  # nothing runs before the answer
    assert approve(client, cid, ask["id"], "chat").status_code == 200
    t.join(5)
    res = next(e for e in events(box["r"]) if e["event"] == "tool_result")
    assert res["status"] == "ok" and res["job"] == "20261103-130000-0001"
    assert ws.jobs[res["job"]]["chat"] == cid
    assert saved(client, cid)["agent"]["grants"] == ["create_job"]
    # allowed in this chat: the next job runs without a card
    evs = send(
        client,
        cid,
        script(
            [call("create_job", tool="image.klein", fields={"prompt": "a cat"})],
            "RESULT",
        ),
    )
    assert "approval" not in kinds(evs) and len(ws.created) == 2


def test_cancel_job_keeps_its_approval(client, ws):
    cid = workstation_chat(client)
    ws.jobs["20261103-120000-abcd"]["chat"] = cid
    t, box = start(
        client,
        cid,
        script([call("cancel_job", job_id="20261103-120000-abcd")], "RESULT"),
    )
    ask = pending(cid)
    assert ask["name"] == "cancel_job" and ask["risk"] == 2
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    res = next(e for e in events(box["r"]) if e["event"] == "tool_result")
    assert res["status"] == "denied" and ws.cancelled == []


def test_the_tools_menu_of_a_workstation_chat(client):
    cid = workstation_chat(client)
    state = client.get(f"/api/chats/{cid}/agent").json()
    assert [t["name"] for t in state["tools"]] == WS_TOOLS
    assert state["enabled"] == WS_TOOLS and state["folder"] is None
    r = client.patch(
        f"/api/chats/{cid}/agent",
        json={"folder": str(Path.home() / "Documents")},
        headers=ORIGIN,
    )
    assert (r.status_code, r.json()["detail"]) == (
        400,
        "Only an agent chat can use a folder.",
    )
    r = client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["run_python"]}, headers=ORIGIN
    )
    assert r.status_code == 400
    on = [n for n in WS_TOOLS if n not in ("create_job", "cancel_job")]
    r = client.patch(f"/api/chats/{cid}/agent", json={"tools": on}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["enabled"] == on
    # an off tool is not offered, and the prompt names it
    evs = send(
        client,
        cid,
        script(
            [call("create_job", tool="image.klein", fields={"prompt": "x"})], "RESULT"
        ),
    )
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "refused" and "off in this chat" in res["content"]
    seen = evs[-1]["stats"]["seen"]
    assert [t["function"]["name"] for t in seen["tools"]] == on
    assert (
        "These tools are off in this chat: create_job, cancel_job."
        in seen["messages"][0]["content"]
    )


def test_a_mode_change_gives_the_tools_of_the_new_mode(client, monkeypatch):
    """A workstation chat's tool list must not become an agent chat's list (the chat is still empty)."""
    agent_reg = Registry(ws_tools.build(FakeWorkstation()) + plan_tool.build())
    monkeypatch.setattr(chat, "registry", lambda: agent_reg)
    cid = workstation_chat(client)
    client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["job_status"]}, headers=ORIGIN
    )
    client.patch(f"/api/chats/{cid}", json={"mode": "agent"}, headers=ORIGIN)
    assert saved(client, cid)["agent"]["tools"] == agent_reg.names()
    client.patch(f"/api/chats/{cid}", json={"mode": "workstation"}, headers=ORIGIN)
    assert saved(client, cid)["agent"]["tools"] == WS_TOOLS


def test_the_chat_profile_has_no_workstation_tools(client, monkeypatch):
    profile(monkeypatch, "chat")
    cid = workstation_chat(client)
    evs = send(client, cid, "hello")
    assert "step" not in kinds(evs) and evs[-1]["event"] == "done"  # the plain answer
    assert client.get(f"/api/chats/{cid}/agent").status_code == 409
    system = chat.system_prompt(saved(client, cid))
    assert "tool calls" not in system and "create_job" not in system
    assert "```prompt music.ace lyrics" in system


def test_a_general_chat_has_no_tools(client):
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    assert "step" not in kinds(send(client, cid, "hello"))
    assert client.get(f"/api/chats/{cid}/agent").status_code == 409


def test_a_workstation_answer_holds_one_memory_lease(client, monkeypatch):
    """All steps run in one llm.session(), as in agent mode: a job that needs the model's memory starts after the
    answer, not between two steps."""
    sessions = []
    real = llm.session

    def counted(*a, **kw):
        sessions.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(llm, "session", counted)
    cid = workstation_chat(client)
    evs = send(
        client,
        cid,
        script(
            [call("list_jobs")],
            [call("job_status", job_id="20261103-120000-abcd")],
            "RESULT",
        ),
    )
    assert evs[-1]["stats"]["steps"] == 3 and len(sessions) == 1


def test_a_saved_workstation_chat_without_tool_state(client):
    """A workstation chat saved before it had tools has no "agent" state: it gets every Workstation tool."""
    cid = workstation_chat(client)
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data.pop("agent", None)
    data.pop("mode", None)  # saved before modes too
    path.write_text(json.dumps(data))
    evs = send(client, cid, "hello")
    assert [
        t["function"]["name"] for t in evs[-1]["stats"]["seen"]["tools"]
    ] == WS_TOOLS


def test_a_tool_added_to_the_app_starts_on_in_saved_chats():
    names = [n for n in WS_TOOLS if n != "system_status"]
    # a chat saved before "known" existed: system_status came later, so it starts on; a tool turned off stays off
    old = {
        "mode": "workstation",
        "agent": {"tools": names[1:], "grants": [], "tainted": False},
    }
    tools = chat.agent_of(old)["tools"]
    assert "system_status" in tools and names[0] not in tools
    # a chat that knew system_status and turned it off keeps it off
    off = {"mode": "workstation", "agent": {"tools": names, "known": WS_TOOLS}}
    assert "system_status" not in chat.agent_of(off)["tools"]
    # a tool unknown to the saved state starts as in a new chat
    newer = {"mode": "workstation", "agent": {"tools": names, "known": names}}
    assert "system_status" in chat.agent_of(newer)["tools"]
    assert set(chat.agent_of(newer)["known"]) == set(WS_TOOLS)


def test_the_file_room_counts_the_tool_schemas_of_a_workstation_chat():
    with_tools = {"mode": "workstation", "messages": []}
    none_on = {
        "mode": "workstation",
        "messages": [],
        "agent": {"tools": [], "known": WS_TOOLS},
    }
    a = chat.file_room(with_tools, "off", "", 0, 1)
    b = chat.file_room(none_on, "off", "", 0, 1)
    assert a < b


def test_the_workstation_prompt_names_the_tools_that_are_not_installed():
    names = ["list_workstation_tools", "create_job"]
    prompt = chat.workstation_prompt(names, [], ["video.t2v", "music.ace"])
    assert "not installed on this Mac: video.t2v, music.ace" in prompt
    assert "do not call list_workstation_tools to learn them" in prompt
    assert "Every media tool below is installed" in chat.workstation_prompt(
        names, [], []
    )
    assert "never promise to tell the user when the job is done" in prompt
    assert (
        "not installed" not in chat.workstation_prompt()
    )  # the chat profile runs no tools


def test_not_installed_follows_the_model_files(monkeypatch, tmp_path):
    there, gone = tmp_path / "model", tmp_path / "missing"
    there.mkdir()
    monkeypatch.setattr(
        chat.tools,
        "REQUIRES",
        {t["id"]: [there] for t in chat.tools.TOOLS} | {"video.t2v": [there, gone]},
    )
    assert chat.not_installed() == ["video.t2v"]
