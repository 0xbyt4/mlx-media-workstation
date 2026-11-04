"""app/runners/subtitles.py: reading SRT and WebVTT, building cues from timed words, writing SRT and WebVTT."""

import pytest

from app.runners import subtitles
from app.runners.subtitles import Cue


def test_parse_srt_with_bom_crlf_tags_and_a_blank_line_in_a_cue() -> None:
    data = (
        "﻿2\r\n00:00:04,000 --> 00:00:05,500\r\nSecond\r\n\r\n"
        '1\r\n00:00:01,200 --> 00:00:03,000 X1:10\r\n<font color="red">Merhaba</font> {\\an8}<i>dünya</i>\r\n'
        "\r\nmore of the same cue\r\n\r\n"
        "3\r\n0:00:06.5 --> 0:00:07,25\r\n\r\n"
    ).encode()
    cues = subtitles.parse(subtitles.decode(data))
    # sorted by start; the empty third cue is left out; <i> stays, other tags go
    assert cues == [
        Cue(1.2, 3.0, "Merhaba <i>dünya</i>\nmore of the same cue"),
        Cue(4.0, 5.5, "Second"),
    ]


def test_parse_webvtt_skips_the_header_notes_and_styles() -> None:
    text = (
        "WEBVTT - a title\n\nNOTE a comment\n\nSTYLE\n::cue { color: red }\n\n"
        "intro\n00:01.000 --> 00:02.500 align:start line:0\n<v Ana>Hi <c.loud>there</c></v>\n\n"
        "01:00:00.000 --> 01:00:01.000\n<00:00:00.500>Late &amp; done\n"
    )
    assert subtitles.parse(text) == [
        Cue(1.0, 2.5, "Hi there"),
        Cue(3600.0, 3601.0, "Late &amp; done"),
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("hello\n", "block 1 has no time line"),
        ("1\n00:00:01,000 -> 00:00:02,000\nx\n", "block 1 has no time line"),
        ("1\n00:00:01,000 --> soon\nx\n", "cannot read the time line"),
        ("1\n00:00:03,000 --> 00:00:02,000\nx\n", "block 1 ends before it starts"),
        ("WEBVTT\n\n", "has no cues"),
        ("1\n00:00:01,000 --> 00:00:02,000\n" + "x" * 1001 + "\n", "more than 1000"),
    ],
)
def test_parse_refuses_a_bad_file(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        subtitles.parse(text)


def test_decode_takes_utf8_and_utf16_and_refuses_other_encodings() -> None:
    assert subtitles.decode("İkinci".encode("utf-16")) == "İkinci"
    with pytest.raises(ValueError, match="not UTF-8"):
        subtitles.decode("İkinci satır".encode("cp1254"))
    with pytest.raises(ValueError, match="larger than 5 MB"):
        subtitles.decode(b"x" * (subtitles.MAX_BYTES + 1))


def test_srt_and_vtt_writers() -> None:
    cues = [Cue(0.0, 1.5, "a --> b"), Cue(3661.25, 3662.0, "Tom & <Jerry>")]
    assert subtitles.srt(cues) == (
        "1\n00:00:00,000 --> 00:00:01,500\na -> b\n\n"
        "2\n01:01:01,250 --> 01:01:02,000\nTom & <Jerry>\n\n"
    )
    assert subtitles.vtt(cues) == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\na -- &gt; b\n\n"
        "01:01:01.250 --> 01:01:02.000\nTom &amp; &lt;Jerry&gt;\n\n"
    )
    # a cue written as SRT reads back the same
    assert subtitles.parse(subtitles.srt([Cue(1.0, 2.0, "Tom &\n<i>Jerry</i>")])) == [
        Cue(1.0, 2.0, "Tom &\n<i>Jerry</i>")
    ]


def test_spread_shares_the_time_by_length() -> None:
    assert subtitles.spread(["a", "bbb"], 10.0, 16.0) == [
        ("a", 10.0, 12.0),
        ("bbb", 12.0, 16.0),
    ]


def words(text: str, start: float = 0.0, step: float = 0.3) -> list:
    return [
        (w, start + i * step, start + (i + 1) * step)
        for i, w in enumerate(text.split())
    ]


def test_build_starts_a_cue_after_a_sentence_and_at_a_pause() -> None:
    timed = words("Hello there. How are") + words("you?", start=3.0)
    cues = subtitles.build(timed)
    assert [c.text for c in cues] == ["Hello there.", "How are", "you?"]
    # a short cue stays up for 1 s, but not past the next cue
    assert (cues[0].start, cues[0].end) == (0.0, 0.6)
    assert (cues[1].start, cues[1].end) == pytest.approx((0.6, 1.6))
    assert (cues[2].start, cues[2].end) == pytest.approx((3.0, 4.0))


def test_build_keeps_a_cue_to_two_lines_and_six_seconds() -> None:
    long = " ".join(["word"] * 40)  # 199 characters, 12 s at 0.3 s a word
    cues = subtitles.build(words(long))
    fast = subtitles.build(words(long, step=0.1))  # 4 s: only the line limit cuts
    assert [c.text for c in fast[:1]] == [
        " ".join(["word"] * 8) + "\n" + " ".join(["word"] * 8)
    ]
    for c in cues:
        lines = c.text.split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)
        assert c.end - c.start <= 6.0
    assert " ".join(c.text.replace("\n", " ") for c in cues) == long
    # a word longer than two lines stays whole
    assert subtitles.build(words("x" * 100))[0].text == "x" * 100


def test_build_without_spaces_cuts_between_characters() -> None:
    # 16 wide characters a line: a cue ends at 32 characters, or at 6 s (30 characters at 0.2 s)
    timed = [(ch, i * 0.2, (i + 1) * 0.2) for i, ch in enumerate("你" * 50)]
    cues = subtitles.build(timed, sep="")
    assert [c.text for c in cues] == [
        "你" * 15 + "\n" + "你" * 15,
        "你" * 10 + "\n" + "你" * 10,
    ]
    fast = [(ch, i * 0.1, (i + 1) * 0.1) for i, ch in enumerate("你" * 40)]
    assert [len(c.text.replace("\n", "")) for c in subtitles.build(fast, sep="")] == [
        32,
        8,
    ]
