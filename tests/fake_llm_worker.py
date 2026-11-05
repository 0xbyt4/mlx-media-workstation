"""A stand-in for app/llm_worker.py that speaks its JSON-line protocol without a model (tests only).

The model path argument picks the behaviour: "fail" reports a load error, "crash" exits after loading, "slowsave"
starts a save and never ends it, "lagsave" waits 0.5 s before it saves. A user message containing SLOW streams tokens
until cancelled (also with a schema); any other request with a schema answers JSON.

The prompt cache is a stand-in too: one chat's "tokens" are its message count plus one for the answer. A request of the
kept chat gets "cached_tokens" in its done stats (0 for another chat), and a cancelled answer drops the cache, as in
the worker. "save" writes a cache file with only the header metadata of the real one (llm_worker.cache_info reads it);
"restore" checks it as the worker does (format, model tag, runtime "fake", chat) and deletes it either way.

With tools, the fake streams scripted tool-call text through the real worker's hold-back and parsing (ToolText,
finish_tools), with a small stand-in for mlx-vlm's qwen3_coder parser. The last message picks the script (see
SCRIPTS); a tool result as the last message gets the answer "Result: <content>". The done stats then echo what the
request carried ("seen"), so a test can check what reached the worker; a request without tools echoes its thinking
settings (LIMITS) the same way, and its "draft" folder when it names one.

An agent test scripts a whole answer: the last user message holds "STEPS:" and a JSON list, one item per model step
(the number of assistant messages after that user message picks the item; past the end the answer is "Done."). An
item is a text answer ("RESULT" answers "Result: <last tool message>", "SLOW" streams until cancelled), a list of
calls, or {"text": ..., "calls": [...], "think": ..., "think_calls": [...]}: "think" is reasoning, and "think_calls"
are written at its end, as Qwen sometimes does. A call is {"name": ..., "arguments": {...}}, written as the model
writes it, or {"raw": "..."}: that text inside the call markers, to test calls that do not parse. A request without
tools answers a STEPS: script's text item too (the eval's general chats); an item with calls stays "Hello world".

The done stats also carry "prefix": a hash of the request's tools array and system message, so a test can check that
the start of the prompt (what the template puts before the conversation) never changes inside a chat. The app's own
user messages (they start with "[The app:") are not the user's: a script is picked from the user message before them.
SLOW streams numbered ticks, so the chat's repetition guard never stops it.

A request with "tool_choice": "none" is the agent's last step: its script item is picked from the user message before
the app's note, and its calls are left out, as the worker's ban of the call marker would do ("FINAL" when it has no
text).
"""

import hashlib
import itertools
import json
import queue
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import llm_worker  # importable through the path above

CALL = "<tool_call>\n<function=get_weather>\n<parameter=city>\n{city}\n</parameter>\n<parameter=days>\n2\n</parameter>\n</function>\n</tool_call>"
# chunks as the model would stream them; the keys are words in the last message
SCRIPTS = {
    # one call, its start marker split across chunks
    "CALL": [
        "Let me check",
        " the weather.\n\n<tool",
        "_ca",
        "ll>\n<function=get_weather>\n<parameter=city>\nAnkara\n</parameter>\n",
        "<parameter=days>\n2\n</parameter>\n</function>\n</tool_call>",
    ],
    # two calls in parallel, no text before them
    "TWO": [
        CALL.format(city="Ankara")[:7],
        CALL.format(city="Ankara")[7:],
        "\n",
        CALL.format(city="Izmir"),
    ],
    # a call without </function>, then a good one
    "BAD": [
        "<tool_call>\n<function=get_weather>\n<parameter=city>\nAnkara\n</tool_call>\n",
        CALL.format(city="Izmir"),
    ],
    # cut off inside a call (max_tokens)
    "CUT": ["Checking.\n<tool_call>\n<function=get_weather>\n<parameter=city>\nAnk"],
    # text that only looks like the start of a marker
    "PARTIAL": ["a <", " b <tool", "box> c <"],
    # text after the call, which the template asks the model not to write
    "AFTER": [CALL.format(city="Ankara"), "\nDone."],
}
# the request's thinking settings, echoed in the done stats as "seen" (app/effort.py)
LIMITS = ("thinking", "reasoning_effort", "thinking_budget", "max_tokens")
PARAM = re.compile(r"<parameter=(.*?)>\n?(.*?)\n?</parameter>", re.DOTALL)
STEPS = "STEPS:"
APP_NOTE = (
    "[The app:"  # the start of the app's own user messages (chat.NUDGE, chat.notice)
)
RUNTIME = "fake"  # the "runtime" tag of the fake's cache files
kept = {"chat": None, "tokens": 0}  # the fake prompt cache


def ticks():
    """An endless stream of numbered ticks: a text that never repeats one block (the chat's repetition guard would
    stop it)."""
    return (f"tick {n} " for n in itertools.count())


def prefix(req):
    """A hash of what the chat template puts before the conversation: the tools array and the system message."""
    head = {"tools": req.get("tools"), "system": req["messages"][0]}
    return hashlib.sha256(json.dumps(head, sort_keys=True).encode()).hexdigest()


def emit(ev):
    llm_worker.emit(ev)


def text_of(message):
    content = message["content"]
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if p.get("type") == "text")


def last_text(messages):
    return text_of(messages[-1])


def call_text(call):
    if "raw" in call:
        return f"<tool_call>\n{call['raw']}\n</tool_call>"
    params = "".join(
        f"<parameter={k}>\n{v if isinstance(v, str) else json.dumps(v)}\n</parameter>\n"
        for k, v in (call.get("arguments") or {}).items()
    )
    return f"<tool_call>\n<function={call['name']}>\n{params}</function>\n</tool_call>"


def scripted(messages, final=False):
    """(the chunks, the reasoning) of this step of a STEPS: script, or None when the last user message has none. The
    app's notes (user messages that start with APP_NOTE: the last step's nudge, a changed setting) are not the user's."""
    users = [
        i
        for i, m in enumerate(messages)
        if m["role"] == "user" and not text_of(m).startswith(APP_NOTE)
    ]
    text = text_of(messages[users[-1]]) if users else ""
    if STEPS not in text:
        return None
    steps = json.loads(text.split(STEPS, 1)[1])
    n = sum(1 for m in messages[users[-1] + 1 :] if m["role"] == "assistant")
    step = steps[n] if n < len(steps) else "Done."
    if step == "RESULT":
        tool = [m for m in messages if m["role"] == "tool"]
        return ["Result: ", text_of(tool[-1]) if tool else "none"], ""
    if step == "SLOW":
        return ticks(), ""
    if isinstance(step, str):
        return [step], ""
    if isinstance(step, list):
        step = {"calls": step}
    if final:
        return [step.get("text") or "FINAL"], step.get("think", "")
    chunks = [step["text"] + "\n\n"] if step.get("text") else []
    think = step.get("think", "")
    if step.get("think_calls"):
        think += "\n\n" + "\n".join(call_text(c) for c in step["think_calls"])
    return chunks + [call_text(c) for c in step.get("calls", [])], think


def parse(text, tools):
    """Like mlx-vlm's qwen3_coder.parse_tool_call: a ValueError when the function block is not closed."""
    found = re.search(r"<function=(.*?)</function>$", text, re.DOTALL)
    if not found:
        raise ValueError("No function provided.")
    name, _, body = found.group(1).partition(">")
    props = next(
        (
            t["function"]["parameters"].get("properties", {})
            for t in tools or []
            if t["function"]["name"] == name
        ),
        {},
    )
    args = {}
    for key, value in PARAM.findall(
        body
    ):  # typed by the schema, as the real parser does
        kind = props.get(key, {}).get("type")
        if kind == "integer":
            args[key] = int(value)
        elif kind == "number":
            args[key] = float(value)
        elif kind in ("boolean", "object", "array"):
            args[key] = json.loads(value)
        else:
            args[key] = value
    return {"name": name, "arguments": args}


def use_cache(req, finish):
    """The cached tokens of this request, and the cache after it (dropped when the answer was cancelled)."""
    chat = req.get("chat")
    cached = kept["tokens"] if chat and chat == kept["chat"] else 0
    if finish == "cancelled" or not chat:
        kept.update(chat=None, tokens=0)
    else:
        kept.update(chat=chat, tokens=len(req["messages"]) + 1)
    return {"cached_tokens": cached} if chat else {}


def save(req, mode):
    path = Path(req["path"])
    tmp = llm_worker.partial(path)
    if kept["chat"] is None:
        emit({"id": req["id"], "event": "saved", "chat": None, "note": "no chat cache"})
        return
    meta = {
        "format": llm_worker.CACHE_FORMAT,
        "model": llm_worker.model_tag(sys.argv[1]),
        "runtime": RUNTIME,
        "chat": kept["chat"],
        "tokens": str(kept["tokens"]),
        "layers": "fake",
    }
    header = json.dumps({"__metadata__": meta}).encode()
    tmp.write_bytes(len(header).to_bytes(8, "little") + header)
    if mode == "slowsave":
        time.sleep(3600)
    if mode == "lagsave":
        time.sleep(0.5)
    tmp.replace(path)
    print(f"prompt cache: saved chat {kept['chat']}", file=sys.stderr)
    emit(
        {
            "id": req["id"],
            "event": "saved",
            "chat": kept["chat"],
            "tokens": kept["tokens"],
        }
    )


def restore(req):
    path = Path(req["path"])
    try:
        info = llm_worker.cache_info(path)
        if (
            info is None
            or info["model"] != llm_worker.model_tag(sys.argv[1])
            or info["runtime"] != RUNTIME
            or (req.get("chat") is not None and info["chat"] != req["chat"])
        ):
            raise ValueError("the cache does not match")
    except ValueError as e:
        emit({"id": req["id"], "event": "error", "message": f"ValueError: {e}"})
        return
    finally:
        path.unlink(missing_ok=True)
    kept.update(chat=info["chat"], tokens=int(info["tokens"]))
    emit(
        {
            "id": req["id"],
            "event": "restored",
            "chat": info["chat"],
            "tokens": kept["tokens"],
        }
    )


def tool_answer(req, cancel):
    """A request with tools: stream the script through the worker's hold-back, then finish as the worker does."""
    rid, messages = req["id"], req["messages"]
    text = last_text(messages) if messages[-1]["role"] != "tool" else ""
    script = scripted(messages, req.get("tool_choice") == "none")
    reasoning = ""
    if script is not None:
        script, reasoning = script
    slow = "SLOW" in text or (script is not None and not isinstance(script, list))
    if reasoning:
        emit({"id": rid, "event": "reasoning", "text": reasoning})
    if script is not None:
        chunks = script
    elif messages[-1]["role"] == "tool":
        chunks = ["Result: ", text_of(messages[-1])]
    elif "SLOW" in text:
        chunks = ticks()
    else:
        chunks = next((v for k, v in SCRIPTS.items() if k in text), ["Hello", " world"])
    cut = "CUT" in text and script is None
    calls, finish = llm_worker.ToolText(), "length" if cut else "stop"
    for chunk in chunks:
        if cancel.is_set():
            finish = "cancelled"
            break
        if shown := calls.feed(chunk):
            emit({"id": rid, "event": "content", "text": shown})
        if slow:
            time.sleep(0.02)
    seen = {
        k: req.get(k)
        for k in (
            "messages",
            "images",
            "tools",
            "tool_choice",
            "chat",
            "allow_emoji",
            "draft",
            *LIMITS,
        )
    }
    done = {
        "id": rid,
        "event": "done",
        "finish": finish,
        "reasoning": reasoning.strip(),
        "content": calls.text,
        "stats": {
            "turns": len(messages),
            "images": len(req["images"]),
            "seen": seen,
            "prefix": prefix(req),
            **use_cache(req, finish),
        },
    }
    llm_worker.finish_tools(done, calls, parse, req["tools"])
    emit(done)


def main():
    llm_worker.proto = sys.stdout
    sys.stdout.reconfigure(line_buffering=True)
    mode = sys.argv[1]
    if mode == "fail":
        emit({"event": "fatal", "message": "Could not load the model: broken"})
        sys.exit(1)
    emit({"event": "ready"})
    if mode == "crash":
        sys.exit(3)
    jobs, cancels = queue.Queue(), {}

    def reader():
        for line in sys.stdin:
            req = json.loads(line)
            if req["op"] == "cancel" and req["id"] in cancels:
                cancels[req["id"]].set()
            elif req["op"] == "generate":
                cancels[req["id"]] = threading.Event()
                jobs.put(req)
            elif req["op"] in ("save", "restore", "forget"):
                jobs.put(req)
            elif req["op"] == "shutdown":
                break
        jobs.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while (req := jobs.get()) is not None:
        if req["op"] == "save":
            save(req, mode)
            continue
        if req["op"] == "restore":
            restore(req)
            continue
        if req["op"] == "forget":
            if req.get("chat") == kept["chat"]:
                kept.update(chat=None, tokens=0)
            continue
        rid, cancel = req["id"], cancels[req["id"]]
        if req.get("tools"):
            tool_answer(req, cancel)
            continue
        text = last_text(req["messages"])
        if req.get("schema") and "SLOW" not in text:
            content = json.dumps(
                {"text": "improved: " + text[-20:], "notes": "added detail"}
            )
            emit({"id": rid, "event": "content", "text": content})
            emit(
                {
                    "id": rid,
                    "event": "done",
                    "finish": "stop",
                    "reasoning": "",
                    "content": content,
                    "stats": {"images": len(req["images"])},
                }
            )
            continue
        reasoning = "thinking it over" if req.get("thinking") else ""
        if reasoning:
            emit({"id": rid, "event": "reasoning", "text": reasoning})
        parts, finish = [], "stop"
        # a text item of a STEPS: script (the eval's general chats); an item with calls stays "Hello world"
        script = scripted(req["messages"])
        said = (
            script[0]
            if script is not None
            and isinstance(script[0], list)
            and not any("<tool_call>" in c for c in script[0])
            else ["Hello", " world"]
        )
        words = ticks() if "SLOW" in text else iter(said)
        for word in words:
            if cancel.is_set():
                finish = "cancelled"
                break
            parts.append(word)
            emit({"id": rid, "event": "content", "text": word})
            if "SLOW" in text:
                time.sleep(0.02)
        emit(
            {
                "id": rid,
                "event": "done",
                "finish": finish,
                "reasoning": reasoning,
                "content": "".join(parts),
                "stats": {
                    "turns": len(req["messages"]),
                    "images": len(req["images"]),
                    "seen": {
                        **{k: req.get(k) for k in LIMITS},
                        **({"draft": req["draft"]} if req.get("draft") else {}),
                    },
                    "prefix": prefix(req),
                    **use_cache(req, finish),
                },
            }
        )


if __name__ == "__main__":
    main()
