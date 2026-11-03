"""The local LLM manager, prompt help and chat, against a fake worker process (no model, no network)."""

import io
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import assist, chat, config, llm, models, server, tools

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
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(chat, "CHATS", tmp_path / "chats")
    monkeypatch.setattr(llm, "closed", False)
    yield use
    # cancel first: a request a failed test left waiting must not start a worker once the real paths are back
    llm.close("test over")
    llm.release()


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


class IdleWorker:
    def __init__(self) -> None:
        self.pending: dict[str, object] = {}
        self.stopped: list[str] = []

    def stop(self, reason: str) -> None:
        self.stopped.append(reason)


@pytest.mark.parametrize(
    ("minutes", "idle_s", "stops"),
    [(10, 601, True), (10, 500, False), (5, 301, True), (0, 10**6, False)],
)
def test_idle_unload_follows_the_setting(monkeypatch, minutes, idle_s, stops):
    w = IdleWorker()
    monkeypatch.setattr(
        config,
        "APP",
        config.App(**{**config.APP_DEFAULTS, "llm_idle_minutes": minutes}),
    )
    monkeypatch.setattr(llm, "worker", w)
    monkeypatch.setattr(llm, "last_used", time.time() - idle_s)
    assert llm.stop_if_idle() is stops
    assert w.stopped == (["Unloaded after being idle."] if stops else [])
    assert (llm.worker is None) is stops
    assert llm.idle_stop() == (minutes * 60.0 if minutes else None)


def test_idle_unload_waits_for_a_running_answer(monkeypatch):
    w = IdleWorker()
    w.pending["r1"] = object()
    monkeypatch.setattr(llm, "worker", w)
    monkeypatch.setattr(llm, "last_used", 0.0)
    assert llm.stop_if_idle() is False


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


def test_cancel_right_after_started_is_not_lost():
    gen = llm.generate(user("SLOW"))
    rid = next(gen)["id"]  # the worker has not seen the request yet
    assert llm.cancel(rid)
    assert list(gen)[-1]["finish"] == "cancelled"


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


def test_claim_unloads_only_for_a_job_that_does_not_fit():
    llm.complete(user("hi"))
    llm.claim(14.0)  # 18 + 14 + 8 reserve <= 48
    assert llm.status()["state"] == "ready"
    assert llm.complete(user("hi"))["content"] == "Hello world"  # runs next to it
    llm.release()
    llm.claim(35.0)
    assert llm.status()["state"] == "off"


def test_tool_peak():
    assert models.tool_peak("video.t2v") == 35.0
    assert models.tool_peak("video.mix") == 0.0


def test_a_request_waits_while_a_big_job_runs():
    llm.claim(35.0)
    gen = llm.generate(user("hi"))
    assert next(gen)["event"] == "started"
    assert next(gen) == {"event": "waiting", "message": llm.WAITING}
    assert llm.status()["waiting"] == 1
    threading.Timer(0.2, llm.release).start()
    assert list(gen)[-1]["content"] == "Hello world"
    assert llm.status()["waiting"] == 0


def test_cancel_while_waiting():
    llm.claim(35.0)
    gen = llm.generate(user("hi"))
    rid = next(gen)["id"]
    next(gen)
    assert llm.cancel(rid)
    assert list(gen) == [
        {
            "event": "done",
            "finish": "cancelled",
            "reasoning": "",
            "content": "",
            "stats": {},
        }
    ]
    assert llm.status()["waiting"] == 0 and llm.status()["state"] == "off"


def test_claim_waits_for_the_running_answer():
    gen = llm.generate(user("SLOW"))
    rid = next(gen)["id"]
    next(gen)
    claimed = threading.Event()
    threading.Thread(
        target=lambda: (llm.claim(35.0), claimed.set()), daemon=True
    ).start()
    assert not claimed.wait(0.3)  # the answer is still running
    llm.cancel(rid)
    assert list(gen)[-1]["finish"] == "cancelled"
    assert claimed.wait(5)
    assert llm.status()["state"] == "off"


def test_waiting_requests_run_before_the_next_job():
    llm.claim(35.0)
    result = {}
    t = threading.Thread(
        target=lambda: result.update(llm.complete(user("hi"))), daemon=True
    )
    t.start()
    assert wait_for(lambda: llm.status()["waiting"] == 1)
    llm.release()
    llm.claim(35.0)  # the next job: the waiting request goes first
    t.join(5)
    assert result["content"] == "Hello world"
    assert llm.status()["state"] == "off"


def test_too_little_memory_is_an_error_not_a_wait(monkeypatch):
    monkeypatch.setattr(models, "mem_total_gb", lambda: 16.0)
    with pytest.raises(llm.LLMError, match="this Mac has less"):
        llm.complete(user("hi"))


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


def test_complete_gives_the_request_id_at_start():
    seen = []
    done = llm.complete(user("hi"), on_start=seen.append)
    assert done["content"] == "Hello world" and seen and seen[0].startswith("r")


def enhance_in_thread(client, draft, request_id):
    result = {}
    form = {"tool": "video.t2v", "field": "prompt", "prompt": draft}
    if request_id is not None:
        form["request_id"] = request_id
    t = threading.Thread(
        target=lambda: result.update(r=client.post("/api/assist/enhance", data=form)),
        daemon=True,
    )
    t.start()
    return t, result


def test_assist_cancel_while_waiting_for_memory(client):
    llm.claim(35.0)
    t, result = enhance_in_thread(client, "a gull", "w-1")
    assert wait_for(lambda: llm.status()["waiting"] == 1)
    r = client.post("/api/assist/cancel", json={"request_id": "w-1"})
    assert r.json() == {"status": "cancelling"}
    t.join(5)
    assert result["r"].status_code == 409 and result["r"].json() == {
        "detail": "Cancelled."
    }
    assert not assist.running and not llm.cancels
    assert llm.status()["waiting"] == 0 and llm.status()["state"] == "off"


def test_assist_cancel_while_generating(client):
    t, result = enhance_in_thread(client, "SLOW", "g_1")
    assert wait_for(lambda: llm.status()["busy"])
    time.sleep(0.1)  # some tokens are out
    assert client.post("/api/assist/cancel", json={"request_id": "g_1"}).json() == {
        "status": "cancelling"
    }
    t.join(5)
    assert result["r"].status_code == 409
    assert not assist.running and not llm.cancels and not llm.status()["busy"]
    assert client.post("/api/assist/cancel", json={"request_id": "g_1"}).json() == {
        "status": "idle"
    }


def test_assist_cancel_before_the_llm_request_starts():
    req = assist.Running()
    req.cancel()  # the request is still reading its inputs: no LLM request yet
    tool = tools.BY_ID["video.t2v"]
    target = next(f for f in tool["fields"] if f["name"] == "prompt")
    with pytest.raises(assist.Cancelled):
        assist.local(tool, target, {"prompt": "SLOW"}, [], req.started)
    assert not llm.cancels and llm.status()["state"] == "off"  # the model never loaded
    with pytest.raises(assist.Cancelled):
        assist.enhance(tool, "prompt", {"prompt": "SLOW"}, [], req)


def test_assist_cancel_unknown_and_bad_ids(client):
    r = client.post("/api/assist/cancel", json={"request_id": "nope"})
    assert r.status_code == 200 and r.json() == {"status": "idle"}
    assert client.post("/api/assist/cancel", json={}).status_code == 422
    form = {"tool": "video.t2v", "field": "prompt", "prompt": "a gull"}
    for bad in ("a b", "x" * 65, "../x"):
        r = client.post("/api/assist/enhance", data={**form, "request_id": bad})
        assert r.status_code == 400 and "request_id" in r.json()["detail"]


def test_assist_requests_are_forgotten_when_they_end(client):
    form = {"tool": "video.t2v", "field": "prompt", "request_id": "c1"}
    r = client.post("/api/assist/enhance", data={**form, "prompt": "a gull"})
    assert r.status_code == 200 and not assist.running
    r = client.post("/api/assist/enhance", data={**form, "prompt": " "})
    assert r.status_code == 400 and not assist.running
    t, result = enhance_in_thread(client, "SLOW", "c1")
    assert wait_for(lambda: llm.status()["busy"])
    r = client.post("/api/assist/enhance", data={**form, "prompt": "a gull"})
    assert r.status_code == 400 and "in use" in r.json()["detail"]
    assist.cancel("c1")
    t.join(5)
    assert result["r"].status_code == 409 and not assist.running


def test_assist_stopped_by_the_app_is_an_error_not_a_cancel():
    llm.claim(35.0)
    result = {}

    def run():
        try:
            assist.enhance(
                tools.BY_ID["video.t2v"],
                "prompt",
                {"prompt": "hi"},
                [],
                assist.Running(),
            )
        except (assist.AssistError, assist.Cancelled) as e:
            result["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert wait_for(lambda: llm.status()["waiting"] == 1)
    llm.close()
    t.join(5)
    assert isinstance(result["e"], assist.AssistError)


@pytest.mark.parametrize("draft", ["SLOW", "waiting"])
def test_assist_is_cancelled_when_the_client_goes_away(draft):
    """A real server: the app's middleware must not hide the disconnect."""
    if draft == "waiting":
        llm.claim(35.0)
    srv = uvicorn.Server(
        uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: srv.started)
        port = srv.servers[0].sockets[0].getsockname()[1]
        body = (
            "--b\r\n"
            + "".join(
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n--b\r\n'
                for k, v in (
                    ("tool", "video.t2v"),
                    ("field", "prompt"),
                    ("request_id", "gone"),
                )
            )
            + f'Content-Disposition: form-data; name="prompt"\r\n\r\n{draft}\r\n--b--\r\n'
        ).encode()
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(
            b"POST /api/assist/enhance HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: multipart/form-data; boundary=b\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        if draft == "waiting":
            assert wait_for(lambda: llm.status()["waiting"] == 1)
        else:
            assert wait_for(lambda: llm.status()["busy"])
        assert "gone" in assist.running
        sock.close()
        assert wait_for(lambda: not assist.running and not llm.cancels)
        assert llm.status()["waiting"] == 0 and not llm.status()["busy"]
    finally:
        srv.should_exit = True
        thread.join(5)


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
        daemon=True,  # a failing test must not keep pytest from exiting
    )
    t.start()
    assert wait_for(lambda: client.get(f"/api/chats/{cid}").json()["running"])
    assert client.delete(f"/api/chats/{cid}").status_code == 409
    assert wait_for(  # the first words are there, so the saved reply keeps them
        lambda: (
            chat.live.get(cid) is not None
            and any(e["event"] == "content" for e in chat.live[cid].events)
        )
    )
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    reply = client.get(f"/api/chats/{cid}").json()["messages"][-1]
    assert reply["finish"] == "cancelled" and reply["content"].startswith("tick")
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "idle"}
    assert client.delete(f"/api/chats/{cid}").status_code == 200
    assert client.get(f"/api/chats/{cid}").status_code == 404


def test_chat_shows_waiting_and_can_stop_it(client):
    llm.claim(35.0)
    cid = new_chat(client)
    t = threading.Thread(
        target=client.post,
        args=(f"/api/chats/{cid}/messages",),
        kwargs={"data": {"text": "hi"}},
        daemon=True,
    )
    t.start()
    assert wait_for(
        lambda: (
            chat.live.get(cid) is not None
            and any(e["event"] == "waiting" for e in chat.live[cid].events)
        )
    )
    assert client.post(f"/api/chats/{cid}/stop").json() == {"status": "stopping"}
    t.join(5)
    reply = client.get(f"/api/chats/{cid}").json()["messages"][-1]
    assert reply["finish"] == "cancelled" and reply["content"] == ""


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


def test_system_prompt_names_every_tool_with_prompt_help():
    system = chat.system_prompt({"mode": "workstation"})
    for tool in tools.TOOLS:
        names = [f["name"] for f in tool["fields"] if f.get("assist")]
        if names:
            assert f"- {tool['id']}: {tool['label']}" in system
            assert ", ".join(names) in system
    assert "edit.mix" not in system  # nothing to write for it
    assert "emoji" in system


def test_system_prompt_follows_the_mode(monkeypatch):
    monkeypatch.setattr(chat, "today", lambda: "Tuesday 3 November 2026")
    workstation = chat.system_prompt({"mode": "workstation"})
    assert chat.system_prompt({}) == workstation  # a chat saved before modes
    general = chat.system_prompt({"mode": "general"})
    assert "Tuesday 3 November 2026" in general and "emoji" in general
    assert "video.t2v" not in general and chat.PROMPT_FENCE + " " not in general
    assert "Workstation" not in general
    # agent mode gets the general prompt until it has tools
    assert chat.system_prompt({"mode": "agent"}) == general


def test_model_input_counts_the_chat_s_own_system_prompt(monkeypatch):
    monkeypatch.setattr(chat, "today", lambda: "Tuesday 3 November 2026")
    monkeypatch.setattr(chat, "CONTEXT_TOKENS", 1500)
    msgs = [{"role": "user", "content": "x" * 92, "images": []} for _ in range(40)]
    kept = {}
    for mode in ("workstation", "general"):
        c = {"id": "c", "mode": mode, "messages": msgs}
        messages, _ = chat.model_input(c, 256)
        assert messages[0] == {"role": "system", "content": chat.system_prompt(c)}
        budget = 1500 - 256 - len(chat.system_prompt(c)) // 3
        kept[mode] = len(messages) - 1
        assert kept[mode] == budget // 38  # 92 // 3 + 8 tokens a message
    assert kept["general"] > kept["workstation"]


def test_chat_modes(client):
    old = new_chat(client)  # a chat saved before modes: no "mode" key
    path = chat.CHATS / old / "chat.json"
    data = json.loads(path.read_text())
    del data["mode"]
    path.write_text(json.dumps(data))
    assert client.get(f"/api/chats/{old}").json()["mode"] == "workstation"
    assert client.get("/api/chats").json()[0]["mode"] == "workstation"

    assert client.post("/api/chats").json()["mode"] == "workstation"
    cid = client.post("/api/chats", json={"mode": "general"}).json()["id"]
    assert client.get(f"/api/chats/{cid}").json()["mode"] == "general"
    assert client.post("/api/chats", json={"mode": "agent"}).json()["mode"] == "agent"
    assert client.post("/api/chats", json={"mode": "x"}).status_code == 400

    r = client.patch(f"/api/chats/{cid}", json={"mode": "workstation"})
    assert r.status_code == 200 and r.json()["mode"] == "workstation"
    assert client.patch(f"/api/chats/{cid}", json={"mode": "x"}).status_code == 400
    assert client.patch("/api/chats/nope", json={"mode": "general"}).status_code == 404
    client.patch(f"/api/chats/{cid}", json={"mode": "general"})
    client.post(f"/api/chats/{cid}/messages", data={"text": "hi"})
    r = client.patch(f"/api/chats/{cid}", json={"mode": "workstation"})
    assert r.status_code == 409
    assert client.get(f"/api/chats/{cid}").json()["mode"] == "general"


def test_mode_changes_only_from_the_app_s_page(client):
    cid = new_chat(client)
    r = client.patch(
        f"/api/chats/{cid}",
        json={"mode": "general"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert client.get(f"/api/chats/{cid}").json()["mode"] == "workstation"


def test_closing_at_started_frees_the_request():
    gen = llm.generate(user("hi"))
    next(gen)
    gen.close()
    assert llm.active == 0 and not llm.cancels
    llm.claim(35.0)
    gen = llm.generate(user("hi"))
    next(gen)
    gen.close()
    assert not llm.waiting and llm.status()["waiting"] == 0


def test_cancel_before_the_model_loads_does_not_load_it():
    gen = llm.generate(user("hi"))
    rid = next(gen)["id"]
    llm.cancel(rid)
    assert list(gen)[-1]["finish"] == "cancelled"
    assert llm.status()["state"] == "off"


def test_close_cancels_waiting_requests_and_starts_no_worker():
    llm.claim(35.0)
    gen = llm.generate(user("hi"))
    next(gen)
    next(gen)  # waiting
    llm.close()
    llm.release()
    assert list(gen)[-1]["finish"] == "cancelled"
    with pytest.raises(llm.LLMError, match="closed"):
        llm.complete(user("hi"))
    assert llm.status()["state"] == "off"


def test_claim_gives_up_when_the_job_is_cancelled():
    gen = llm.generate(user("SLOW"))
    rid = next(gen)["id"]
    next(gen)
    off = threading.Event()
    result = {}
    t = threading.Thread(
        target=lambda: result.update(ok=llm.claim(35.0, off.is_set)), daemon=True
    )
    t.start()
    time.sleep(0.2)
    off.set()  # the job was cancelled while its claim waited
    t.join(5)
    assert result == {"ok": False}
    assert llm.status()["media_peak_gb"] == 0.0 and llm.status()["state"] == "ready"
    assert llm.cancel(rid)
    list(gen)


def test_job_shows_why_it_waits(monkeypatch):
    jobs = {
        j: {
            "id": j,
            "tool": "video.t2v",
            "status": "queued",
            "params": {},
            "created": c,
        }
        for j, c in (("j1", "2026-11-02T10:00:01"), ("j2", "2026-11-02T10:00:02"))
    }
    monkeypatch.setattr(server, "jobs", jobs)
    monkeypatch.setattr(server, "claiming", {"j1"})
    assert "chat answer" in server.view(jobs["j1"])["waiting"]
    assert "waiting" not in server.view(jobs["j2"])


def test_prompt_help_is_off_where_the_model_cannot_run(monkeypatch):
    assert assist.status()["available"] is True
    monkeypatch.setattr(models, "mem_total_gb", lambda: 24.0)  # 18 + 8 reserve > 24
    assert assist.status()["available"] is False
