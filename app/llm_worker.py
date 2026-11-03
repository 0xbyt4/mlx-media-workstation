"""The local LLM worker: Qwen3.8 on mlx-vlm, in its own process, run by app/llm.py with the LLM environment's python.

It loads the model once and answers requests one at a time, as JSON lines: requests on stdin, events on stdout. A
"cancel" line stops the running answer between tokens; the parent kills the process to stop a long prompt read or to
free the memory, which only a process exit releases in full. Everything mlx-vlm prints goes to stderr, so stdout
carries only the protocol.

Requests: {"op": "generate", "id", "messages", "images", "thinking", "schema", "max_tokens", "chat", "tools",
"tool_choice", "reasoning_effort", "allow_emoji"}, {"op": "cancel", "id"} and {"op": "shutdown"}. Events: {"event": "ready"}, then per request
"reasoning" and "content" deltas, and one "done" (with the full answer and its stats) or "error".

With "tools" (OpenAI-style function schemas), the content deltas stop at the first tool-call marker: the call text is
held back, parsed with mlx-vlm's parser for the model at the end, and the "done" event carries it as "tool_calls"
(see ToolText and parse_calls; tests/fake_llm_worker.py uses them too, so this module imports nothing from MLX at the
top level and has no side effects on import).

Every request bans emoji with a logits processor (ByteBan, NoEmoji), unless it has "allow_emoji": true: the prompt
rule alone did not stop them. "tool_choice": "none" also bans the call marker, so the model answers with text while
the tools stay in the prompt.
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


def trailing_calls(
    text: str, start: str = TOOL_START, end: str = TOOL_END
) -> tuple[str, list[tuple[str, bool]]]:
    """The complete call blocks at the end of a text, as (the text before them, blocks as in ToolText.finish). Qwen
    sometimes writes its call inside its reasoning; finish_tools then takes it from there."""
    rest, blocks = text.rstrip(), []
    while rest.endswith(end):
        at = rest.rfind(start, 0, len(rest) - len(end))
        if at < 0:
            break
        blocks.insert(0, (rest[at + len(start) : len(rest) - len(end)], True))
        rest = rest[:at].rstrip()
    return rest, blocks


# ---------- banned tokens: emoji, and the call marker for tool_choice "none" ----------

# Code points that are emoji: the Misc Symbols and Dingbats blocks (but the music signs U+2669-266F, as in "C♯"), the
# emoji among the technical and geometric symbols, the keycap mark, the emoji variation selector, the tags of flag
# sequences and everything from U+1F000 to U+1FFFF. Arrows, box drawing, math, (c) and TM stay allowed.
EMOJI = (
    (0x20E3, 0x20E3),
    (0x231A, 0x231B),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x25FB, 0x25FE),
    (0x2600, 0x2668),
    (0x2670, 0x27BF),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0xFE0F, 0xFE0F),
    (0x1F000, 0x1FFFF),
    (0xE0000, 0xE007F),
)


def byte_decoder() -> dict[str, int]:
    """The byte-level BPE alphabet (GPT-2's bytes_to_unicode) read backwards: token character -> byte."""
    keep = [*range(0x21, 0x7F), *range(0xA1, 0xAD), *range(0xAE, 0x100)]
    chars, n = list(keep), 0
    for b in range(256):
        if b not in keep:
            keep.append(b)
            chars.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(keep, chars, strict=True)}


def token_pieces(vocab: dict[str, int]) -> list[bytes]:
    """Each token id's bytes, from a byte-level BPE vocabulary (token text -> id). A token outside that alphabet
    (an added token such as <|im_start|>) counts as its UTF-8 text."""
    table = byte_decoder()
    pieces = [b""] * (max(vocab.values()) + 1)
    for text, i in vocab.items():
        if all(c in table for c in text):
            pieces[i] = bytes(table[c] for c in text)
        else:
            pieces[i] = text.encode()
    return pieces


def _size(lead: int) -> int:
    """The length of the UTF-8 sequence that this byte starts; 0 for a byte that cannot start one."""
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    return 4 if 0xF0 <= lead <= 0xF4 else 0


def _span(prefix: bytes) -> tuple[int, int]:
    """The code points that the bytes of an unfinished character can still become (lowest, highest). An overlong
    start gives low > high: it can become nothing."""
    size = _size(prefix[0])
    low, high = (prefix[0] & (0x7F >> size),) * 2
    for k in range(1, size):
        low = low << 6 | (prefix[k] & 0x3F if k < len(prefix) else 0)
        high = high << 6 | (prefix[k] & 0x3F if k < len(prefix) else 0x3F)
    return max(low, (0x80, 0x800, 0x10000)[size - 2]), high


def _scan(data: bytes) -> tuple[bool, bytes]:
    """(whether the bytes hold an emoji, the unfinished character at their end). Broken sequences are skipped."""
    i, n = 0, len(data)
    while i < n:
        size = _size(data[i]) if data[i] >= 0x80 else 1
        j = i + 1
        while j < n and j < i + size and 0x80 <= data[j] <= 0xBF:
            j += 1
        if size > 1 and j == i + size:
            try:
                point = ord(data[i:j].decode())
            except UnicodeDecodeError:  # an overlong form or a surrogate
                point = -1
            if any(lo <= point <= hi for lo, hi in EMOJI):
                return True, b""
        elif size > 1 and j == n:
            return False, data[i:]
        i = j
    return False, b""


class ByteBan:
    """Which tokens would write an emoji, built once from the vocabulary's bytes (token_pieces).

    Byte-level BPE can split one emoji over two tokens ("\\xf0\\x9f" + "\\x98\\x80"). So the banned set
    depends on the unfinished character at the end of the text so far: the state. State 0 is "nothing that can still
    become an emoji"; each other state is such an unfinished character. A token is banned in a state when the state's
    bytes plus the token's hold an emoji, or end in an unfinished character that can only become one. Only tokens
    that start with a continuation byte can finish the state's character, so the other states differ from state 0
    only for those tokens.

    nxt[s][t] is the state after token t in state s, ban[s][t] whether t is banned there."""

    def __init__(self, pieces: list[bytes]) -> None:
        self.width = len(pieces)
        self.states: list[bytes] = [b""]
        index = {b"": 0}

        def outcome(data: bytes) -> tuple[bool, int]:
            hit, tail = _scan(data)
            if hit:
                return True, 0
            if not tail:
                return False, 0
            low, high = _span(tail)
            if low > high:
                return False, 0
            if any(lo <= low and high <= hi for lo, hi in EMOJI):
                return True, 0
            if not any(lo <= high and low <= hi for lo, hi in EMOJI):
                return False, 0
            if tail not in index:
                index[tail] = len(self.states)
                self.states.append(tail)
            return False, index[tail]

        base = [(False, 0)] * self.width
        for t, piece in enumerate(pieces):
            if not piece.isascii():
                base[t] = outcome(piece)
        finishers = [t for t, p in enumerate(pieces) if p and 0x80 <= p[0] <= 0xBF]
        self.nxt = [[o[1] for o in base]]
        self.ban = [[o[0] for o in base]]
        s = 1
        while s < len(self.states):  # outcome() may add states while this runs
            nxt, ban = list(self.nxt[0]), list(self.ban[0])
            for t in finishers:
                ban[t], nxt[t] = outcome(self.states[s] + pieces[t])
            self.nxt.append(nxt)
            self.ban.append(ban)
            s += 1
        self.arrays: dict[int, tuple[Any, Any]] = {}

    def tables(self, xp: Any, width: int) -> tuple[Any, Any]:
        """(nxt flattened, ban) as arrays of the module xp, padded to the model's vocabulary width; made once."""
        if width not in self.arrays:
            pad = max(0, width - self.width)
            nxt = [n for row in self.nxt for n in row + [0] * pad]
            ban = [row + [False] * pad for row in self.ban]
            self.arrays[width] = (xp.array(nxt), xp.array(ban))
        return self.arrays[width]


class NoEmoji:
    """A logits processor (one per request) that keeps a ByteBan's state and bans its tokens. The state stays an
    array of xp (mlx.core in the worker), so a step never waits for the token before it."""

    def __init__(self, xp: Any, ban: ByteBan) -> None:
        self.xp, self.bans = xp, ban
        self.state: Any = None

    def __call__(self, tokens: Any, logits: Any) -> Any:
        xp = self.xp
        width = logits.shape[-1]
        nxt, ban = self.bans.tables(xp, width)
        # the first call sees the prompt, which ends with a whole character
        if self.state is None:
            self.state = xp.array(0)
        else:
            self.state = xp.take(nxt, self.state * width + tokens[-1])
        return xp.where(xp.take(ban, self.state, axis=0), -float("inf"), logits)


class BanIds:
    """A logits processor that bans fixed token ids (the call marker for tool_choice "none")."""

    def __init__(self, xp: Any, ids: list[int]) -> None:
        self.xp, self.ids, self.mask = xp, ids, None

    def __call__(self, tokens: Any, logits: Any) -> Any:
        if self.mask is None:
            row = [False] * logits.shape[-1]
            for i in self.ids:
                row[i] = True
            self.mask = self.xp.array(row)
        return self.xp.where(self.mask, -float("inf"), logits)


def bans(
    xp: Any, req: dict[str, Any], emoji: ByteBan, vocab: dict[str, int], marker: str
) -> list[Any]:
    """The logits processors that ban tokens for a request: emoji unless "allow_emoji" is true, and the tool-call
    marker with "tool_choice": "none" (the marker must be one token)."""
    out: list[Any] = [] if req.get("allow_emoji") else [NoEmoji(xp, emoji)]
    if req.get("tools") and req.get("tool_choice") == "none":
        if marker in vocab:
            out.append(BanIds(xp, [vocab[marker]]))
        else:
            print(f"tool_choice none: {marker!r} is not one token", file=sys.stderr)
    return out


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
        # the token bans of every request (bans(): emoji, and the call marker for tool_choice "none")
        self.vocab = self.processor.tokenizer.get_vocab()
        self.no_emoji = ByteBan(token_pieces(self.vocab))

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
        marker = self.tool_parser().tool_call_start if tools else TOOL_START
        kwargs["logits_processors"] = kwargs.get("logits_processors", []) + bans(
            self.mx, req, self.no_emoji, self.vocab, marker
        )
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
    gets no calls: nothing it started may run. When the content has no call but the reasoning ends with complete
    call blocks (Qwen wrote its call while it thought), those are the calls, and the reasoning loses them."""
    tail, visible, blocks = calls.finish()
    if tail and done["finish"] != "cancelled":
        emit({"id": done["id"], "event": "content", "text": tail})
    done["content"] = visible.lstrip("\n")
    if not blocks and done["finish"] != "cancelled":
        rest, blocks = trailing_calls(done["reasoning"], calls.start, calls.end)
        if blocks:
            done["reasoning"] = rest
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
