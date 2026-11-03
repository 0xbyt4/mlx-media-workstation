"""The local LLM worker: Qwen3.8 on mlx-vlm, in its own process, run by app/llm.py with the LLM environment's python.

It loads the model once and answers requests one at a time, as JSON lines: requests on stdin, events on stdout. A
"cancel" line stops the running answer between tokens; the parent kills the process to stop a long prompt read or to
free the memory, which only a process exit releases in full. Everything mlx-vlm prints goes to stderr, so stdout
carries only the protocol.

Requests: {"op": "generate", "id", "messages", "images", "thinking", "schema", "max_tokens", "chat"},
{"op": "cancel", "id"} and {"op": "shutdown"}. Events: {"event": "ready"}, then per request "reasoning" and "content"
deltas, and one "done" (with the full answer and its stats) or "error".
"""

import json
import os
import queue
import sys
import threading
import time
from typing import Any

THINK_END = 248069  # the '</think>' token of Qwen3.5-3.8
MAX_PIXELS = (
    1024 * 1024
)  # a 4K frame costs 8,160 prompt tokens unscaled, about 1,000 at this size
# sampling settings from the Qwen3.8 model card, per mode; mlx-vlm applies the presence penalty to the last 20 tokens
THINKING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}
NO_THINKING = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
}

proto = os.fdopen(os.dup(1), "w", buffering=1)  # the protocol keeps the real stdout
os.dup2(2, 1)  # anything else printed goes to stderr
sys.stdout = sys.stderr
write_lock = threading.Lock()


def emit(event: dict[str, Any]) -> None:
    with write_lock:
        proto.write(json.dumps(event) + "\n")


class Engine:
    def __init__(self, model_dir: str) -> None:
        import mlx.core as mx
        from mlx_vlm import PromptCacheState, apply_chat_template, load, stream_generate
        from mlx_vlm.structured import build_json_schema_logits_processor

        self.mx, self.template, self.stream = mx, apply_chat_template, stream_generate
        self.cache_state, self.json_processor = (
            PromptCacheState,
            build_json_schema_logits_processor,
        )
        self.model, self.processor = load(model_dir)
        self.chat: str | None = (
            None  # the chat whose prompt cache is kept (one at a time)
        )
        self.state = PromptCacheState()

    def generate(self, req: dict[str, Any], cancel: threading.Event) -> None:
        rid, thinking = req["id"], bool(req.get("thinking"))
        images = req.get("images") or []
        prompt = self.template(
            self.processor,
            self.model.config,
            req["messages"],
            num_images=len(images),
            enable_thinking=thinking,
        )
        kwargs: dict[str, Any] = dict(THINKING if thinking else NO_THINKING)
        if req.get("schema") is not None:  # thinking is off for JSON answers
            kwargs["logits_processors"] = [
                self.json_processor(self.processor.tokenizer, req["schema"])
            ]
        state = None
        if req.get(
            "chat"
        ):  # reuse the chat's earlier turns when the new prompt extends them
            if req["chat"] != self.chat:
                self.chat, self.state = req["chat"], self.cache_state()
            state = self.state
        self.mx.reset_peak_memory()
        t0 = time.time()
        gen = self.stream(
            self.model,
            self.processor,
            prompt,
            image=images or None,
            max_tokens=int(req.get("max_tokens") or 4096),
            max_pixels=MAX_PIXELS,
            prompt_cache_state=state,
            **kwargs,
        )
        in_think, reasoning, answer, last, cancelled = thinking, [], [], None, False
        try:
            for r in gen:
                if cancel.is_set():
                    cancelled = True
                    break
                last = r
                if r.finish_reason is not None:
                    continue
                if in_think and r.token == THINK_END:
                    in_think = False
                    continue
                part = "reasoning" if in_think else "content"
                (reasoning if in_think else answer).append(r.text)
                if r.text:
                    emit({"id": rid, "event": part, "text": r.text})
        finally:
            gen.close()
            if cancelled:
                if state is not None:  # the cache was extended in place: drop it
                    self.chat, self.state = None, self.cache_state()
                self.mx.clear_cache()
        stats = {"seconds": round(time.time() - t0, 1)}
        if last is not None:
            stats.update(
                prompt_tokens=last.prompt_tokens,
                generation_tokens=last.generation_tokens,
                cached_tokens=last.cached_tokens,
                prompt_tps=round(last.prompt_tps, 1),
                generation_tps=round(last.generation_tps, 1),
                peak_gb=round(last.peak_memory, 1),
            )
        emit(
            {
                "id": rid,
                "event": "done",
                "finish": "cancelled"
                if cancelled
                else (last.finish_reason if last else "stop"),
                "reasoning": "".join(reasoning).strip(),
                "content": "".join(answer).lstrip("\n"),
                "stats": stats,
            }
        )


def read_requests(jobs: queue.Queue, cancels: dict[str, threading.Event]) -> None:
    """The stdin reader: generate requests go to the queue; cancel applies at once, between tokens."""
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = req.get("op")
        if op == "cancel":
            ev = cancels.get(req.get("id", ""))
            if ev is not None:
                ev.set()
        elif op == "generate":
            cancels[req["id"]] = threading.Event()
            jobs.put(req)
        elif op == "shutdown":
            break
    jobs.put(None)  # stdin closed or shutdown: stop after the current answer


def main() -> None:
    try:
        engine = Engine(sys.argv[1])
    except Exception as e:  # noqa: BLE001 - the parent shows this to the user
        emit({"event": "fatal", "message": f"Could not load the model: {e}"})
        sys.exit(1)
    emit({"event": "ready"})
    jobs: queue.Queue = queue.Queue()
    cancels: dict[str, threading.Event] = {}
    threading.Thread(target=read_requests, args=(jobs, cancels), daemon=True).start()
    # every MLX call stays on this thread: mlx-vlm's generation stream is thread-local
    while (req := jobs.get()) is not None:
        cancel = cancels[req["id"]]
        try:
            if not cancel.is_set():
                engine.generate(req, cancel)
            else:
                emit(
                    {
                        "id": req["id"],
                        "event": "done",
                        "finish": "cancelled",
                        "reasoning": "",
                        "content": "",
                        "stats": {},
                    }
                )
        except Exception as e:  # noqa: BLE001 - one failed request must not end the worker
            emit(
                {
                    "id": req["id"],
                    "event": "error",
                    "message": f"{type(e).__name__}: {e}",
                }
            )
        finally:
            cancels.pop(req["id"], None)


if __name__ == "__main__":
    main()
