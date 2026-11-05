"""Thinking levels (app/effort.py): the level table, the form fields of the message endpoint, the per-step budgets of
an agent answer and the worker's use of mlx-vlm's thinking budget. The fake LLM worker echoes what each request
carried ("seen" in the done stats); no test runs a model."""

import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import chat, config, effort, llm, llm_worker, models
from app.agent import Limits, Settings, build_registry
from tests.conftest import app_client
from tests.fake_workstation import FakeWorkstation

FAKE = Path(__file__).with_name("fake_llm_worker.py")
TOKENS_PER_SECOND = 17  # measured generation speed of Qwen3.8 27B 4-bit on an M5 Pro


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
    reg = build_registry(FakeWorkstation(), Settings(marginalia=False), limits=Limits())
    monkeypatch.setattr(chat, "registry", lambda: reg)
    yield
    llm.close("test over")
    llm.release()


@pytest.fixture
def client():
    return app_client()


def events(response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def new_chat(client, mode="general") -> str:
    return client.post("/api/chats", json={"mode": mode}).json()["id"]


def send(client, cid, **data) -> list[dict[str, Any]]:
    return events(client.post(f"/api/chats/{cid}/messages", data=data))


def seen(evs) -> dict[str, Any]:
    return evs[-1]["stats"]["seen"]


# ---------- the level table ----------


@pytest.mark.parametrize(
    ("form", "level"),
    [
        ({"effort": "medium"}, "medium"),
        ({"effort": " MAX "}, "max"),
        ({"effort": "off", "thinking": "true"}, "off"),  # effort wins
        ({"effort": "low", "thinking": "false"}, "low"),
        ({"thinking": "true"}, effort.THINKING_ON),
        ({"thinking": "TRUE"}, effort.THINKING_ON),
        ({"thinking": "false"}, "off"),
        ({"thinking": "yes"}, "off"),  # any other value was off before levels existed
        ({"effort": "", "thinking": "true"}, effort.THINKING_ON),
        ({}, None),
        ({"effort": "", "thinking": ""}, None),
    ],
)
def test_parse(form, level):
    assert effort.parse(form.get("effort"), form.get("thinking")) == level


@pytest.mark.parametrize("value", ["on", "true", "x-high", "maximum", "1"])
def test_parse_refuses_an_unknown_level(value):
    with pytest.raises(ValueError, match="Unknown thinking level"):
        effort.parse(value, "true")


def test_the_level_table():
    table = {level: effort.options(level) for level in effort.LEVELS}
    assert table["off"] == {
        "thinking": False,
        "reasoning_effort": None,
        "thinking_budget": None,
        "max_tokens": effort.ANSWER_TOKENS,
    }
    budgets = {
        k: (v["reasoning_effort"], v["thinking_budget"]) for k, v in table.items()
    }
    assert budgets == {
        "off": (None, None),
        "low": ("low", 1024),
        "medium": ("medium", 3072),
        "high": ("xhigh", 6144),
        "xhigh": ("xhigh", 8192),
        "max": ("xhigh", None),
    }
    for level in ("low", "medium", "high", "xhigh"):
        # a thought cut off at the budget still leaves the answer its room
        assert (
            table[level]["max_tokens"]
            == table[level]["thinking_budget"] + effort.ANSWER_TOKENS
        )
    # the largest budget ends well inside the 10-minute limit of an answer
    assert effort.PLANS["xhigh"][1] < chat.TURN_SECONDS * TOKENS_PER_SECOND
    assert effort.MAX_RESERVE == table["xhigh"]["max_tokens"]


def test_max_takes_the_room_the_context_has_left():
    assert effort.options("max", room=20000)["max_tokens"] == 20000
    assert effort.options("max", room=100)["max_tokens"] == effort.MAX_RESERVE
    assert effort.options("max", step=3, room=20000)["thinking_budget"] is None
    assert effort.reserve("max") == effort.MAX_RESERVE
    assert effort.reserve("low") == 1024 + effort.ANSWER_TOKENS


def test_later_agent_steps_get_part_of_the_budget():
    for level in ("low", "medium", "high", "xhigh"):
        first, later = effort.options(level, 1), effort.options(level, 2)
        assert later["thinking_budget"] == int(
            first["thinking_budget"] * effort.LATER_STEPS
        )
        assert later["max_tokens"] == later["thinking_budget"] + effort.ANSWER_TOKENS
        # the same template effort: it is in the system prompt, so a change would read the chat again
        assert later["reasoning_effort"] == first["reasoning_effort"]
        assert effort.options(level, 5) == later


@pytest.mark.parametrize("level", effort.LEVELS)
@pytest.mark.parametrize("step", [1, 2, 8])
def test_the_template_gets_only_an_effort_it_knows(level, step):
    o = effort.options(level, step, room=30000)
    assert o["reasoning_effort"] in (None, *llm.EFFORTS)
    assert (o["reasoning_effort"] is None) == (not o["thinking"])
    assert o["thinking_budget"] is None or o["thinking_budget"] > 0


def test_generate_checks_the_budget():
    for bad in (0, -5, 1.5, "100", True):
        with pytest.raises(ValueError, match="thinking_budget"):
            next(llm.generate([{"role": "user", "content": "hi"}], thinking_budget=bad))
    with pytest.raises(ValueError, match="reasoning_effort"):
        next(llm.generate([{"role": "user", "content": "hi"}], reasoning_effort="high"))


# ---------- the message endpoint ----------


@pytest.mark.parametrize("level", effort.LEVELS)
def test_each_level_reaches_the_worker(client, level):
    cid = new_chat(client)
    got = seen(send(client, cid, text="hi", effort=level))
    want = effort.options(level)
    if level == "max":  # its max_tokens depends on the prompt: the next test
        got, want = {**got, "max_tokens": 0}, {**want, "max_tokens": 0}
    assert got == want
    assert got["reasoning_effort"] in (None, *llm.EFFORTS)
    assert client.get(f"/api/chats/{cid}").json()["effort"] == level


def test_max_gives_the_answer_most_of_the_context(client):
    got = seen(send(client, new_chat(client), text="hi", effort="max"))
    assert got["thinking_budget"] is None
    assert chat.CONTEXT_TOKENS - 1000 < got["max_tokens"] < chat.CONTEXT_TOKENS


def test_the_older_thinking_field_still_works(client):
    cid = new_chat(client)
    on = send(client, cid, text="one", thinking="true")
    assert seen(on)["thinking"] is True
    assert (
        seen(on)["thinking_budget"]
        == effort.options(effort.THINKING_ON)["thinking_budget"]
    )
    assert on[-1]["reasoning"] == "thinking it over"
    off = send(client, cid, text="two", thinking="false")
    assert seen(off)["thinking"] is False and off[-1]["reasoning"] == ""
    assert seen(off)["max_tokens"] == effort.ANSWER_TOKENS
    assert client.get(f"/api/chats/{cid}").json()["effort"] == "off"


def test_an_unknown_level_is_refused(client):
    cid = new_chat(client)
    r = client.post(f"/api/chats/{cid}/messages", data={"text": "hi", "effort": "huge"})
    assert r.status_code == 400 and "Unknown thinking level" in r.json()["detail"]
    saved = client.get(f"/api/chats/{cid}").json()
    assert saved["messages"] == [] and "effort" not in saved


def test_regenerate_and_the_next_message_keep_the_level(client):
    cid = new_chat(client)
    send(client, cid, text="one", effort="low")
    again = send(client, cid, regenerate="true")
    assert seen(again)["reasoning_effort"] == "low"
    assert seen(send(client, cid, text="two"))["thinking_budget"] == 1024
    assert (
        seen(send(client, cid, regenerate="true", effort="medium"))["reasoning_effort"]
        == "medium"
    )
    assert client.get(f"/api/chats/{cid}").json()["effort"] == "medium"


def test_a_new_chat_uses_the_settings_default(client, monkeypatch):
    monkeypatch.setattr(config, "APP", replace(config.APP, chat_effort="high"))
    assert client.get("/api/llm").json()["default_effort"] == "high"
    cid = new_chat(client)
    assert seen(send(client, cid, text="hi"))["thinking_budget"] == 6144
    # a level saved by hand that the app does not know falls back to the default too
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "effort": "huge"}))
    assert seen(send(client, cid, text="again"))["thinking_budget"] == 6144


# ---------- agent mode: a budget per step ----------


def script(*steps) -> str:
    return "STEPS:" + json.dumps(list(steps))


def test_an_agent_answer_has_a_budget_per_step(client):
    cid = new_chat(client, "agent")
    text = script(
        [{"name": "list_workstation_tools", "arguments": {}}],
        [{"name": "job_status", "arguments": {"job_id": "20261103-120000-abcd"}}],
        "RESULT",
    )
    evs = send(client, cid, text=text, effort="high")
    assert evs[-1]["event"] == "done" and evs[-1]["stats"]["steps"] == 3
    msgs = client.get(f"/api/chats/{cid}").json()["messages"]
    steps = [m["stats"]["seen"] for m in msgs[:-1] if m["role"] == "assistant"]
    steps.append(evs[-1]["stats"]["seen"])
    assert [s["thinking_budget"] for s in steps] == [6144, 2457, 2457]
    assert [s["max_tokens"] for s in steps] == [10240, 6553, 6553]
    assert {s["reasoning_effort"] for s in steps} == {"xhigh"}
    assert all(s["thinking"] for s in steps)


def test_an_off_agent_answer_that_can_queue_jobs_thinks_briefly(client):
    cid = new_chat(client, "agent")
    text = script([{"name": "list_workstation_tools", "arguments": {}}], "RESULT")
    evs = send(client, cid, text=text)
    msgs = client.get(f"/api/chats/{cid}").json()["messages"]
    steps = [m["stats"]["seen"] for m in msgs[:-1] if m["role"] == "assistant"]
    steps.append(evs[-1]["stats"]["seen"])
    assert [s["thinking_budget"] for s in steps] == [256, 102]
    assert [s["max_tokens"] for s in steps] == [256 + 4096, 102 + 4096]
    # the same template effort in every step: a change would read the chat again
    assert {s["reasoning_effort"] for s in steps} == {"low"}
    assert all(s["thinking"] for s in steps)


def test_the_agent_plan():
    assert effort.agent_level("off", ["create_job", "job_status"]) == effort.QUICK
    assert effort.agent_level("off", ["read_file", "web_search"]) == "off"
    assert effort.agent_level("high", ["create_job"]) == "high"
    assert effort.QUICK not in effort.LEVELS  # the user cannot pick it
    o = effort.options("off")
    assert (o["thinking"], o["reasoning_effort"], o["thinking_budget"]) == (
        False,
        None,
        None,
    )


def test_an_agent_answer_at_max_has_no_budget(client):
    cid = new_chat(client, "agent")
    text = script([{"name": "list_workstation_tools", "arguments": {}}], "RESULT")
    evs = send(client, cid, text=text, effort="max")
    first = client.get(f"/api/chats/{cid}").json()["messages"][1]["stats"]["seen"]
    last = evs[-1]["stats"]["seen"]
    assert first["thinking_budget"] is None and last["thinking_budget"] is None
    # the second step's prompt holds the first step and its result: less room is left
    assert effort.MAX_RESERVE <= last["max_tokens"] < first["max_tokens"]


# ---------- the worker: mlx-vlm's thinking budget ----------


class FakeMx:
    def reset_peak_memory(self) -> None: ...

    def clear_cache(self) -> None: ...


def fake_engine(tokens: list[int], record: dict[str, Any]) -> llm_worker.Engine:
    """An Engine without MLX: the template and stream_generate record their arguments."""

    def template(processor, cfg, messages, **kw):
        record["template"] = kw
        return "PROMPT"

    def stream(model, processor, prompt, **kw):
        record["stream"] = kw
        for n, tok in enumerate(tokens, 1):
            yield SimpleNamespace(token=tok, text=f"<{tok}>", finish_reason=None, prompt_tokens=5,
                                  generation_tokens=n, cached_tokens=0, prompt_tps=1.0, generation_tps=17.0,
                                  peak_memory=1.0)  # fmt: skip

    eng = llm_worker.Engine.__new__(llm_worker.Engine)
    eng.mx, eng.template, eng.stream = FakeMx(), template, stream
    eng.model, eng.processor, eng.specials = SimpleNamespace(config=None), None, []
    eng.vocab, eng.no_emoji = {}, None  # no token bans: the requests set allow_emoji
    eng.draft, eng.spec = None, None  # no DFlash2 draft
    return eng


def run_engine(monkeypatch, req: dict[str, Any], tokens: list[int]):
    out = io.StringIO()
    monkeypatch.setattr(llm_worker, "proto", out)
    record: dict[str, Any] = {}
    base = {
        "id": "r1",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "allow_emoji": True,
    }
    fake_engine(tokens, record).generate({**base, **req}, llm_worker.threading.Event())
    done = [json.loads(line) for line in out.getvalue().splitlines()][-1]
    return record, done


def test_the_worker_passes_the_budget_to_mlx_vlm(monkeypatch):
    end = llm_worker.THINK_END
    req = {"thinking": True, "reasoning_effort": "low", "thinking_budget": 2}
    record, done = run_engine(monkeypatch, req, [1, 2, 198, end, 7])
    assert record["template"]["enable_thinking"] is True
    assert record["template"]["reasoning_effort"] == "low"
    assert record["stream"]["thinking_budget"] == 2
    assert (
        record["stream"]["enable_thinking"] is True
    )  # mlx-vlm forces the end only with this
    assert done["reasoning"] == "<1><2><198>" and done["content"] == "<7>"
    assert done["stats"]["thinking_tokens"] == 3


def test_the_worker_without_a_budget(monkeypatch):
    record, done = run_engine(
        monkeypatch, {"thinking": True}, [1, llm_worker.THINK_END, 7]
    )
    assert (
        "thinking_budget" not in record["stream"]
        and "enable_thinking" not in record["stream"]
    )
    assert (
        "reasoning_effort" not in record["template"]
    )  # the template's default (xhigh)
    assert done["stats"]["thinking_tokens"] == 1
    # thinking off: no budget even when one is sent, and no thinking count
    record, done = run_engine(
        monkeypatch, {"thinking": False, "thinking_budget": 50}, [7]
    )
    assert "thinking_budget" not in record["stream"]
    assert record["template"]["enable_thinking"] is False
    assert "thinking_tokens" not in done["stats"] and done["content"] == "<7>"
