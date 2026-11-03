"""The worker's token bans and the recovery of a call written in the reasoning (app/llm_worker.py), without MLX.

A fake byte-level BPE vocabulary stands in for Qwen's tokenizer, and a small list-based array module stands in for
mlx.core, so the logits processors run here as they do in the worker.
"""

import io

import pytest

from app import llm, llm_worker
from app.llm_worker import (
    BanIds,
    ByteBan,
    NoEmoji,
    ToolText,
    bans,
    byte_decoder,
    finish_tools,
    token_pieces,
    trailing_calls,
)
from tests.fake_llm_worker import parse

ENCODE = {
    b: c for c, b in byte_decoder().items()
}  # byte -> the BPE alphabet's character


def bpe(data: bytes) -> str:
    """A token's text as a byte-level BPE vocabulary writes it."""
    return "".join(ENCODE[b] for b in data)


# the fake vocabulary: id -> bytes; emoji come whole, split in two and split byte by byte
PIECES = [
    b"Hello",  # 0
    b" world",  # 1
    "\U0001f600".encode(),  # 2: a whole emoji (grinning face)
    b" \xf0\x9f",  # 3: the start of any U+1Fxxx: can only become an emoji
    b"\x98\x80",  # 4: its end (U+1F600 after 3)
    b"\xf0",  # 5: a lead byte alone: emoji or another plane
    b"\x9f",  # 6
    b"\xa0\x80\x80",  # 7: after 5 this is U+20000 (a CJK character)
    "✓".encode(),  # 8: a check mark
    "♯".encode(),  # 9: the music sharp sign stays allowed
    "→".encode(),  # 10: an arrow stays allowed
    "çok".encode(),  # 11: Turkish text
    b"\xe2",  # 12: the lead byte of U+2000-2FFF
    b"\x9c\x93",  # 13: after 12 this is U+2713, the check mark
    b"\x86\x92",  # 14: after 12 this is U+2192, the arrow
    b"\xe2\x99",  # 15: U+2640-267F: the gender signs (banned) and the music signs (allowed)
    b"\xaf",  # 16: after 15 this is U+266F, the sharp sign
    b"\x80",  # 17: after 15 this is U+2640, banned
    "️".encode(),  # 18: the emoji variation selector
    "中文".encode(),  # 19: Chinese text
]
VOCAB = {bpe(p): i for i, p in enumerate(PIECES)} | {"<tool_call>": 20}
NEG = -float("inf")


class Lists:
    """The part of mlx.core that the processors use, over Python lists (one sequence: 1-D logits)."""

    @staticmethod
    def array(value):
        return value

    @staticmethod
    def take(a, index, axis=None):
        return a[index]

    @staticmethod
    def where(mask, x, y):
        return [x if m else v for m, v in zip(mask, y, strict=True)]


class Logits(list):
    @property
    def shape(self):
        return (len(self),)


@pytest.fixture(scope="module")
def table() -> ByteBan:
    return ByteBan(token_pieces(VOCAB))


def run(table: ByteBan, ids: list[int], width: int = 24) -> list[set[int]]:
    """The banned ids before each token of `ids` is sampled, and after the last one."""
    proc = NoEmoji(Lists, table)
    tokens, out = [99], []  # the prompt
    for i in [None, *ids]:
        if i is not None:
            tokens.append(i)
        logits = proc(tokens, Logits([0.0] * width))
        out.append({t for t, v in enumerate(logits) if v == NEG})
    return out


# ---------- the vocabulary ----------


def test_token_pieces_reads_the_byte_level_alphabet():
    pieces = token_pieces(VOCAB)
    assert pieces[:20] == PIECES and pieces[20] == b"<tool_call>"
    assert byte_decoder()["Ġ"] == 0x20  # "Ġ" is the space


# ---------- emoji ----------


def test_whole_and_unambiguous_emoji_tokens_are_banned_at_any_time(table):
    first = run(table, [])[0]
    assert {2, 3, 8, 18} <= first
    assert not first & {0, 1, 9, 10, 11, 19}


def test_a_lead_byte_alone_is_allowed_and_the_ban_follows_it(table):
    after_f0 = run(table, [5])[1]
    assert 6 in after_f0  # F0 9F can only become an emoji
    assert 7 not in after_f0  # F0 A0 80 80 is a CJK character
    after_e2 = run(table, [12])[1]
    assert 13 in after_e2 and 14 not in after_e2  # the check mark, not the arrow


def test_a_state_carries_over_two_tokens(table):
    after = run(table, [15])[1]  # E2 99: the next byte decides
    assert 17 in after and 16 not in after
    assert (
        run(table, [15, 16])[2] == run(table, [])[0]
    )  # the sharp sign ends the character


def test_stray_continuation_bytes_do_not_confuse_the_state(table):
    # E2 then an ASCII token: the character is broken, so the next token starts afresh
    assert run(table, [12, 0])[2] == run(table, [])[0]


def test_the_tables_are_padded_to_the_model_vocabulary(table):
    width = 40  # the model's embedding has more rows than the tokenizer has tokens
    banned = run(table, [12], width)
    assert len(table.tables(Lists, width)[1][0]) == width
    assert 13 in banned[1] and not any(t >= 21 for t in banned[1])


def test_bans_follow_the_request():
    table = ByteBan(token_pieces(VOCAB))
    req = {"tools": [{"type": "function"}]}
    kinds = [type(p) for p in bans(Lists, req, table, VOCAB, "<tool_call>")]
    assert kinds == [NoEmoji]  # every request bans emoji
    assert bans(Lists, {"allow_emoji": True}, table, VOCAB, "<tool_call>") == []
    none = bans(Lists, {**req, "tool_choice": "none"}, table, VOCAB, "<tool_call>")
    assert [type(p) for p in none] == [NoEmoji, BanIds]
    logits = none[1]([1], Logits([0.0] * 24))
    assert [t for t, v in enumerate(logits) if v == NEG] == [20]
    # without tools there is no call to ban; a marker of several tokens cannot be banned
    assert len(bans(Lists, {"tool_choice": "none"}, table, VOCAB, "<tool_call>")) == 1
    assert len(bans(Lists, {**req, "tool_choice": "none"}, table, VOCAB, "<x>")) == 1


def test_generate_checks_tool_choice():
    with pytest.raises(ValueError, match="tool_choice"):
        list(llm.generate([{"role": "user", "content": "hi"}], tool_choice="auto"))


# ---------- a call in the reasoning ----------

CALL = "<tool_call>\n<function=get_weather>\n<parameter=city>\nAnkara\n</parameter>\n</function>\n</tool_call>"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        },
    }
]


def finish(content: str, reasoning: str, finish: str = "stop") -> dict:
    calls = ToolText()
    calls.feed(content)
    done = {"id": "r1", "finish": finish, "content": content, "reasoning": reasoning}
    # finish_tools may emit a last content delta
    with pytest.MonkeyPatch.context() as m:
        m.setattr(llm_worker, "proto", io.StringIO())
        finish_tools(done, calls, parse, TOOLS)
    return done


def test_trailing_calls_takes_only_complete_blocks_at_the_end():
    assert trailing_calls(f"I will check.\n\n{CALL}\n{CALL}\n") == (
        "I will check.",
        [(CALL[11:-12], True)] * 2,
    )
    assert trailing_calls(f"{CALL}\nthen I answer.") == (
        f"{CALL}\nthen I answer.",
        [],
    )
    assert trailing_calls("no call </tool_call>")[1] == []


def test_a_call_at_the_end_of_the_reasoning_is_recovered():
    done = finish("", f"The user wants the weather.\n{CALL}")
    assert done["finish"] == "tool_calls" and done["reasoning"] == (
        "The user wants the weather."
    )
    assert [(c["name"], c["arguments"]) for c in done["tool_calls"]] == [
        ("get_weather", {"city": "Ankara"})
    ]


def test_calls_in_the_content_win_and_a_cancelled_answer_gets_none():
    done = finish(CALL.replace("Ankara", "Izmir"), f"thinking {CALL}")
    assert [c["arguments"]["city"] for c in done["tool_calls"]] == ["Izmir"]
    assert done["reasoning"] == f"thinking {CALL}"
    done = finish("", f"thinking {CALL}", "cancelled")
    assert done["tool_calls"] == [] and done["finish"] == "cancelled"


def test_a_cut_call_in_the_reasoning_stays_there():
    done = finish("Hello", f"thinking {CALL[:-5]}")
    assert done["tool_calls"] == [] and done["finish"] == "stop"
