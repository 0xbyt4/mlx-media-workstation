"""The chat's prompt platform (app/chat.py, app/agent/deferred.py, app/agent/spill.py): the prompt a chat freezes at
its first turn, the notes for later changes, core and deferred tools with the bridge (tool_search, tool_describe,
tool_call), the providers, the guards for small models and the spill of long results. The fake LLM worker reports a
hash of each request's tools array and system message ("prefix" in its stats), so a test can see that the prompt's
start never changes inside a chat."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app import chat, chat_search, llm_worker
from app.agent import (
    Context,
    Limits,
    Risk,
    Settings,
    Tool,
    build_registry,
    deferred,
    extensions,
    spill,
)
from app.agent.extensions import Section
from app.agent.registry import MAX_RESULT, Registry
from app.agent.web_tools import Net
from tests import test_agent_chat as base
from tests.test_agent_chat import (
    ORIGIN,
    approve,
    call,
    pending,
    saved,
    script,
    send,
    start,
)

# the fixtures of the agent chat tests: the fake worker, the fake tools and a client
fake_llm, reg, online, client = base.fake_llm, base.reg, base.online, base.client
ws, box, shell, net = base.ws, base.box, base.shell, base.net


def agent_chat(client) -> str:
    return client.post("/api/chats", json={"mode": "agent"}).json()["id"]


def steps_of(client, cid) -> list[dict[str, Any]]:
    """The saved assistant messages of every model step."""
    return [m for m in saved(client, cid)["messages"] if m["role"] == "assistant"]


def prefixes(client, cid) -> list[str]:
    return [m["stats"]["prefix"] for m in steps_of(client, cid)]


def edit(cid: str, **values: Any) -> None:
    """Change chat.json as another part of the app (or an older version of it) would."""
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data.update(values)
    path.write_text(json.dumps(data))


def bridged(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A tool_call call that runs the named tool."""
    return {"name": "tool_call", "arguments": {"name": name, "arguments": arguments}}


def result_of(evs, n=1) -> dict[str, Any]:
    return [e for e in evs if e["event"] == "tool_result"][n - 1]


def tool(name: str, text: str = "ok", **kw: Any) -> Tool:
    return Tool(
        name=name,
        description=kw.pop("description", f"The {name} tool."),
        parameters=kw.pop(
            "parameters",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
        risk=kw.pop("risk", Risk.R0),
        run=lambda a, c: text,
        **kw,
    )


class FakeProvider:
    name = "demo"

    def __init__(self, tools, core=(), section=None) -> None:
        self._tools, self._core, self._section = tools, list(core), section
        self.asked = 0

    def tools(self):
        self.asked += 1
        return list(self._tools)

    def core(self, chat):
        return self._core

    def section(self, chat):
        return self._section


real_load = (
    chat.providers.load
)  # the app's own providers, before the fixture below replaces them


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    monkeypatch.setattr(chat.providers, "load", list)
    monkeypatch.setattr(chat, "provided", {})


# ---------- P1: the prompt a chat keeps ----------


def test_the_prefix_stays_byte_equal_across_steps_turns_and_changes(
    client, monkeypatch, tmp_path, ws, box, shell, net
):
    """The tools array and the system text of the first turn go into every later request: after a Tools menu change,
    a folder grant, a new day, a lost network and new [agent] settings (a restart). The model hears of the changes
    in one note at the end of the chat."""
    cid = agent_chat(client)
    send(client, cid, script([call("job_status", job_id="x")], "RESULT"))
    first = saved(client, cid)["prompt"]
    assert first["tools"][:3] == ["update_plan", "list_workstation_tools", "job_status"]
    assert (
        first["date"] and first["online"] is True and "run_python" not in first["tools"]
    )
    client.patch(
        f"/api/chats/{cid}/agent",
        json={"tools": [*first["state"]["tools"], "run_python"]},
        headers=ORIGIN,
    )
    state = saved(client, cid)["agent"]
    edit(
        cid,
        agent={
            **state,
            "tools": [n for n in state["tools"] if n != "web_search"],
            "folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(chat, "today", lambda: "Friday 1 January 2027")
    monkeypatch.setattr(chat, "online_check", lambda: False)
    other = build_registry(
        ws, Settings(python=Path("/opt/env/bin/python"), marginalia=False), net=Net(net.resolve, net.transport),
        sandbox=box, limits=Limits(), shell=shell,
    )  # fmt: skip
    monkeypatch.setattr(chat, "registry", lambda: other)
    send(client, cid, script([call("list_jobs", limit=1)], "RESULT"))
    after = saved(client, cid)
    assert after["prompt"] == first
    assert len(set(prefixes(client, cid))) == 1 and len(prefixes(client, cid)) == 4
    # the note is on the first step of the second turn, and the model got it after the user's message
    steps = steps_of(client, cid)
    note = steps[2]["notice"]
    assert note.startswith("[The app: this chat's settings changed.")
    assert "Now on: run_python. Call them by name" in note
    assert "Now off: web_search." in note and f"the folder {tmp_path}" in note
    assert steps[2]["told"]["folder"] == str(tmp_path) and "notice" not in steps[3]
    sent, _ = chat.model_input(after, 256, after["prompt"]["schemas"])
    texts = [m["content"][0]["text"] for m in sent if m["role"] == "user"]
    assert texts[-1] == note and texts[-2].startswith("STEPS:")
    # a third turn has nothing new to tell
    send(client, cid, "thanks")
    assert "notice" not in steps_of(client, cid)[-1]
    assert len(set(prefixes(client, cid))) == 1


def test_a_plain_chat_keeps_its_date(client, monkeypatch):
    monkeypatch.setattr(chat, "today", lambda: "Tuesday 3 November 2026")
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    send(client, cid, "hello")
    monkeypatch.setattr(chat, "today", lambda: "Wednesday 4 November 2026")
    send(client, cid, "and today?")
    msgs = steps_of(client, cid)
    assert msgs[0]["stats"]["prefix"] == msgs[1]["stats"]["prefix"]
    assert "Tuesday 3 November 2026" in saved(client, cid)["prompt"]["system"]
    assert saved(client, cid)["prompt"]["tools"] == []


def test_a_chat_saved_before_the_frozen_prompt_gets_one_at_its_next_turn(client):
    cid = agent_chat(client)
    send(client, cid, script([call("list_jobs")], "RESULT"))
    edit(cid, prompt=None)  # an older app saved no prompt
    evs = send(client, cid, script([call("job_status", job_id="x")], "RESULT"))
    assert evs[-1]["finish"] == "stop"
    data = saved(client, cid)
    assert chat.frozen(data) and data["messages"][2]["name"] == "list_jobs"


def test_a_tool_added_to_the_app_reaches_the_model_as_a_note(client, monkeypatch, reg):
    """A tool that the app gets after a chat started starts on (agent_of "known"), but the tools array stays."""
    cid = agent_chat(client)
    send(client, cid, "hi")
    state = saved(client, cid)["agent"]
    edit(cid, agent={**state, "known": [n for n in state["known"] if n != "system_status"],
                     "tools": [n for n in state["tools"] if n != "system_status"]})  # fmt: skip
    data = saved(client, cid)
    edit(
        cid,
        prompt={
            **data["prompt"],
            "state": {**data["prompt"]["state"], "tools": data["agent"]["tools"]},
        },
    )
    send(client, cid, "again")
    assert "Now on: system_status." in steps_of(client, cid)[-1]["notice"]
    assert len(set(prefixes(client, cid))) == 1


def test_a_workstation_chat_hears_of_an_installed_media_tool(client, monkeypatch, ws):
    monkeypatch.setattr(chat, "port", ws)
    monkeypatch.setattr(chat, "not_installed", lambda: ["video.t2v"])
    cid = client.post("/api/chats", json={"mode": "workstation"}).json()["id"]
    send(client, cid, "hi")
    assert (
        "not installed on this Mac: video.t2v" in saved(client, cid)["prompt"]["system"]
    )
    monkeypatch.setattr(chat, "not_installed", list)
    send(client, cid, "and now?")
    assert "Now installed: video.t2v." in steps_of(client, cid)[-1]["notice"]
    assert len(set(prefixes(client, cid))) == 1
    # a workstation chat has few tools: all of them are core, no bridge
    assert deferred.SEARCH not in saved(client, cid)["prompt"]["tools"]


# ---------- P2 and P3: core and deferred tools ----------


def test_tool_search_finds_a_deferred_tool_with_its_schema(client):
    cid = agent_chat(client)
    evs = send(
        client, cid, script([call("tool_search", query="cancel a media job")], "RESULT")
    )
    res = result_of(evs)
    assert res["status"] == "ok" and res["content"].startswith("The best matches")
    first = res["content"].split("\n")[1]
    assert first == "1. cancel_job (Media jobs). Call it by name or with tool_call."
    assert '"name": "cancel_job"' in res["content"] and '"job_id"' in res["content"]
    evs = send(client, cid, script([call("tool_search", query="job_status")], "RESULT"))
    assert "1. job_status: already in your tools" in result_of(evs)["content"]
    evs = send(client, cid, script([call("tool_search", query="zzz qqq")], "RESULT"))
    assert (
        "No tool matches" in result_of(evs)["content"]
        and "Media jobs:" in result_of(evs)["content"]
    )


def test_tool_describe_gives_schemas_and_nearest_names(client):
    cid = agent_chat(client)
    evs = send(
        client,
        cid,
        script([call("tool_describe", names="list_jobs, lists_jobs")], "RESULT"),
    )
    text = result_of(evs)["content"]
    assert '"name": "list_jobs"' in text
    assert "There is no tool 'lists_jobs'. The nearest names: list_jobs" in text


def test_tool_call_runs_the_real_tool_under_its_own_rules(client, ws):
    cid = agent_chat(client)
    ws.jobs["j1"] = {
        "id": "j1",
        "status": "running",
        "tool": "image.klein",
        "chat": cid,
    }
    t, box_ = start(
        client,
        cid,
        script([bridged("cancel_job", {"job_id": "j1"})], "RESULT"),
    )
    ask = pending(cid)
    assert ask["name"] == "cancel_job" and ask["arguments"] == {"job_id": "j1"}
    assert approve(client, cid, ask["id"], "deny").status_code == 200
    t.join(5)
    evs = [
        json.loads(line[6:])
        for line in box_["r"].text.splitlines()
        if line.startswith("data: ")
    ]
    tc = next(e for e in evs if e["event"] == "tool_call")
    assert tc["name"] == "cancel_job" and tc["via"] == "tool_call" and tc["risk"] == 2
    assert result_of(evs)["status"] == "denied" and ws.cancelled == []
    msgs = saved(client, cid)["messages"]
    assert (
        msgs[1]["tool_calls"][0]["name"] == "tool_call"
    )  # the model's own call stays as it wrote it
    assert (
        msgs[2]["name"] == "cancel_job"
        and msgs[2]["tool_call_id"] == msgs[1]["tool_calls"][0]["id"]
    )


def test_the_repeat_rule_sees_through_tool_call(client):
    cid = agent_chat(client)
    evs = send(
        client,
        cid,
        script(
            [bridged("list_workstation_tools", {})],
            [call("list_workstation_tools")],
            "RESULT",
        ),
    )
    assert [r["status"] for r in evs if r["event"] == "tool_result"] == ["ok", "repeat"]


def test_a_deferred_tool_called_by_name_gets_typed_arguments(client, ws):
    """The worker types arguments by the schemas of the tools array; list_jobs is deferred, so its limit comes as the
    string "1" and the loop turns it into a number."""
    cid = agent_chat(client)
    ws.jobs["a"] = {"id": "a", "status": "done", "tool": "image.klein"}
    ws.jobs["b"] = {"id": "b", "status": "done", "tool": "image.klein"}
    evs = send(client, cid, script([call("list_jobs", limit=1)], "RESULT"))
    res = result_of(evs)
    assert res["status"] == "ok"
    assert saved(client, cid)["messages"][1]["tool_calls"][0]["arguments"] == {
        "limit": "1"
    }


def test_an_unknown_tool_gets_the_nearest_names(client):
    cid = agent_chat(client)
    evs = send(client, cid, script([call("cancel_jobs", job_id="x")], "RESULT"))
    text = result_of(evs)["content"]
    assert text.startswith(
        "There is no tool 'cancel_jobs'. The nearest names: cancel_job"
    )
    assert "Your tools: update_plan," in text and "tool_search finds more tools" in text
    evs = send(
        client,
        cid,
        script([bridged("tool_search", {"query": "x"})], "RESULT"),
    )
    assert (
        result_of(evs)["content"] == "Call tool_search directly, not through tool_call."
    )


def test_an_offline_chat_defers_the_web_tools(client, monkeypatch):
    monkeypatch.setattr(chat, "online_check", lambda: False)
    cid = agent_chat(client)
    evs = send(
        client, cid, script([call("tool_search", query="search the web")], "RESULT")
    )
    p = saved(client, cid)["prompt"]
    assert p["online"] is False
    assert "web_search" not in p["tools"] and "web_search" in p["deferred"]
    assert "This Mac had no network when this chat started." in p["system"]
    assert "- Web (need the network): web_search, fetch_url" in p["system"]
    assert deferred.OFFLINE.strip() in result_of(evs)["content"]
    # the check runs only at the first turn
    monkeypatch.setattr(chat, "online_check", lambda: pytest.fail("checked again"))
    send(client, cid, "again")


def test_the_tools_menu_says_where_each_tool_is_loaded(client):
    cid = agent_chat(client)
    view = client.get(f"/api/chats/{cid}/agent").json()
    by = {t["name"]: t for t in view["tools"]}
    assert (
        by["web_search"]["loaded"] == "core" and by["list_jobs"]["loaded"] == "search"
    )
    assert by["read_file"]["loaded"] == "search"  # no folder: it would be deferred
    assert "tool_search" not in by and "read_result" not in by
    assert by["list_jobs"]["group"] == "Media jobs"
    assert view["prompt"] == {
        "frozen": False,
        "date": None,
        "online": None,
        "tokens": None,
        "problems": [],
    }
    send(client, cid, "hi")
    view = client.get(f"/api/chats/{cid}/agent").json()
    assert view["prompt"]["frozen"] is True and view["prompt"]["tokens"]["system"] > 0
    bad = client.patch(
        f"/api/chats/{cid}/agent", json={"tools": ["tool_search"]}, headers=ORIGIN
    )
    assert bad.status_code == 400


# ---------- providers ----------


def test_the_app_providers_reach_an_agent_chat_but_host_tools_skip_a_workstation_chat(
    client, monkeypatch
):
    monkeypatch.setattr(chat.providers, "load", real_load)
    cid = agent_chat(client)
    send(client, cid, "hi")
    p = saved(client, cid)["prompt"]
    assert p["problems"] == []
    assert "memory" in p["tools"]  # a core tool of the memory provider
    if chat_search.available():  # this Python's SQLite has FTS5
        assert "search_chats" in p["deferred"]
    assert "speak" in p["deferred"] and "tool_search" in p["tools"]
    assert "Memory" in p["tokens"]["sections"] and "\n\nMemory:\n" in p["system"]
    ws_chat = client.post("/api/chats", json={"mode": "workstation"}).json()["id"]
    send(client, ws_chat, "hi")
    w = saved(client, ws_chat)["prompt"]
    assert not {"speak", "spotlight_search", "run_shortcut"} & set(w["providers"])


def test_providers_are_asked_once_and_frozen_per_chat(client, monkeypatch, tmp_path):
    long = tool(
        "demo__long",
        "L" * 30000,
        description="Return the long report of the demo server.",
    )
    ping = tool("demo__ping", "pong", description="Ping the demo server.")
    demo = FakeProvider(
        [long, ping],
        core=["demo__ping"],
        section=Section("Demo", "The demo server answers pings.", 150),
    )
    monkeypatch.setattr(chat.providers, "load", lambda: [demo])
    cid = agent_chat(client)
    evs = send(client, cid, script([call("demo__long")], "RESULT"))
    p = saved(client, cid)["prompt"]
    assert p["providers"] == ["demo__long", "demo__ping"]
    assert "demo__ping" in p["tools"] and "demo__long" in p["deferred"]
    assert p["system"].endswith("\n\nDemo:\nThe demo server answers pings.")
    assert "- demo: demo__long" in p["system"] and p["tokens"]["sections"] == {
        "Demo": 10
    }
    # the long result: its start and the file name for the model, the whole text in the work folder
    res = result_of(evs)
    msg = saved(client, cid)["messages"][2]
    assert (
        len(msg["content"]) <= MAX_RESULT
        and "The whole result is saved in the work folder" in msg["content"]
    )
    path = msg["files"][-1]["path"]
    assert path.startswith("results/") and res["files"][-1]["url"].endswith(path)
    assert (chat.work_dir(chat.CHATS, cid) / path).read_text() == "L" * 30000
    offset = int(msg["content"].rsplit("offset=", 1)[1].rstrip(").]"))
    evs = send(
        client, cid, script([call("read_result", file=path, offset=offset)], "RESULT")
    )
    assert (
        result_of(evs)["status"] == "ok"
        and f"characters {offset:,}-" in result_of(evs)["content"]
    )
    asked = demo.asked
    # another chat sees the provider's tools of its own first turn
    demo._tools = [tool("demo__other")]
    other = agent_chat(client)
    send(client, other, "hi")
    assert saved(client, other)["prompt"]["providers"] == ["demo__other"]
    # the first chat keeps its tools while the app runs; after a restart a tool that is gone is gone
    evs = send(client, cid, script([call("demo__ping")], "RESULT"))
    assert result_of(evs)["content"] == "pong"
    monkeypatch.setattr(chat, "provided", {})
    evs = send(client, cid, script([call("demo__ping")], "RESULT"))
    assert result_of(evs)["status"] == "refused"
    assert (
        demo.asked == asked + 2
    )  # the second chat's first turn, and the first chat after the "restart"
    view = client.get(f"/api/chats/{other}/agent").json()
    assert {t["name"]: t["loaded"] for t in view["tools"]}["demo__other"] == "search"


def test_a_provider_tool_that_clashes_with_an_app_tool_is_left_out(client, monkeypatch):
    monkeypatch.setattr(
        chat.providers, "load", lambda: [FakeProvider([tool("web_search")])]
    )
    cid = agent_chat(client)
    send(client, cid, "hi")
    p = saved(client, cid)["prompt"]
    assert p["providers"] == [] and p["problems"] == [
        "web_search: the app has a tool of this name"
    ]


# ---------- P4: guards for small models ----------


def test_a_malformed_call_gets_one_extra_step(client, monkeypatch):
    monkeypatch.setattr(chat, "MAX_STEPS", 2)
    cid = agent_chat(client)
    bad = {"raw": "<function=job_status>\n<parameter=job_id>\nx\n"}
    evs = send(client, cid, script([bad], [call("job_status", job_id="x")], "RESULT"))
    steps = [(e["n"], e["max"], e["last"]) for e in evs if e["event"] == "step"]
    assert steps == [(1, 2, False), (2, 3, False), (3, 3, True)]
    first = result_of(evs)
    assert first["status"] == "error" and "Invalid tool call" in first["content"]
    assert 'job_status takes: {"type": "object"' in first["content"]
    assert (
        "Invalid tool call" not in result_of(evs, 2)["content"]
    )  # the retry ran the call
    # a second malformed call in the same answer counts as a step
    cid = agent_chat(client)
    evs = send(client, cid, script([bad], [bad], "RESULT"))
    assert [e["max"] for e in evs if e["event"] == "step"] == [2, 3, 3]


def test_invalid_arguments_come_back_with_the_schema(client):
    cid = agent_chat(client)
    evs = send(client, cid, script([call("job_status", wrong="x")], "RESULT"))
    text = result_of(evs)["content"]
    assert text.startswith("Invalid arguments for job_status: wrong: unknown argument")
    assert 'Its parameters: {"type":"object","properties":{"job_id"' in text


CALL = "<tool_call>\n<function=job_status>\n<parameter=job_id>\nj1\n</parameter>\n</function>\n</tool_call>"


def test_a_call_in_the_middle_of_the_thinking_runs_when_the_answer_is_empty(client):
    cid = agent_chat(client)
    evs = send(
        client, cid, script({"think": f"Check it.\n{CALL}\nThen answer."}, "RESULT")
    )
    assert result_of(evs)["name"] == "job_status"
    step = saved(client, cid)["messages"][1]
    assert step["reasoning"] == "Check it.\n\nThen answer." and step["tool_calls"][0][
        "arguments"
    ] == {"job_id": "j1"}


def test_a_call_in_the_thinking_of_a_text_answer_is_a_draft(client):
    cid = agent_chat(client)
    evs = send(
        client, cid, script({"think": f"Maybe {CALL} no.", "text": "No call needed."})
    )
    assert not [e for e in evs if e["event"] == "tool_result"]
    assert evs[-1]["content"].strip() == "No call needed."


def test_a_call_cut_off_in_the_thinking_comes_back_as_malformed(client):
    cid = agent_chat(client)
    cut = CALL[: CALL.index("</parameter>")]
    evs = send(client, cid, script({"think": f"Check it.\n{cut}"}, "RESULT"))
    res = result_of(evs)
    assert res["status"] == "error" and "cut off" in res["content"]


def test_thought_calls():
    rest, blocks = llm_worker.thought_calls(f"a {CALL} b {CALL} c", "")
    assert (
        rest == "a  b  c" and len(blocks) == 1 and blocks[0][1] is True
    )  # the same call once
    assert llm_worker.thought_calls(f"a {CALL}", "text")[1] == [
        (CALL[11:-12], True)
    ]  # trailing: as before
    assert llm_worker.thought_calls(f"a {CALL} b", "text") == (f"a {CALL} b", [])
    assert llm_worker.thought_calls("no call", "") == ("no call", [])


def test_a_looping_answer_stops_and_the_loop_leaves_the_history(client):
    cid = agent_chat(client)
    evs = send(client, cid, script("Intro.\n" + "Same words again. " * 60))
    done = evs[-1]
    assert done["finish"] == "repeat" and done["note"] == chat.NOTE_LOOP
    msg = saved(client, cid)["messages"][-1]
    assert msg["content"] == "Intro.\nSame words again.\n\n" + chat.LOOP_MARK.format(
        times=59
    )


def test_a_looping_thought_stops_the_answer(client):
    cid = agent_chat(client)
    evs = send(
        client,
        cid,
        script({"think": "Let me think. " * 40, "calls": [call("list_jobs")]}),
    )
    assert evs[-1]["finish"] == "repeat"
    assert not [
        e for e in evs if e["event"] == "tool_result"
    ]  # the step's calls do not run
    assert saved(client, cid)["messages"][-1]["reasoning"].startswith(
        "Let me think.\n\n[The app removed 39"
    )


def test_looping():
    assert deferred.looping("ab" * 300) == (2, 300)
    assert deferred.looping("x" * 399) is None  # under LOOP_SPAN
    assert deferred.looping("tick 1 tick 2 tick 3 " * 2) is None
    assert deferred.looping("".join(f"line {n}\n" for n in range(200))) is None
    assert (
        deferred.looping("start " + "A long sentence that repeats. " * 4 + "A long")
        is None
    )  # not at the end
    assert deferred.looping("A long sentence that repeats itself here. " * 10) == (
        42,
        10,
    )


def test_coerce_types_string_arguments_by_the_schema():
    params = {
        "properties": {
            "n": {"type": "integer"},
            "f": {"type": "number"},
            "b": {"type": "boolean"},
            "s": {"type": "string"},
            "o": {"type": ["array", "string"]},
            "x": {"type": "integer"},
        }
    }
    args = {
        "n": "2",
        "f": "1.5",
        "b": "true",
        "s": "3",
        "o": '["a"]',
        "x": "two",
        "extra": "1",
    }
    assert deferred.coerce(params, args) == {
        "n": 2, "f": 1.5, "b": True, "s": "3", "o": '["a"]', "x": "two", "extra": "1",
    }  # fmt: skip


def test_bm25_ranks_by_name_description_and_parameters():
    tools = [
        tool(
            "burn_captions", description="Write subtitles into the picture of a video."
        ),
        tool("cancel_job", description="Cancel a queued or running media job."),
        tool(
            "media_info",
            description="Duration and codecs of a file.",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        ),
    ]
    assert [t.name for _, t in deferred.rank(tools, "subtitles for my video")] == [
        "burn_captions"
    ]
    assert (
        deferred.rank(tools, "cancel jobs")[0][1].name == "cancel_job"
    )  # plural "jobs" finds "job"
    assert deferred.rank(tools, "path")[0][1].name == "media_info"
    assert deferred.rank(tools, "weather") == []


# ---------- P5: result spill ----------


def test_spill_refuses_a_linked_results_folder(tmp_path):
    work, outside = tmp_path / "ws", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (work / spill.FOLDER).symlink_to(outside)
    assert spill.save(work, "call_1", "x" * 20000) is None
    assert list(outside.iterdir()) == []
    ctx = Context("c", None, threading.Event(), work)
    reader = Registry([spill.build()])
    res = reader.call(spill.NAME, {"file": "results/call_1.txt"}, ctx)
    assert res.status == "error" and "No saved result" in res.content


def test_spill_note_fits_the_cap():
    text = "y" * 50000
    out = spill.note(text, "results/call_9.txt")
    assert len(out) <= MAX_RESULT and out.startswith("y" * 11000)
    shown = int(out.rsplit("offset=", 1)[1].rstrip(").]"))
    assert out[:shown] == text[:shown] and "of 50,000 characters" in out


def test_invalid_arguments_of_a_registry_call_name_the_parameters(tmp_path):
    r = Registry(
        [
            tool(
                "t",
                parameters={"type": "object", "properties": {"n": {"type": "integer"}}},
            )
        ]
    )
    res = r.call("t", {"n": "x"}, Context("c", None, threading.Event(), tmp_path))
    assert res.content == 'Invalid arguments for t: n: expected integer. Its parameters: {"type":"object",' \
        '"properties":{"n":{"type":"integer"}}}'  # fmt: skip


def test_collect_sections_keep_the_provider_order():
    a = FakeProvider([tool("a__x")], section=Section("A", "first", 10))
    b = FakeProvider([tool("b__y")], section=Section("B", "second", 10))
    b.name = "b"
    got = extensions.collect([a, b], {})
    assert [s.title for s in got.sections] == ["A", "B"]
