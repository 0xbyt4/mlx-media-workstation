"""The local LLM manager, prompt help and chat, against a fake worker process (no model, no network)."""

import io
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import assist, chat, llm, models, server, tools

FAKE = Path(__file__).with_name("fake_llm_worker.py")
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def fake_llm(tmp_path, monkeypatch):
    def use(mode="ok"):
        monkeypatch.setattr(
            llm,
            "PATHS",
            SimpleNamespace(qwen_python=Path(sys.executable), qwen_model=Path(mode)),
        )

    use()
    monkeypatch.setattr(llm, "WORKER", [str(FAKE)])
    monkeypatch.setattr(llm, "installed", lambda: True)
    monkeypatch.setattr(llm, "LOG", tmp_path / "llm.log")
    monkeypatch.setattr(llm, "media_peak", lambda: 0.0)
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(chat, "CHATS", tmp_path / "chats")
    yield use
    llm.stop("test over")


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:7870")


def user(text):
    return [{"role": "user", "content": text}]


def events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------- llm ----------


def test_generate_streams_started_deltas_and_done():
    evs = list(llm.generate(user("hi")))
    assert evs[0]["event"] == "started"
    assert [e["text"] for e in evs if e["event"] == "content"] == ["Hello", " world"]
    assert evs[-1]["event"] == "done" and evs[-1]["content"] == "Hello world"
    assert llm.status()["state"] == "ready"


def test_thinking_streams_reasoning_first():
    evs = list(llm.generate(user("hi"), thinking=True))
    kinds = [e["event"] for e in evs]
    assert kinds.index("reasoning") < kinds.index("content")
    assert evs[-1]["reasoning"] == "thinking it over"


def test_load_error_is_shown(fake_llm):
    fake_llm("fail")
    with pytest.raises(llm.LLMError, match="Could not load the model: broken"):
        llm.complete(user("hi"))
    assert llm.status()["state"] == "off"


def test_crash_fails_the_request(fake_llm):
    fake_llm("crash")
    with pytest.raises(llm.LLMError):
        llm.complete(user("hi"))


def test_cancel_stops_between_tokens():
    gen = llm.generate(user("SLOW"))
    rid = next(gen)["id"]
    assert next(gen)["event"] == "content"
    assert llm.cancel(rid)
    rest = list(gen)
    assert rest[-1]["event"] == "done" and rest[-1]["finish"] == "cancelled"
    assert not llm.cancel(rid)  # finished: nothing to cancel


def test_closing_the_iterator_cancels_and_the_worker_goes_on():
    gen = llm.generate(user("SLOW"))
    next(gen)
    next(gen)
    gen.close()
    assert llm.complete(user("hi"))["content"] == "Hello world"


def test_stop_fails_waiting_requests_with_the_reason():
    gen = llm.generate(user("SLOW"))
    next(gen)
    threading.Timer(0.1, llm.stop, args=("Unloaded for a test.",)).start()
    last = list(gen)[-1]
    assert last == {"event": "error", "message": "Unloaded for a test."}
    assert llm.status()["state"] == "off"


def test_make_room_unloads_only_for_a_job_that_does_not_fit():
    llm.complete(user("hi"))
    llm.make_room(14.0)  # 18 + 14 + 8 reserve <= 48
    assert llm.status()["state"] == "ready"
    llm.make_room(35.0)
    assert llm.status()["state"] == "off"


def test_refused_next_to_a_big_job(monkeypatch):
    monkeypatch.setattr(llm, "media_peak", lambda: 35.0)
    with pytest.raises(llm.LLMError, match="media job is using the memory"):
        llm.complete(user("hi"))


def test_server_reports_the_running_jobs_peak(monkeypatch):
    monkeypatch.setattr(
        server,
        "jobs",
        {
            "a": {"tool": "video.t2v", "status": "running"},
            "b": {"tool": "image.klein", "status": "queued"},
        },
    )
    assert server.running_peak() == models.tool_peak("video.t2v") == 35.0
    assert models.tool_peak("video.mix") == 0.0


def test_not_installed_message(monkeypatch):
    monkeypatch.setattr(llm, "installed", lambda: False)
    with pytest.raises(llm.LLMError, match="Install it on the Models screen"):
        llm.complete(user("hi"))


# ---------- prompt help ----------


def test_enhance_returns_the_local_answer():
    tool = tools.BY_ID["video.t2v"]
    out = assist.enhance(
        tool, "prompt", {"prompt": "a gull on a pier", "size": "704x1280"}, []
    )
    assert out["text"].startswith("improved:") and out["notes"] == "added detail"
    assert out["model"] == llm.MODEL.label and out["cost_usd"] is None


def test_enhance_needs_a_draft():
    with pytest.raises(assist.AssistError, match="short idea"):
        assist.enhance(tools.BY_ID["video.t2v"], "prompt", {"prompt": "  "}, [])


def test_bad_json_is_a_clear_error():
    with pytest.raises(assist.AssistError, match="expected form"):
        assist.parse("not json")


def test_assist_endpoint(client):
    assert client.get("/api/assist").json()["available"] is True
    r = client.post(
        "/api/assist/enhance",
        data={"tool": "video.t2v", "field": "prompt", "prompt": "a gull"},
    )
    assert r.status_code == 200 and r.json()["text"].startswith("improved:")
    r = client.post("/api/assist/enhance", data={"tool": "video.t2v", "field": "size"})
    assert r.status_code == 400


# ---------- chat ----------


def new_chat(client):
    return client.post("/api/chats").json()["id"]


def test_chat_round_trip(client):
    cid = new_chat(client)
    r = client.post(
        f"/api/chats/{cid}/messages", data={"text": "Hello there\nsecond line"}
    )
    evs = events(r)
    assert evs[0]["event"] == "content" and evs[-1]["event"] == "done"
    c = client.get(f"/api/chats/{cid}").json()
    assert c["title"] == "Hello there" and not c["running"]
    assert [m["role"] for m in c["messages"]] == ["user", "assistant"]
    assert c["messages"][1]["content"] == "Hello world"
    assert client.get("/api/chats").json()[0]["messages"] == 2


def test_history_goes_to_the_model(client):
    cid = new_chat(client)
    client.post(f"/api/chats/{cid}/messages", data={"text": "one"})
    r = client.post(
        f"/api/chats/{cid}/messages", data={"text": "two", "thinking": "true"}
    )
    done = events(r)[-1]
    assert done["stats"]["turns"] == 4  # system, user, assistant, user
    assert done["reasoning"] == "thinking it over"


def test_images_are_stored_sent_and_served(client):
    cid = new_chat(client)
    r = client.post(
        f"/api/chats/{cid}/messages",
        data={"text": "what is this"},
        files=[("images", ("a.png", io.BytesIO(PNG), "image/png"))],
    )
    assert events(r)[-1]["stats"]["images"] == 1
    msg = client.get(f"/api/chats/{cid}").json()["messages"][0]
    assert msg["images"] == ["img_0.png"]
    assert client.get(f"/api/chats/{cid}/files/img_0.png").content == PNG
    assert client.get(f"/api/chats/{cid}/files/chat.json").status_code == 404
    assert client.get(f"/api/chats/{cid}/files/..%2Fchat.json").status_code == 404


def test_message_checks(client):
    cid = new_chat(client)
    assert (
        client.post(f"/api/chats/{cid}/messages", data={"text": " "}).status_code == 400
    )
    bad = [("images", ("a.gif", io.BytesIO(b"GIF8"), "image/gif"))]
    assert (
        client.post(
            f"/api/chats/{cid}/messages", data={"text": "x"}, files=bad
        ).status_code
        == 400
    )
    many = [("images", (f"{i}.png", io.BytesIO(PNG), "image/png")) for i in range(5)]
    assert (
        client.post(
            f"/api/chats/{cid}/messages", data={"text": "x"}, files=many
        ).status_code
        == 400
    )
    assert (
        client.post("/api/chats/nope/messages", data={"text": "x"}).status_code == 404
    )
    assert (
        client.post(
            f"/api/chats/{cid}/messages", data={"regenerate": "true"}
        ).status_code
        == 400
    )


def test_regenerate_replaces_the_last_answer(client):
    cid = new_chat(client)
    client.post(f"/api/chats/{cid}/messages", data={"text": "one"})
    client.post(f"/api/chats/{cid}/messages", data={"regenerate": "true"})
    roles = [m["role"] for m in client.get(f"/api/chats/{cid}").json()["messages"]]
    assert roles == ["user", "assistant"]


def test_stop_and_delete(client):
    cid = new_chat(client)
    t = threading.Thread(
        target=client.post,
        args=(f"/api/chats/{cid}/messages",),
        kwargs={"data": {"text": "SLOW"}},
    )
    t.start()
    assert wait_for(lambda: client.get(f"/api/chats/{cid}").json()["running"])
    assert client.delete(f"/api/chats/{cid}").status_code == 409
    assert wait_for(
        lambda: chat.live.get(cid) is not None and chat.live[cid].rid is not None
    )
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    reply = client.get(f"/api/chats/{cid}").json()["messages"][-1]
    assert reply["finish"] == "cancelled" and reply["content"].startswith("tick")
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "idle"}
    assert client.delete(f"/api/chats/{cid}").status_code == 200
    assert client.get(f"/api/chats/{cid}").status_code == 404


def test_error_is_saved_in_the_chat(client, monkeypatch):
    monkeypatch.setattr(llm, "installed", lambda: False)
    cid = new_chat(client)
    evs = events(client.post(f"/api/chats/{cid}/messages", data={"text": "hi"}))
    assert evs[-1]["event"] == "error"
    reply = client.get(f"/api/chats/{cid}").json()["messages"][-1]
    assert reply["finish"] == "error" and "not installed" in reply["error"]


def test_model_input_keeps_the_newest_turns_that_fit(monkeypatch):
    monkeypatch.setattr(chat, "CONTEXT_TOKENS", 2000)
    big = "x" * 1500  # about 500 tokens
    msgs = []
    for i in range(6):
        msgs.append({"role": "user", "content": f"{i} {big}", "images": []})
        msgs.append({"role": "assistant", "content": big, "reasoning": ""})
    msgs.append({"role": "user", "content": "last", "images": ["img_0.png"]})
    msgs.append({"role": "assistant", "content": "", "finish": "cancelled"})
    messages, images = chat.model_input({"id": "c", "messages": msgs}, 256)
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    assert messages[-1]["content"][-1]["text"] == "last"
    assert len(messages) < len(msgs)
    assert images == [str(chat.CHATS / "c" / "img_0.png")]
