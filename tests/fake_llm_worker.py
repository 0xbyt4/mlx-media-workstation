"""A stand-in for app/llm_worker.py that speaks its JSON-line protocol without a model (tests only).

The model path argument picks the behaviour: "fail" reports a load error, "crash" exits after loading. A user message
containing SLOW streams tokens until cancelled; a request with a schema answers JSON.
"""

import json
import queue
import sys
import threading
import time


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def last_text(messages):
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if p.get("type") == "text")


def main():
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
        text = last_text(req["messages"])
        if req.get("schema"):
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
