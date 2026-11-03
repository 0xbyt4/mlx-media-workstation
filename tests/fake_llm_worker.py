"""A stand-in for app/llm_worker.py that speaks its JSON-line protocol without a model (tests only).

The model path argument picks the behaviour: "fail" reports a load error, "crash" exits after loading. A user message
containing SLOW streams tokens until cancelled (also with a schema); any other request with a schema answers JSON.

With tools, the fake streams scripted tool-call text through the real worker's hold-back and parsing (ToolText,
finish_tools), with a small stand-in for mlx-vlm's qwen3_coder parser. The last message picks the script (see
SCRIPTS); a tool result as the last message gets the answer "Result: <content>". The done stats then echo what the
request carried ("seen"), so a test can check what reached the worker.
"""

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
PARAM = re.compile(r"<parameter=(.*?)>\n?(.*?)\n?</parameter>", re.DOTALL)


def emit(ev):
    llm_worker.emit(ev)


def last_text(messages):
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if p.get("type") == "text")


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
    for key, value in PARAM.findall(body):
        args[key] = int(value) if props.get(key, {}).get("type") == "integer" else value
    return {"name": name, "arguments": args}


def tool_answer(req, cancel):
    """A request with tools: stream the script through the worker's hold-back, then finish as the worker does."""
    rid, messages = req["id"], req["messages"]
    text = last_text(messages) if messages[-1]["role"] != "tool" else ""
    if messages[-1]["role"] == "tool":
        chunks = ["Result: ", messages[-1]["content"]]
    elif "SLOW" in text:
        chunks = iter(lambda: "tick ", None)
    else:
        chunks = next((v for k, v in SCRIPTS.items() if k in text), ["Hello", " world"])
    calls, finish = llm_worker.ToolText(), "length" if "CUT" in text else "stop"
    for chunk in chunks:
        if cancel.is_set():
            finish = "cancelled"
            break
        if shown := calls.feed(chunk):
            emit({"id": rid, "event": "content", "text": shown})
        if "SLOW" in text:
            time.sleep(0.02)
    seen = {k: req.get(k) for k in ("messages", "tools", "reasoning_effort", "chat")}
    done = {
        "id": rid,
        "event": "done",
        "finish": finish,
        "reasoning": "",
        "content": calls.text,
        "stats": {"turns": len(messages), "images": len(req["images"]), "seen": seen},
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
            elif req["op"] == "shutdown":
                break
        jobs.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while (req := jobs.get()) is not None:
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
        words = (
            iter(lambda: "tick ", None) if "SLOW" in text else iter(["Hello", " world"])
        )
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
                "stats": {"turns": len(req["messages"]), "images": len(req["images"])},
            }
        )


if __name__ == "__main__":
    main()
