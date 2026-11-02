"""Chat with the local LLM: conversations stored on disk, answers streamed to the page.

Each chat is a folder outputs/chats/<id>/ with chat.json (the messages) and the images sent in it. An answer runs in
its own thread and is saved when it ends, whether or not the page still listens: the page reads it as server-sent
events from POST .../messages and can reconnect with GET .../stream. Stop cancels it between tokens. The model sees as
many recent turns as fit in CONTEXT_TOKENS.
"""

import asyncio
import json
import re
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app import llm
from app.config import ROOT

CHATS = ROOT / "outputs" / "chats"
ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
IMAGE = re.compile(r"img_\d+\.(png|jpg|jpeg|webp)")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_IMAGES = 4  # per message
MAX_IMAGE = 20 * 1024 * 1024
MAX_TEXT = 20000  # characters per message
# the 16 full-attention layers take 64 KiB of KV cache per token: 32K tokens is 2 GiB on top of the weights
CONTEXT_TOKENS = 32768
IMAGE_TOKENS = 1100  # an image at the worker's 1024x1024 pixel cap
SYSTEM = (
    "You are a helpful assistant in Workstation, a media app that runs open models locally on the user's Mac. "
    "Answer in the user's language. Be clear and concise; use Markdown when it helps."
)

router = APIRouter(prefix="/api")
lock = threading.Lock()  # guards chat files and live
live: dict[str, "Live"] = {}  # chat id -> its running answer


class Live:
    """A running answer: the events so far, for every page that streams it."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.rid: str | None = None
        self.stop = False


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def folder(chat_id: str) -> Path:
    if not ID.fullmatch(chat_id) or not (CHATS / chat_id / "chat.json").exists():
        raise HTTPException(404, "No such chat")
    return CHATS / chat_id


def load(chat_id: str) -> dict[str, Any]:
    return json.loads((folder(chat_id) / "chat.json").read_text())


def save(chat: dict[str, Any]) -> None:
    path = CHATS / chat["id"] / "chat.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=1))
    tmp.replace(path)


def summary(chat: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": chat["id"],
        "title": chat["title"],
        "created": chat["created"],
        "updated": chat["updated"],
        "messages": len(chat["messages"]),
        "running": chat["id"] in live,
    }


def tokens(msg: dict[str, Any]) -> int:
    """A rough count (about 3 characters a token across languages), plus the images."""
    text = msg.get("content", "") + msg.get("reasoning", "")
    return len(text) // 3 + 8 + IMAGE_TOKENS * len(msg.get("images", []))


def model_input(
    chat: dict[str, Any], max_tokens: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """The messages and image paths for the model: the newest turns that fit, oldest dropped first."""
    budget = CONTEXT_TOKENS - max_tokens - len(SYSTEM) // 3
    kept: list[dict[str, Any]] = []
    for msg in reversed(chat["messages"]):
        if msg["role"] == "assistant" and msg.get("finish") == "cancelled":
            continue
        budget -= tokens(msg)
        if budget < 0 and kept:
            break
        kept.append(msg)
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # a turn starts with the user
        kept.pop(0)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]
    images: list[str] = []
    for msg in kept:
        if msg["role"] == "user":
            parts: list[dict[str, Any]] = [{"type": "image"} for _ in msg["images"]]
            parts.append({"type": "text", "text": msg["content"]})
            images += [str(CHATS / chat["id"] / name) for name in msg["images"]]
            messages.append({"role": "user", "content": parts})
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg["content"],
                    "reasoning_content": msg.get("reasoning", ""),
                }
            )
    return messages, images


def answer(chat_id: str, thinking: bool, run: Live) -> None:
    """The answer thread: run the model, collect the events, save the reply."""
    with lock:
        chat = load(chat_id)
    max_tokens = 8192 if thinking else 4096  # thinking counts against the limit
    messages, images = model_input(chat, max_tokens)
    reply: dict[str, Any] = {"role": "assistant", "content": "", "reasoning": ""}
    try:
        for ev in llm.generate(
            messages,
            images,
            thinking=thinking,
            max_tokens=max_tokens,
            chat=chat_id,
        ):
            if ev["event"] == "started":
                run.rid = ev["id"]
                if run.stop:
                    llm.cancel(run.rid)
                continue
            if ev["event"] == "reasoning":
                reply["reasoning"] += ev["text"]
            elif ev["event"] == "content":
                reply["content"] += ev["text"]
            elif ev["event"] == "done":
                reply.update(
                    content=ev["content"],
                    reasoning=ev["reasoning"],
                    finish=ev["finish"],
                    stats=ev["stats"],
                )
            elif ev["event"] == "error":
                reply.update(finish="error", error=ev["message"])
            run.events.append(ev)
    except llm.LLMError as e:
        reply.update(finish="error", error=str(e))
        run.events.append({"event": "error", "message": str(e)})
    finally:
        reply["time"] = now()
        with lock:
            chat = load(chat_id)
            chat["messages"].append(reply)
            chat["updated"] = reply["time"]
            save(chat)
            live.pop(chat_id, None)
        run.done = True


async def stream(request: Request, run: Live) -> Any:
    """Server-sent events of a running answer from its first event; a page that leaves stops only its own stream."""
    sent = 0
    while True:
        events, done = run.events[sent:], run.done
        for ev in events:
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        sent += len(events)
        if done and sent == len(run.events):
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.05)


@router.get("/llm")
def llm_status() -> dict[str, Any]:
    return llm.status()


@router.post("/llm/unload")
def llm_unload() -> dict[str, str]:
    llm.stop("Unloaded from the app.")
    return {"status": "off"}


@router.get("/chats")
def list_chats() -> list[dict[str, Any]]:
    out = []
    with lock:
        for path in CHATS.glob("*/chat.json") if CHATS.exists() else []:
            try:
                out.append(summary(json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


@router.post("/chats")
def create_chat() -> dict[str, Any]:
    chat_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    chat = {
        "id": chat_id,
        "title": "New chat",
        "created": now(),
        "updated": now(),
        "messages": [],
    }
    with lock:
        (CHATS / chat_id).mkdir(parents=True)
        save(chat)
    return summary(chat)


@router.get("/chats/{chat_id}")
def get_chat(chat_id: str) -> dict[str, Any]:
    with lock:
        chat = load(chat_id)
    return {**chat, "running": chat_id in live}


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict[str, str]:
    with lock:
        path = folder(chat_id)
        if chat_id in live:
            raise HTTPException(409, "Stop the answer first.")
        shutil.rmtree(path)
    return {"status": "deleted"}


@router.get("/chats/{chat_id}/files/{name}")
def chat_file(chat_id: str, name: str) -> FileResponse:
    path = folder(chat_id) / name
    if not IMAGE.fullmatch(name) or not path.is_file() or path.is_symlink():
        raise HTTPException(404, "No such file")
    return FileResponse(path)


@router.post("/chats/{chat_id}/messages")
async def send_message(chat_id: str, request: Request) -> StreamingResponse:
    """A new user message (text, optional images, thinking on/off), or regenerate=true to answer the last one again."""
    form = await request.form()
    text = str(form.get("text") or "").replace("\r\n", "\n").strip()
    thinking = str(form.get("thinking", "false")).lower() == "true"
    regenerate = str(form.get("regenerate", "false")).lower() == "true"
    uploads = [u for u in form.getlist("images") if getattr(u, "filename", None)]
    if len(text) > MAX_TEXT:
        raise HTTPException(
            400, f"The message is too long (max {MAX_TEXT} characters)."
        )
    if len(uploads) > MAX_IMAGES:
        raise HTTPException(400, f"At most {MAX_IMAGES} images per message.")
    files = []
    for up in uploads:
        ext = IMAGE_TYPES.get(up.content_type or "")
        if ext is None:
            raise HTTPException(400, f"{up.filename}: use a PNG, JPEG or WebP image.")
        if (up.size or 0) > MAX_IMAGE:
            raise HTTPException(400, f"{up.filename}: image too large (max 20 MB).")
        files.append((ext, await up.read()))
    if not regenerate and not text and not files:
        raise HTTPException(400, "Write a message first.")
    with lock:
        chat = load(chat_id)
        if chat_id in live:
            raise HTTPException(409, "This chat is still answering.")
        if regenerate:
            while chat["messages"] and chat["messages"][-1]["role"] == "assistant":
                chat["messages"].pop()
            if not chat["messages"]:
                raise HTTPException(400, "Nothing to answer again.")
        else:
            n = sum(len(m.get("images", [])) for m in chat["messages"])
            names = []
            for i, (ext, data) in enumerate(files):
                name = f"img_{n + i}{ext}"
                (CHATS / chat_id / name).write_bytes(data)
                names.append(name)
            chat["messages"].append(
                {"role": "user", "content": text, "images": names, "time": now()}
            )
            if chat["title"] == "New chat" and text:
                chat["title"] = text.splitlines()[0][:60]
        chat["updated"] = now()
        save(chat)
        run = live[chat_id] = Live()
    threading.Thread(target=answer, args=(chat_id, thinking, run), daemon=True).start()
    return StreamingResponse(stream(request, run), media_type="text/event-stream")


@router.get("/chats/{chat_id}/stream")
async def resume_stream(chat_id: str, request: Request) -> StreamingResponse:
    """The running answer's events from the start (for a page that came back); 404 when nothing runs."""
    folder(chat_id)
    run = live.get(chat_id)
    if run is None:
        raise HTTPException(404, "Nothing is running in this chat.")
    return StreamingResponse(stream(request, run), media_type="text/event-stream")


@router.post("/chats/{chat_id}/stop")
def stop_answer(chat_id: str) -> dict[str, str]:
    folder(chat_id)
    run = live.get(chat_id)
    if run is None:
        return {"status": "idle"}
    run.stop = True
    if run.rid is not None:
        llm.cancel(run.rid)
    return {"status": "stopping"}
