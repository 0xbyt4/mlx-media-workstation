"""The file-writing and media tools in the chat loop (app/chat.py Turn): the approval card shows edit_file's diff, a
call that cannot run is not asked, a file that changed after the card is not written, view_image's picture reaches
the model's next request, and the page can show a written file in Finder. Same fakes as tests/test_agent_chat.py."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import chat, server
from tests import test_agent_chat as base
from tests.test_agent_chat import (
    ORIGIN,
    PNG,
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

# the fixtures of the agent chat tests: the fake worker, the fake Workstation, network and sandbox, the client
fake_llm = base.fake_llm
reg = base.reg
ws = base.ws
box = base.box
shell = base.shell
net = base.net
client = base.client


def grant(cid: str, folder: Path) -> Path:
    """Grant the folder as the server stores it (its disk spelling), without the home-folder rules of the endpoint."""
    real = chat.disk_path(folder)
    path = chat.CHATS / cid / "chat.json"
    data = json.loads(path.read_text())
    data["agent"]["folder"] = str(real)
    path.write_text(json.dumps(data))
    return real


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "granted"
    root.mkdir()
    (root / "todo.md").write_text("# Todo\nbuy milk\nCall Ada\n")
    return root


def test_edit_file_asks_with_the_diff_and_keeps_a_backup(client, folder):
    cid = agent_chat(client)
    grant(cid, folder)
    args = {"path": "todo.md", "old_text": "buy milk", "new_text": "buy tea"}
    t, resp = start(client, cid, script([call("edit_file", **args)], "RESULT"))
    ask = pending(cid)
    assert ask["name"] == "edit_file" and ask["risk"] == 3
    assert ask["chat_scope"] is False and ask["network"] is False
    assert ask["preview"].splitlines() == [
        "--- a/todo.md",
        "+++ b/todo.md",
        "@@ -1,3 +1,3 @@",
        " # Todo",
        "-buy milk",
        "+buy tea",
        " Call Ada",
    ]
    assert (
        folder / "todo.md"
    ).read_text() == "# Todo\nbuy milk\nCall Ada\n"  # not before the answer
    assert approve(client, cid, ask["id"], "chat").status_code == 400  # every time
    approve(client, cid, ask["id"], "once")
    t.join(5)
    assert (folder / "todo.md").read_text() == "# Todo\nbuy tea\nCall Ada\n"
    res = next(e for e in events(resp["r"]) if e["event"] == "tool_result")
    assert res["status"] == "ok" and "backups/" in res["content"]
    backup, written = res["files"][1], res["files"][0]
    assert written == {"path": "todo.md", "url": "", "kind": "saved"}
    assert backup["url"].startswith(f"/api/chats/{cid}/ws/backups/")
    assert client.get(backup["url"]).text == "# Todo\nbuy milk\nCall Ada\n"
    state = saved(client, cid)["agent"]
    assert state["tainted"] is False and state["grants"] == []


def test_a_change_that_cannot_be_made_is_not_asked(client, folder):
    cid = agent_chat(client)
    grant(cid, folder)
    steps = script(
        [call("edit_file", path="todo.md", old_text="no such text", new_text="x")],
        "RESULT",
    )
    evs = send(client, cid, steps)
    assert "approval" not in [e["event"] for e in evs]
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "error" and "old_text was not found" in res["content"]


def test_a_file_that_changes_after_the_card_is_not_written(client, folder):
    cid = agent_chat(client)
    grant(cid, folder)
    args = {"path": "todo.md", "old_text": "Call Ada", "new_text": "Call Grace"}
    t, resp = start(client, cid, script([call("edit_file", **args)], "RESULT"))
    ask = pending(cid)
    (folder / "todo.md").write_text("# Todo\nsell the car\nCall Ada\n")
    approve(client, cid, ask["id"], "once")
    t.join(5)
    res = next(e for e in events(resp["r"]) if e["event"] == "tool_result")
    assert res["status"] == "error" and "changed after the user saw" in res["content"]
    assert (folder / "todo.md").read_text() == "# Todo\nsell the car\nCall Ada\n"


def test_write_file_can_be_allowed_for_the_chat(client, folder):
    cid = agent_chat(client)
    grant(cid, folder)
    steps = script(
        [call("write_file", path="a.md", content="one\n")],
        [call("write_file", path="b.md", content="two\n")],
        "RESULT",
    )
    t, resp = start(client, cid, steps)
    ask = pending(cid)
    assert ask["chat_scope"] is True and ask["risk"] == 2 and ask["network"] is False
    assert ask["preview"].splitlines()[:2] == ["--- /dev/null", "+++ b/a.md"]
    approve(client, cid, ask["id"], "chat")
    t.join(5)
    evs = events(resp["r"])
    assert [e["event"] for e in evs].count("approval") == 1
    assert (folder / "a.md").read_text() == "one\n" and (
        folder / "b.md"
    ).read_text() == "two\n"
    assert saved(client, cid)["agent"]["grants"] == ["write_file"]


def test_the_tools_menu_names_what_the_file_tools_do(client, reg):
    cid = agent_chat(client)
    view = client.get(f"/api/chats/{cid}/agent").json()
    by = {t["name"]: t for t in view["tools"]}
    for name in (
        "write_file",
        "edit_file",
        "copy_to_workspace",
        "export_from_workspace",
    ):
        assert by[name]["enabled"] is True, name  # on by default: the writers ask first
    assert "never overwrites" in by["write_file"]["risk_text"]
    assert "Asks before every call" in by["edit_file"]["risk_text"]
    assert "Runs without asking" in by["view_image"]["risk_text"]
    assert by["edit_file"]["label"] == "Change a file"


def test_reveal_shows_a_file_of_the_granted_folder(client, folder, monkeypatch):
    cid = agent_chat(client)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        chat.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0),
    )

    def reveal(path, headers=ORIGIN):
        return client.post(
            f"/api/chats/{cid}/reveal", json={"path": path}, headers=headers
        )

    assert reveal("todo.md").status_code == 409  # no folder yet
    real = grant(cid, folder)
    (folder / ".env").write_text("dummy")
    assert reveal("todo.md").json() == {"status": "shown"}
    assert ran == [["open", "-R", str(real / "todo.md")]]
    for bad in ("../x", ".env", "missing.md", "/etc/hosts"):
        assert reveal(bad).status_code == 404, bad
    assert (
        reveal("todo.md", headers={"Origin": "http://evil.example"}).status_code == 403
    )
    assert len(ran) == 1


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


@needs_ffmpeg
def test_view_image_puts_the_picture_into_the_next_request(client, folder, tmp_path):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48", "-frames:v", "1",
         str(folder / "pic.png")],
        check=True,
    )  # fmt: skip
    cid = agent_chat(client)
    grant(cid, folder)
    evs = send(client, cid, script([call("view_image", path="pic.png")], "RESULT"))
    assert "approval" not in [e["event"] for e in evs]  # R1: the grant is the consent
    res = next(e for e in evs if e["event"] == "tool_result")
    assert res["status"] == "ok"
    (image,) = res["files"]
    name = image["path"]
    assert image == {
        "path": name,
        "url": f"/api/chats/{cid}/files/{name}",
        "kind": "image",
    }
    got = client.get(image["url"])
    assert got.status_code == 200 and got.content.startswith(b"\x89PNG")
    msgs = saved(client, cid)["messages"]
    assert msgs[2]["role"] == "tool" and msgs[2]["images"] == [name]
    seen = msgs[3]["stats"]["seen"]
    tool = seen["messages"][-1]
    assert tool["role"] == "tool"
    assert tool["content"][0] == {"type": "image"}
    assert tool["content"][1]["text"].startswith(
        "pic.png (64x48): the image is attached"
    )
    assert seen["images"] == [str(chat.CHATS / cid / name)]
    assert saved(client, cid)["agent"]["tainted"] is True


def test_only_the_newest_tool_images_go_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CHATS", tmp_path)
    (tmp_path / "c").mkdir()
    names = [f"view_{i:012x}.png" for i in range(3)]
    for n in names:
        (tmp_path / "c" / n).write_bytes(PNG)
    calls = [
        {"id": f"c{i}", "name": "view_image", "arguments": {"path": "p.png"}}
        for i in range(3)
    ]
    msgs = [{"role": "user", "content": "look", "images": []}]
    for i, n in enumerate(names):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning": "",
                "tool_calls": [calls[i]],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "view_image",
                "content": f"image {i}",
                "images": [n],
            }
        )
    msgs[-1]["images"].append(
        "view_ffffffffffff.png"
    )  # removed from the disk: not sent
    c = {"id": "c", "mode": "agent", "agent": chat.default_agent(), "messages": msgs}
    sent, images = chat.model_input(c, 256)
    tools = [m for m in sent if m["role"] == "tool"]
    assert tools[0]["content"].startswith("image 0\n[The image is no longer attached")
    assert [p["type"] for p in tools[1]["content"]] == ["image", "text"]
    assert [p["type"] for p in tools[2]["content"]] == ["image", "text"]
    assert images == [str(tmp_path / "c" / n) for n in names[1:]]
    # a file name that is not a view_image copy is never sent
    msgs[-1]["images"] = ["../chat.json"]
    _, images = chat.model_input(c, 256)
    assert str(tmp_path / "c" / "../chat.json") not in images


def test_chat_files_serve_view_copies_only(client):
    cid = agent_chat(client)
    (chat.CHATS / cid / "view_0123456789ab.png").write_bytes(PNG)
    (chat.CHATS / cid / "view_x.png").write_bytes(PNG)
    os.symlink(
        chat.CHATS / cid / "chat.json", chat.CHATS / cid / "view_aaaaaaaaaaaa.png"
    )
    assert (
        client.get(f"/api/chats/{cid}/files/view_0123456789ab.png").status_code == 200
    )
    assert client.get(f"/api/chats/{cid}/files/view_x.png").status_code == 404
    assert (
        client.get(f"/api/chats/{cid}/files/view_aaaaaaaaaaaa.png").status_code == 404
    )


def test_the_server_port_gives_listed_outputs_only(state, make_job):
    make_job(
        "20261103-100000-aaaa",
        "done",
        outputs=[{"name": "image.png", "kind": "image", "url": ""}],
    )
    job_dir = state / "20261103-100000-aaaa"
    (job_dir / "image.png").write_bytes(PNG)
    (job_dir / "other.png").write_bytes(PNG)
    make_job("20261103-100000-bbbb", "running")
    port = server.Workstation()
    assert (
        port.output_file("20261103-100000-aaaa", "image.png") == job_dir / "image.png"
    )
    assert port.output_file("20261103-100000-aaaa", "other.png") is None  # not listed
    assert port.output_file("20261103-100000-aaaa", "../x.png") is None
    assert port.output_file("20261103-100000-bbbb", "image.png") is None
    assert port.output_file("missing", "image.png") is None
