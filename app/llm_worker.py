"""The local LLM worker: Qwen3.8 on mlx-vlm, in its own process, run by app/llm.py with the LLM environment's python.

It loads the model once and answers requests one at a time, as JSON lines: requests on stdin, events on stdout. A
"cancel" line stops the running answer between tokens; the parent kills the process to stop a long prompt read or to
free the memory, which only a process exit releases in full. Everything mlx-vlm prints goes to stderr, so stdout
carries only the protocol.

Requests: {"op": "generate", "id", "messages", "images", "thinking", "schema", "max_tokens", "chat", "tools",
"reasoning_effort"}, {"op": "cancel", "id"} and {"op": "shutdown"}. Events: {"event": "ready"}, then per request
"reasoning" and "content" deltas, and one "done" (with the full answer and its stats) or "error".

With "tools" (OpenAI-style function schemas), the content deltas stop at the first tool-call marker: the call text is
held back, parsed with mlx-vlm's parser for the model at the end, and the "done" event carries it as "tool_calls"
(see ToolText and parse_calls; tests/fake_llm_worker.py uses them too, so this module imports nothing from MLX at the
top level and has no side effects on import).
"""

import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
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

# Qwen3.8's tool-call markers (mlx-vlm's qwen3_coder parser); the worker takes them from the parser module
TOOL_START, TOOL_END = "<tool_call>", "</tool_call>"
FUNCTION = re.compile(r"<function=([^>\n]*)>")
proto: Any = None  # the protocol's stdout, set by main()
write_lock = threading.Lock()


def emit(event: dict[str, Any]) -> None:
    with write_lock:
        proto.write(json.dumps(event) + "\n")


class ToolText:
    """Splits a streamed answer into the text the user may see and the tool-call text, which is held back.

    feed() returns the new text that may be shown: everything before the first start marker, less an end that could
    be the start of a marker split across chunks. From the first marker on, nothing more is shown until finish().
    """

    def __init__(self, start: str = TOOL_START, end: str = TOOL_END) -> None:
        self.start, self.end = start, end
        self.text = ""  # the whole answer so far
        self.shown = 0  # length of the part returned by feed
        self.held = False  # a start marker was seen

    def feed(self, delta: str) -> str:
        self.text += delta
        if self.held:
            return ""
        at = self.text.find(self.start, self.shown)
        if at >= 0:
            self.held = True
        else:  # keep back the longest end of the text that begins a marker
            at = len(self.text)
            for n in range(min(len(self.start) - 1, at - self.shown), 0, -1):
                if self.start.startswith(self.text[-n:]):
                    at -= n
                    break
        out, self.shown = self.text[self.shown : at], at
        return out

    def finish(self) -> tuple[str, str, list[tuple[str, bool]]]:
        """(the rest to show, the whole visible answer, the call blocks as (text inside the markers, closed))."""
        if not self.held:
            return self.text[self.shown :], self.text, []
        blocks: list[tuple[str, bool]] = []
        outside: list[str] = []
        pos = self.shown
        while (at := self.text.find(self.start, pos)) >= 0:
            outside.append(self.text[pos:at])
            body = at + len(self.start)
            close = self.text.find(self.end, body)
            if close < 0:  # cut off, for example by max_tokens
                blocks.append((self.text[body:], False))
                pos = len(self.text)
                break
            blocks.append((self.text[body:close], True))
            pos = close + len(self.end)
        outside.append(self.text[pos:])
        before = self.text[: self.shown]
        # text between or after the calls: the template asks for none, so it is shown only at the end
        after = "".join(outside[1:]).strip()
        if not after:
            return "", before.rstrip(), blocks
        if not before.strip():
            return after, after, blocks
        return f"\n\n{after}", f"{before.rstrip()}\n\n{after}", blocks


def neutralize(
    messages: list[dict[str, Any]], specials: list[str]
) -> list[dict[str, Any]]:
    """The messages with every special-token string in their text broken by a word joiner (U+2060), so text from a
    web page, a file or a tool result cannot close its own block and speak as the user or the system. specials is
    the tokenizer's added tokens (<|im_start|>, </tool_response>, ...); the system message is the app's own."""

    def clean(text: Any) -> Any:
        if not isinstance(text, str) or "<" not in text:
            return text
        for token in specials:
            text = text.replace(token, token[0] + "\u2060" + token[1:])
        return text

    def deep(
        value: Any,
    ) -> Any:  # tool-call arguments: the template writes their strings raw
        if isinstance(value, dict):
            return {k: deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [deep(v) for v in value]
        return clean(value)

    out = []
    for msg in messages:
        if msg.get("role") != "system":
            msg = {
                **msg,
                "content": clean(msg.get("content")),
                "reasoning_content": clean(msg.get("reasoning_content")),
            }
            if msg["reasoning_content"] is None:
                del msg["reasoning_content"]
            if msg.get("tool_calls"):
                msg["tool_calls"] = [deep(call) for call in msg["tool_calls"]]
            if isinstance(msg["content"], list):
                msg["content"] = [
                    {**part, "text": clean(part["text"])} if "text" in part else part
                    for part in msg["content"]
                ]
        out.append(msg)
    return out


def extend_ids(
    prompt: str,
    cached: list[int] | None,
    encode: Callable[[str], list[int]],
    decode: Callable[[list[int]], str],
) -> list[int] | None:
    """The new prompt as the cached ids plus the ids of the new text, or None when the prompt does not start with the
    cached text. The model often samples a token split that a fresh tokenization of the same text does not give
    (" **Ç" + "ok" against " **" + "Çok"). Re-tokenized, the old answer then differs from the cache in one token, and
    Qwen3.8's linear-attention layers cannot rewind to that point, so the whole chat is read again. With the ids
    the model saw, the cache always fits."""
    if not cached:
        return None
    text = decode(cached)
    if not prompt.startswith(text) or len(prompt) == len(text):
        return None
    return cached + encode(prompt[len(text) :])


def parse_calls(
    blocks: list[tuple[str, bool]],
    parse: Callable[[str, Any], Any],
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The calls of an answer as {"id", "name", "arguments" (a dict)}; a call that does not parse gets an "error" (and
    its "raw" text) instead of failing the answer. parse is the parser module's parse_tool_call(text, tools)."""
    calls: list[dict[str, Any]] = []
    for raw, closed in blocks:
        found = FUNCTION.search(raw)
        name = found.group(1).strip() if found else ""
        try:
            if not closed:
                raise ValueError(f"the call has no {TOOL_END} (the answer was cut off)")
            parsed = parse(raw.strip(), tools)
            for call in parsed if isinstance(parsed, list) else [parsed]:
                args = call.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                if not isinstance(args, dict):
                    raise TypeError("the arguments are not an object")
                calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "name": str(call["name"]).strip(),
                        "arguments": args,
                    }
                )
        except Exception as e:  # noqa: BLE001 - the model's text is untrusted: any parse failure is reported
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "arguments": {},
                    "error": f"Invalid tool call: {type(e).__name__}: {e}",
                    "raw": raw,
                }
            )
    return calls


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
        # the tool-call parser module, loaded at the first request with tools
        self.parser: Any = None
        # the strings the tokenizer turns into special tokens; neutralize() breaks them in message text
        added = self.processor.tokenizer.added_tokens_decoder.values()
        self.specials = sorted(
            {t.content for t in added if len(t.content) > 1}, key=len, reverse=True
        )

    def tool_parser(self) -> Any:
        if self.parser is None:
            from mlx_vlm.tools import (
                _infer_tool_parser_from_processor,
                load_tool_module,
            )

            name = _infer_tool_parser_from_processor(self.processor)
            if name is None:
                raise ValueError(
                    "This model's chat template has no known tool-call format."
                )
            self.parser = load_tool_module(name)
        return self.parser

    def generate(self, req: dict[str, Any], cancel: threading.Event) -> None:
        rid, thinking = req["id"], bool(req.get("thinking"))
        images = req.get("images") or []
        tools = req.get("tools") or None
        options: dict[str, Any] = {}  # passed to the chat template only when set
        if tools:
            options["tools"] = tools
            parser = self.tool_parser()
            calls = ToolText(parser.tool_call_start, parser.tool_call_end)
        if req.get("reasoning_effort"):
            options["reasoning_effort"] = req["reasoning_effort"]
        prompt = self.template(
            self.processor,
            self.model.config,
            neutralize(req["messages"], self.specials),
            num_images=len(images),
            enable_thinking=thinking,
            **options,
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
        if (
            state is not None and not images
        ):  # an image's tokens come from the processor, not the tokenizer
            tok = self.processor.tokenizer
            ids = extend_ids(
                prompt,
                state.token_ids,
                lambda text: tok(text, add_special_tokens=False)["input_ids"],
                lambda ids: tok.decode(ids, skip_special_tokens=False),
            )
            if ids is not None:
                kwargs["input_ids"] = self.mx.array([ids])
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
                text = r.text if in_think or not tools else calls.feed(r.text)
                if text:
                    emit({"id": rid, "event": part, "text": text})
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
        done = {
            "id": rid,
            "event": "done",
            "finish": "cancelled"
            if cancelled
            else (last.finish_reason if last else "stop"),
            "reasoning": "".join(reasoning).strip(),
            "content": "".join(answer).lstrip("\n"),
            "stats": stats,
        }
        if tools:
            finish_tools(done, calls, parser.parse_tool_call, tools)
        emit(done)


def finish_tools(
    done: dict[str, Any],
    calls: ToolText,
    parse: Callable[[str, Any], Any],
    tools: list[dict[str, Any]],
) -> None:
    """Complete a "done" event of a request with tools: the rest of the visible text (as a last content delta), the
    content without call markup, and "tool_calls" ("finish": "tool_calls" when there are any). A cancelled answer
    gets no calls: nothing it started may run."""
    tail, visible, blocks = calls.finish()
    if tail and done["finish"] != "cancelled":
        emit({"id": done["id"], "event": "content", "text": tail})
    done["content"] = visible.lstrip("\n")
    done["tool_calls"] = (
        [] if done["finish"] == "cancelled" else parse_calls(blocks, parse, tools)
    )
    if done["tool_calls"]:
        done["finish"] = "tool_calls"


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
    global proto
    proto = os.fdopen(os.dup(1), "w", buffering=1)  # the protocol keeps the real stdout
    os.dup2(2, 1)  # anything else printed goes to stderr
    sys.stdout = sys.stderr
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
