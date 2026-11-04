"""Presets and chat UX on the server (app/chat.py): a new chat from another one's settings, temporary chats, the
"waits for approval" mark of the chat list and arguments that the user edits on the approval card. Same fakes as
tests/test_agent_chat.py: no test runs a model or reaches the network."""

import json
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException

from app import chat
from tests import test_agent_chat as base
from tests.test_agent_chat import (
    ORIGIN,
    agent_chat,
    approve,
    call,
    events,
    pending,
    saved,
    script,
    send,
    start,
)
from tests.test_agent_write_chat import grant

# the fixtures of the agent chat tests: the fake worker, the fake Workstation, network and sandbox, the client
fake_llm = base.fake_llm
reg = base.reg
ws = base.ws
box = base.box
shell = base.shell
net = base.net
client = base.client


def stored(cid: str) -> dict:
    return json.loads((chat.CHATS / cid / "chat.json").read_text())


def answer_with(client, cid, call_id, decision, arguments):
    return client.post(
        f"/api/chats/{cid}/approve",
        json={"call_id": call_id, "decision": decision, "arguments": arguments},
        headers=ORIGIN,
    )


# ---------- temporary chats ----------


def test_a_temporary_chat_is_marked_in_chat_json_and_the_list(client):
    temp = client.post("/api/chats", json={"mode": "general", "temporary": True})
    kept = client.post("/api/chats", json={"mode": "general"}).json()
    assert temp.json()["temporary"] is True and kept["temporary"] is False
    assert stored(temp.json()["id"])["temporary"] is True
    assert "temporary" not in stored(kept["id"])  # a kept chat has no key
    listed = {c["id"]: c["temporary"] for c in client.get("/api/chats").json()}
    assert listed == {temp.json()["id"]: True, kept["id"]: False}


def test_the_temporary_flag_changes_only_while_the_chat_is_empty(client):
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    r = client.patch(f"/api/chats/{cid}", json={"temporary": True})
    assert r.status_code == 200 and r.json()["temporary"] is True
    assert r.json()["mode"] == "general"  # a body without mode keeps it
    r = client.patch(f"/api/chats/{cid}", json={"temporary": False})
    assert r.json()["temporary"] is False and "temporary" not in stored(cid)
    send(client, cid, "hello")
    assert (
        client.patch(f"/api/chats/{cid}", json={"temporary": True}).status_code == 409
    )


def test_the_app_start_deletes_the_temporary_chats(client):
    temp = client.post("/api/chats", json={"temporary": True}).json()["id"]
    kept = client.post("/api/chats", json={}).json()["id"]
    assert chat.drop_temporary() == [temp]
    assert not (chat.CHATS / temp).exists()
    assert (chat.CHATS / kept / "chat.json").exists()
    assert chat.drop_temporary() == []


def test_the_app_start_skips_a_temporary_chat_that_answers(client):
    temp = client.post("/api/chats", json={"temporary": True}).json()["id"]
    chat.live[temp] = chat.Live()
    try:
        assert chat.drop_temporary() == []
    finally:
        chat.live.pop(temp)
    assert (chat.CHATS / temp / "chat.json").exists()


# ---------- a new chat from another one's settings ----------


def test_a_new_chat_gets_the_settings_but_not_the_history(client, monkeypatch):
    monkeypatch.setattr(
        chat, "check_folder", lambda path: path
    )  # no home-folder rules here
    cid = agent_chat(client)
    on = ["list_files", "read_file", "web_search"]
    client.patch(f"/api/chats/{cid}/agent", json={"tools": on}, headers=ORIGIN)
    data = stored(cid)
    data["agent"].update(folder="/Users/you/notes", grants=["web_search"], tainted=True)
    data["effort"] = "low"
    (chat.CHATS / cid / "chat.json").write_text(json.dumps(data))
    send(client, cid, "hello")
    r = client.post(f"/api/chats/{cid}/new", json={}, headers=ORIGIN)
    assert r.status_code == 200
    new = r.json()
    assert new["id"] != cid and new["mode"] == "agent" and new["messages"] == 0
    assert new["note"] == "" and new["temporary"] is False
    after = stored(new["id"])
    assert after["messages"] == [] and after["title"] == "New chat"
    assert after["effort"] == "low"
    assert after["agent"]["tools"] == on
    assert after["agent"]["folder"] == "/Users/you/notes"
    assert after["agent"]["grants"] == [] and after["agent"]["tainted"] is False
    assert len(stored(cid)["messages"]) == 2  # the old chat keeps its messages


def test_a_new_chat_takes_the_tools_and_the_preset_of_the_body(client, monkeypatch):
    monkeypatch.setattr(chat, "check_folder", lambda path: path)
    cid = agent_chat(client)
    data = stored(cid)
    data["agent"]["folder"] = "/Users/you/notes"
    (chat.CHATS / cid / "chat.json").write_text(json.dumps(data))
    body = {"tools": ["web_search", "run_python"]}
    new = client.post(f"/api/chats/{cid}/new", json=body, headers=ORIGIN).json()
    assert stored(new["id"])["agent"]["tools"] == ["web_search", "run_python"]
    # another preset: its default tools, and no folder outside an agent chat
    new = client.post(
        f"/api/chats/{cid}/new", json={"mode": "workstation"}, headers=ORIGIN
    ).json()
    state = stored(new["id"])["agent"]
    assert new["mode"] == "workstation" and state["folder"] is None
    assert "create_job" in state["tools"] and "web_search" not in state["tools"]
    new = client.post(
        f"/api/chats/{cid}/new", json={"mode": "general"}, headers=ORIGIN
    ).json()
    assert new["mode"] == "general" and "agent" not in stored(new["id"])


def test_a_new_chat_carries_the_temporary_flag(client):
    cid = client.post("/api/chats", json={"mode": "general", "temporary": True})
    cid = cid.json()["id"]
    new = client.post(f"/api/chats/{cid}/new", json={}, headers=ORIGIN).json()
    assert new["temporary"] is True
    body = {"temporary": False}
    new = client.post(f"/api/chats/{cid}/new", json=body, headers=ORIGIN).json()
    assert new["temporary"] is False


def test_a_folder_that_cannot_be_granted_now_stays_behind(client, monkeypatch):
    cid = agent_chat(client)
    data = stored(cid)
    data["agent"]["folder"] = "/Users/you/gone"
    (chat.CHATS / cid / "chat.json").write_text(json.dumps(data))

    def refuse(path):
        raise HTTPException(400, f"There is no folder {path}.")

    monkeypatch.setattr(chat, "check_folder", refuse)
    new = client.post(f"/api/chats/{cid}/new", json={}, headers=ORIGIN).json()
    assert stored(new["id"])["agent"]["folder"] is None
    assert (
        new["note"]
        == "The folder was not carried over: There is no folder /Users/you/gone."
    )


def test_a_new_chat_from_another_needs_the_app_s_origin_and_known_values(client):
    cid = agent_chat(client)
    assert client.post(f"/api/chats/{cid}/new", json={}).status_code == 403
    r = client.post(f"/api/chats/{cid}/new", json={"tools": ["nope"]}, headers=ORIGIN)
    assert r.status_code == 400 and "nope" in r.json()["detail"]
    r = client.post(f"/api/chats/{cid}/new", json={"mode": "x"}, headers=ORIGIN)
    assert r.status_code == 400
    r = client.post("/api/chats/20260101-000000-abcd/new", json={}, headers=ORIGIN)
    assert r.status_code == 404
    assert len(client.get("/api/chats").json()) == 1  # nothing was made


def test_a_new_chat_from_another_takes_no_body(client):
    temp = client.post("/api/chats", json={"mode": "general", "temporary": True}).json()
    r = client.post(f"/api/chats/{temp['id']}/new", headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["mode"] == "general" and r.json()["temporary"] is True
    assert r.json()["id"] != temp["id"]


# ---------- the chat list's approval mark ----------


def test_the_chat_list_marks_a_chat_that_waits_for_approval(client):
    cid = agent_chat(client)
    t, _box = start(client, cid, script([call("web_search", query="fox")], "RESULT"))
    ask = pending(cid)
    row = next(c for c in client.get("/api/chats").json() if c["id"] == cid)
    assert row["running"] is True and row["waiting"] is True
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    row = next(c for c in client.get("/api/chats").json() if c["id"] == cid)
    assert row["running"] is False and row["waiting"] is False


# ---------- edited arguments on the approval card ----------


def test_edited_arguments_run_and_are_stored(client, net):
    cid = agent_chat(client)
    t, box = start(client, cid, script([call("web_search", query="fox")], "RESULT"))
    ask = pending(cid)
    assert ask["editable"] is True
    edited = {"query": "red fox habitat"}
    r = answer_with(client, cid, ask["id"], "once", edited)
    assert r.status_code == 200 and r.json()["decision"] == "once"
    t.join(5)
    evs = events(box["r"])
    done = next(e for e in evs if e["event"] == "approval_done")
    assert done["arguments"] == edited and done["edited"] is True
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok" and res["edited"] is True
    assert "red fox habitat" in unquote(net.requests[0].target).replace("+", " ")
    msgs = saved(client, cid)["messages"]
    step = next(m for m in msgs if m.get("tool_calls"))
    assert step["tool_calls"][0]["arguments"] == edited
    assert step["tool_calls"][0]["edited"] is True
    tool = next(m for m in msgs if m["role"] == "tool")
    assert tool["edited"] is True and "red fox habitat" in tool["describe"]


def test_the_model_sees_the_edited_call(client):
    """The next request's history has the arguments that ran, and no "edited" key (the chat template's call shape)."""
    cid = agent_chat(client)
    t, _box = start(client, cid, script([call("web_search", query="fox")], "RESULT"))
    answer_with(client, cid, pending(cid)["id"], "once", {"query": "hare"})
    t.join(5)
    messages, _ = chat.model_input(stored(cid), 1000)
    calls = [c for m in messages for c in m.get("tool_calls") or []]
    assert calls == [
        {"id": calls[0]["id"], "name": "web_search", "arguments": {"query": "hare"}}
    ]


def test_invalid_edited_arguments_are_refused_and_the_call_still_waits(client, net):
    cid = agent_chat(client)
    t, box = start(client, cid, script([call("web_search", query="fox")], "RESULT"))
    ask = pending(cid)
    r = answer_with(client, cid, ask["id"], "once", {"query": 5})
    assert (
        r.status_code == 400
        and r.json()["detail"] == "Invalid arguments: query: expected string"
    )
    r = answer_with(client, cid, ask["id"], "once", {"text": "fox"})
    assert r.status_code == 400 and "unknown argument" in r.json()["detail"]
    r = answer_with(client, cid, ask["id"], "deny", {"query": "hare"})
    assert r.status_code == 400 and "only once" in r.json()["detail"]
    assert chat.live[cid].approval is not None  # still waiting
    approve(client, cid, ask["id"], "once")
    t.join(5)
    evs = events(box["r"])
    assert "edited" not in next(e for e in evs if e["event"] == "approval_done")
    assert "edited" not in next(e for e in evs if e["event"] == "tool_result")
    assert "fox" in net.requests[0].target


def test_a_call_with_a_diff_cannot_be_edited(client, tmp_path: Path):
    folder = tmp_path / "granted"
    folder.mkdir()
    (folder / "todo.md").write_text("buy milk\n")
    cid = agent_chat(client)
    grant(cid, folder)
    args = {"path": "todo.md", "old_text": "buy milk", "new_text": "buy tea"}
    t, _box = start(client, cid, script([call("edit_file", **args)], "RESULT"))
    ask = pending(cid)
    assert ask["editable"] is False and ask["preview"]
    changed = {**args, "new_text": "buy beer"}
    r = answer_with(client, cid, ask["id"], "once", changed)
    assert r.status_code == 400 and "Deny it" in r.json()["detail"]
    approve(client, cid, ask["id"], "deny")
    t.join(5)
    assert (folder / "todo.md").read_text() == "buy milk\n"


def test_edited_arguments_cannot_be_allowed_for_the_chat(client):
    cid = agent_chat(client)
    steps = script(
        [call("create_job", tool="image.klein", fields={"prompt": "a fox"})], "RESULT"
    )
    t, _box = start(client, cid, steps)
    ask = pending(cid)
    assert ask["chat_scope"] is True
    edited = {"tool": "image.klein", "fields": {"prompt": "a hare"}}
    r = answer_with(client, cid, ask["id"], "chat", edited)
    assert r.status_code == 400
    approve(client, cid, ask["id"], "deny")
    t.join(5)
