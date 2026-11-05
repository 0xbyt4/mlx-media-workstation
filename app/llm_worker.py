"""The local LLM worker: Qwen3.8 on mlx-vlm, in its own process, run by app/llm.py with the LLM environment's python.

It loads the model once and answers requests one at a time, as JSON lines: requests on stdin, events on stdout. A
"cancel" line stops the running answer between tokens; the parent kills the process to stop a long prompt read or to
free the memory, which only a process exit releases in full. Everything mlx-vlm prints goes to stderr, so stdout
carries only the protocol.

Requests: {"op": "generate", "id", "messages", "images", "thinking", "schema", "max_tokens", "chat", "tools",
"tool_choice", "reasoning_effort", "thinking_budget", "allow_emoji"}, {"op": "cancel", "id"}, {"op": "save", "id",
"path"}, {"op": "restore", "id", "path", "chat"}, {"op": "forget", "chat"} and {"op": "shutdown"}.
Events: {"event": "ready"}, then per request "reasoning" and "content" deltas, and one "done" (with the full answer
and its stats) or "error". A save ends with {"event": "saved", "chat", ...} or "error", a restore with
{"event": "restored", "chat", "tokens", "seconds"} or "error".

"save" and "restore" keep the chat's prompt cache over a stop for a media job (Engine.save, Engine.restore): the
parent asks for a save before it stops the worker, and the next worker restores the file, so the first answer after
the job does not read the whole chat again. The file holds the model's tag (model_tag), the MLX versions and the exact
token ids; a cache that does not match is never used, and a restore deletes the file whether it worked or not.

thinking_budget uses mlx-vlm's ThinkingBudgetCriteria: after that many thinking tokens it forces "\n</think>", so the
model must answer. It counts from the "<think>" that the template's generation prompt opens; mlx-vlm looks for that
token in the prompt part it still reads, which always holds the new generation prompt here (Qwen3.8's cache cannot be
cut back, so a reused cache is always a whole earlier prompt and answer). The done stats give "thinking_tokens".

With "tools" (OpenAI-style function schemas), the content deltas stop at the first tool-call marker: the call text is
held back, parsed with mlx-vlm's parser for the model at the end, and the "done" event carries it as "tool_calls"
(see ToolText and parse_calls; tests/fake_llm_worker.py uses them too, so this module imports nothing from MLX at the
top level and has no side effects on import).

Every request bans emoji with a logits processor (ByteBan, NoEmoji), unless it has "allow_emoji": true: the prompt
rule alone did not stop them. Inside a tool-call block (the arguments) nothing is banned. "tool_choice": "none" also
bans the call marker, so the model answers with text while the tools stay in the prompt.
"""

import ast
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
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
# one parameter of a call's text, as the qwen3_coder parser reads it: a closed block, or an unclosed last one
PARAMETER = re.compile(r"<parameter=([^>\n]*)>(.*?)(</parameter>|\Z)", re.DOTALL)
# a size such as 1344x768: models write it unquoted inside a JSON object, where it is never valid JSON
SIZE = re.compile(r"\d+[xX]\d+")
JSON_TYPES = (
    "object",
    "array",
)  # the parameter types whose value the parser reads as JSON
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


def raw_params(raw: str) -> dict[str, str]:
    """The parameters of a call's text as the model wrote them (name -> text), as the parser splits them."""
    out = {}
    for name, value, closer in PARAMETER.findall(raw.rsplit("</function>", 1)[0]):
        value = value.removeprefix("\n")
        # an unclosed last parameter runs to the end of the block
        out[name.strip()] = value.removesuffix("\n") if closer else value.rstrip()
    return out


def properties(raw: str, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The parameter schemas of the tool that a call's text names."""
    found = FUNCTION.search(raw)
    name = found.group(1).strip() if found else ""
    for t in tools or []:
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return (fn.get("parameters") or {}).get("properties") or {}
    return {}


def quote_sizes(text: str) -> str:
    """JSON text with each bare size token outside a string ("size": 1344x768) put in double quotes. Such a token is
    never valid JSON or a Python literal, so no text that parses changes meaning."""
    out, i, in_string = [], 0, False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            in_string = c != '"'
        elif c == '"':
            in_string = True
        elif (m := SIZE.match(text, i)) and not (
            i and (text[i - 1].isalnum() or text[i - 1] in "._")
        ):
            after = text[m.end() : m.end() + 1]
            if not (after.isalnum() or after in ("_", ".")):
                out.append(f'"{m.group()}"')
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def repair(raw: str, tools: list[dict[str, Any]] | None) -> str:
    """The call's text with bare sizes quoted in its object and array parameters (quote_sizes); text parameters stay
    as they are."""
    props = properties(raw, tools)

    def fix(m: re.Match[str]) -> str:
        if (props.get(m.group(1).strip()) or {}).get("type") not in JSON_TYPES:
            return m.group(0)
        return f"<parameter={m.group(1)}>{quote_sizes(m.group(2))}{m.group(3)}"

    return PARAMETER.sub(fix, raw)


def explain(raw: str, tools: list[dict[str, Any]] | None) -> str:
    """Which parameter of a call that did not parse is wrong, and where, so the model can write it again; "" when
    no single parameter is to blame."""
    props = properties(raw, tools)
    for name, value in raw_params(raw).items():
        kind = (props.get(name) or {}).get("type")
        if kind in JSON_TYPES:
            try:
                json.loads(value)
                continue
            except json.JSONDecodeError as e:
                why, near = e.msg, value[max(0, e.pos - 40) : e.pos + 20]
            try:
                ast.literal_eval(value)  # the parser takes a Python literal too
                continue
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
            return (
                f"the parameter {name} is not valid JSON ({why} at {near!r}). Write it as JSON, with every text "
                'value in double quotes, for example "size": "1344x768"'
            )
        if kind in ("integer", "number"):
            try:
                float(value)
            except ValueError:
                return f"the parameter {name} must be a number, not {value[:60]!r}"
    return ""


def parse_calls(
    blocks: list[tuple[str, bool]],
    parse: Callable[[str, Any], Any],
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The calls of an answer as {"id", "name", "arguments" (a dict)}; a call that does not parse gets an "error" (and
    its "raw" text) instead of failing the answer. parse is the parser module's parse_tool_call(text, tools). A call
    that fails only because of a bare size in an object parameter (repair) parses after the repair."""
    calls: list[dict[str, Any]] = []
    for raw, closed in blocks:
        found = FUNCTION.search(raw)
        name = found.group(1).strip() if found else ""
        try:
            if not closed:
                raise ValueError(f"the call has no {TOOL_END} (the answer was cut off)")
            # a failed parse is retried once with bare sizes quoted (repair), else reported below
            try:
                parsed = parse(raw.strip(), tools)
            except Exception:
                fixed = repair(raw, tools)
                if fixed == raw:
                    raise
                parsed = parse(fixed.strip(), tools)
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
                    "error": "Invalid tool call: "
                    + (explain(raw, tools) or f"{type(e).__name__}: {e}"),
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


def thought_calls(
    reasoning: str, answer: str, start: str = TOOL_START, end: str = TOOL_END
) -> tuple[str, list[tuple[str, bool]]]:
    """The calls that Qwen wrote in its reasoning, for an answer that has none, as (the reasoning without them, blocks
    as in ToolText.finish):

    - complete call blocks at the end of the reasoning (trailing_calls);
    - when the answer has no text either: every complete call block in the reasoning, each one once;
    - when the answer has no text and the reasoning ends inside a call (the thinking budget cut it): that call as an
      unclosed block, so it comes back to the model as a malformed call instead of an empty answer.

    An answer with text keeps a call in the middle of the reasoning there: it was a draft that the model did not
    send."""
    rest, blocks = trailing_calls(reasoning, start, end)
    if blocks or answer.strip():
        return (rest, blocks) if blocks else (reasoning, [])
    found = re.compile(re.escape(start) + r"(.*?)" + re.escape(end), re.DOTALL)
    seen: list[str] = []
    for body in found.findall(reasoning):
        if body.strip() and body not in seen:
            seen.append(body)
    if seen:
        return found.sub("", reasoning).strip(), [(body, True) for body in seen]
    at = reasoning.rfind(start)
    if at >= 0 and FUNCTION.search(reasoning, at):
        return reasoning[:at].rstrip(), [(reasoning[at + len(start) :], False)]
    return reasoning, []


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
    array of xp (mlx.core in the worker), so a step never waits for the token before it.

    calls: the ids of the tool-call start and end markers, or None. From a start marker to its end marker the model
    writes a call's arguments (a file's text, a command), where an emoji can be the data: nothing is banned there."""

    def __init__(
        self, xp: Any, ban: ByteBan, calls: tuple[int, int] | None = None
    ) -> None:
        self.xp, self.bans, self.calls = xp, ban, calls
        self.state: Any = None
        # whether the text is inside a call block (an array of xp)
        self.inside: Any = None

    def __call__(self, tokens: Any, logits: Any) -> Any:
        xp = self.xp
        width = logits.shape[-1]
        nxt, ban = self.bans.tables(xp, width)
        # the first call sees the prompt, which ends with a whole character and is outside a call
        if self.state is None:
            self.state, self.inside = xp.array(0), xp.array(False)
        else:
            self.state = xp.take(nxt, self.state * width + tokens[-1])
            if self.calls is not None:
                start, end = self.calls
                self.inside = xp.where(
                    tokens[-1] == start,
                    True,
                    xp.where(tokens[-1] == end, False, self.inside),
                )
        banned = xp.where(xp.take(ban, self.state, axis=0), -float("inf"), logits)
        return banned if self.calls is None else xp.where(self.inside, logits, banned)


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
    xp: Any,
    req: dict[str, Any],
    emoji: ByteBan,
    vocab: dict[str, int],
    marker: str,
    end: str = TOOL_END,
) -> list[Any]:
    """The logits processors that ban tokens for a request: emoji unless "allow_emoji" is true (but not inside a
    tool-call block, from `marker` to `end`, when the request has tools), and the tool-call marker with
    "tool_choice": "none". Each marker must be one token."""
    calls = None
    if req.get("tools"):
        if marker in vocab and end in vocab:
            calls = (vocab[marker], vocab[end])
        else:
            print(
                f"tool calls: {marker!r} or {end!r} is not one token", file=sys.stderr
            )
    out: list[Any] = [] if req.get("allow_emoji") else [NoEmoji(xp, emoji, calls)]
    if req.get("tools") and req.get("tool_choice") == "none":
        if marker in vocab:
            out.append(BanIds(xp, [vocab[marker]]))
        else:
            print(f"tool_choice none: {marker!r} is not one token", file=sys.stderr)
    return out


# ---------- the saved prompt cache (the parent and tests/fake_llm_worker.py use these too) ----------

CACHE_FORMAT = "1"
CACHE_HEADER_MAX = 1 << 20  # bytes; the header of a 32K-token cache is about 15 KB


def model_tag(model_dir: str | os.PathLike[str]) -> str:
    """The identity of a model folder: its full path and the name, size and change time of each file in it. A saved
    prompt cache carries the tag of its model; a cache with another tag is never used."""
    folder = Path(model_dir).expanduser().absolute()
    parts = [str(folder)]
    try:
        files = sorted(
            p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
        )
    except OSError:  # no such folder: the path alone
        files = []
    for p in files:
        st = p.stat()
        parts.append(f"{p.name} {st.st_size} {st.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def partial(path: Path) -> Path:
    """The file a save writes before it renames it to path (MLX adds .safetensors to a name without it)."""
    return path.with_name(f"{path.stem}.partial.safetensors")


def cache_info(path: Path) -> dict[str, Any] | None:
    """The metadata of a saved prompt cache ("format", "model", "runtime", "chat", "tokens", "layers"), read from
    the safetensors header without MLX. None when there is no file, or it is not a cache of this format."""
    try:
        with open(path, "rb") as f:
            size = int.from_bytes(f.read(8), "little")
            if not 0 < size <= CACHE_HEADER_MAX:
                return None
            header = json.loads(f.read(size))
    except (OSError, ValueError):
        return None
    meta = header.get("__metadata__") if isinstance(header, dict) else None
    if not isinstance(meta, dict) or meta.get("format") != CACHE_FORMAT:
        return None
    return meta


class Engine:
    def __init__(self, model_dir: str) -> None:
        import mlx.core as mx
        from mlx_vlm import PromptCacheState, apply_chat_template, load, stream_generate
        from mlx_vlm import __version__ as vlm_version
        from mlx_vlm.models.cache import ArraysCache, KVCache, make_prompt_cache
        from mlx_vlm.structured import build_json_schema_logits_processor

        self.mx, self.template, self.stream = mx, apply_chat_template, stream_generate
        self.cache_state, self.json_processor = (
            PromptCacheState,
            build_json_schema_logits_processor,
        )
        # the layer caches that save() and restore() know: the hybrid Qwen3.8 has KVCache (full attention) and
        # ArraysCache (the conv and recurrent state of its linear-attention layers); any other type is not saved
        self.kinds: dict[str, type] = {"KVCache": KVCache, "ArraysCache": ArraysCache}
        self.make_cache = make_prompt_cache
        self.tag = model_tag(model_dir)
        self.runtime = f"mlx {mx.__version__}, mlx-vlm {vlm_version}"
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

    def unsaved(self) -> str | None:
        """Why the kept prompt cache cannot be saved, or None. Only a whole cache of known layer types is saved: each
        full-attention layer holds exactly the cached tokens (mlx-vlm feeds every generated token before it stops)."""
        state = self.state
        if self.chat is None or state.cache is None or not state.token_ids:
            return "no chat cache"
        n = len(state.token_ids)
        for c in state.cache:
            kind = type(c).__name__
            if self.kinds.get(kind) is not type(c):
                return f"unknown layer cache {kind}"
            if kind == "KVCache" and (c.keys is None or c.offset != n):
                return "a layer does not hold the cached tokens"
            if kind == "ArraysCache" and (
                c.left_padding is not None or c.lengths is not None
            ):
                return "a batch cache"
        return None

    def save(self, req: dict[str, Any]) -> None:
        """Write the kept chat's prompt cache to req["path"]: a safetensors file with the layer states, the token ids
        and the tags that restore() checks. It is written to partial(path) first and renamed, so a stopped save never
        leaves a file that looks whole."""
        rid, path = req["id"], Path(req["path"])
        reason = self.unsaved()
        if reason is not None:
            print(f"prompt cache: not saved: {reason}", file=sys.stderr)
            emit({"id": rid, "event": "saved", "chat": None, "note": reason})
            return
        mx, state, t0 = self.mx, self.state, time.time()
        arrays = {"token_ids": mx.array(state.token_ids, dtype=mx.int32)}
        for i, c in enumerate(state.cache):
            if type(c).__name__ == "KVCache" and c.offset != c.keys.shape[2]:
                # cut the free room off one layer at a time: saving the slices would copy every layer at once, and
                # the media job waits for that memory (+0.54 GB at 9K tokens, measured with arrays of these shapes)
                c.keys = mx.contiguous(c.keys[..., : c.offset, :])
                c.values = mx.contiguous(c.values[..., : c.offset, :])
                mx.eval(c.keys, c.values)
                mx.clear_cache()
            for j, a in enumerate(c.state):
                if a is not None:
                    arrays[f"{i}.{j}"] = a
        meta = {
            "format": CACHE_FORMAT,
            "model": self.tag,
            "runtime": self.runtime,
            "chat": self.chat,
            "tokens": str(len(state.token_ids)),
            "layers": ",".join(type(c).__name__ for c in state.cache),
        }
        tmp = partial(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mx.save_safetensors(str(tmp), arrays, meta)
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        size, seconds = path.stat().st_size, round(time.time() - t0, 2)
        print(
            f"prompt cache: saved chat {self.chat}: {meta['tokens']} tokens, {size / 1e9:.2f} GB in {seconds} s",
            file=sys.stderr,
        )
        emit(
            {
                "id": rid,
                "event": "saved",
                "chat": self.chat,
                "tokens": len(state.token_ids),
                "bytes": size,
                "seconds": seconds,
            }
        )

    def restore(self, req: dict[str, Any]) -> None:
        """Load a prompt cache that save() wrote, as the kept cache of its chat; extend_ids then reuses it as if this
        process had made it. Every check must pass, or the cache is not used. The file is deleted either way."""
        rid, path, t0 = req["id"], Path(req["path"]), time.time()
        mx = self.mx
        try:
            arrays, meta = mx.load(str(path), return_metadata=True)
            ids, caches = self.loaded(arrays, meta, req.get("chat"))
            mx.eval(list(arrays.values()))  # read it all before the file goes
        finally:
            path.unlink(missing_ok=True)
        state = self.cache_state()
        state.update(ids, caches)
        self.chat, self.state = meta["chat"], state
        seconds = round(time.time() - t0, 2)
        print(
            f"prompt cache: restored chat {self.chat}: {len(ids)} tokens in {seconds} s",
            file=sys.stderr,
        )
        emit(
            {
                "id": rid,
                "event": "restored",
                "chat": self.chat,
                "tokens": len(ids),
                "seconds": seconds,
            }
        )

    def loaded(
        self, arrays: dict[str, Any], meta: dict[str, str], chat: str | None
    ) -> tuple[list[int], list[Any]]:
        """(the token ids, the layer caches) of a loaded cache file; a ValueError when anything does not match this
        model, these MLX versions, the chat or the file's own token count."""
        for key, want in (
            ("format", CACHE_FORMAT),
            ("model", self.tag),
            ("runtime", self.runtime),
        ):
            if meta.get(key) != want:
                raise ValueError(f"the cache is of another {key}")
        if not meta.get("chat") or (chat is not None and meta["chat"] != chat):
            raise ValueError("the cache is of another chat")
        ids = arrays["token_ids"].tolist()
        if not ids or len(ids) != int(meta.get("tokens", -1)):
            raise ValueError("the token ids do not match the cache")
        caches = self.make_cache(self.model.language_model)
        if meta.get("layers", "").split(",") != [type(c).__name__ for c in caches]:
            raise ValueError("the layers do not match the model")
        used = 1  # token_ids
        for i, c in enumerate(caches):
            if type(c).__name__ == "KVCache":
                keys, values = arrays.get(f"{i}.0"), arrays.get(f"{i}.1")
                if keys is None or values is None or keys.shape[2] != len(ids):
                    raise ValueError(f"layer {i} does not hold the cached tokens")
                c.state = (keys, values)
                used += 2
            else:
                parts = [arrays.get(f"{i}.{j}") for j in range(len(c.cache))]
                c.state = parts
                used += sum(p is not None for p in parts)
        if used != len(arrays):
            raise ValueError("the file has arrays that the model has no place for")
        return ids, caches

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
        if thinking and req.get("thinking_budget"):
            # enable_thinking here is mlx-vlm's switch for the budget, not the template's
            kwargs.update(
                thinking_budget=int(req["thinking_budget"]), enable_thinking=True
            )
        if req.get("schema") is not None:  # thinking is off for JSON answers
            kwargs["logits_processors"] = [
                self.json_processor(self.processor.tokenizer, req["schema"])
            ]
        marker, end = (
            (parser.tool_call_start, parser.tool_call_end)
            if tools
            else (TOOL_START, TOOL_END)
        )
        kwargs["logits_processors"] = kwargs.get("logits_processors", []) + bans(
            self.mx, req, self.no_emoji, self.vocab, marker, end
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
        thought = 0  # tokens inside the thinking block
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
                thought += in_think
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
        if thinking:
            stats["thinking_tokens"] = thought
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
    gets no calls: nothing it started may run. When the content has no call, the calls that Qwen wrote while it
    thought are the calls (thought_calls), and the reasoning loses them."""
    tail, visible, blocks = calls.finish()
    if tail and done["finish"] != "cancelled":
        emit({"id": done["id"], "event": "content", "text": tail})
    done["content"] = visible.lstrip("\n")
    if not blocks and done["finish"] != "cancelled":
        rest, blocks = thought_calls(
            done["reasoning"], done["content"], calls.start, calls.end
        )
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
        elif op in (
            "save",
            "restore",
            "forget",
        ):  # in the queue: MLX runs on the main thread, in order
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
        if (
            req["op"] == "forget"
        ):  # a deleted chat: a later save must not write its tokens
            if req.get("chat") == engine.chat:
                engine.chat, engine.state = None, engine.cache_state()
            continue
        if req["op"] in ("save", "restore"):
            try:
                (engine.save if req["op"] == "save" else engine.restore)(req)
            except Exception as e:  # noqa: BLE001 - without the cache the chat is only read again
                print(f"prompt cache: {req['op']} failed: {e}", file=sys.stderr)
                emit(
                    {
                        "id": req["id"],
                        "event": "error",
                        "message": f"{type(e).__name__}: {e}",
                    }
                )
            continue
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
