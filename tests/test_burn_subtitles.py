"""app/runners/burn_subtitles.py: the text's size and place, long subtitles in parts of two lines, the timeline and
its ffconcat list, and the PNGs that AppKit draws (on macOS with pyobjc)."""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.runners import burn_subtitles as burn
from app.runners.burn_subtitles import Layout


def test_layout_scales_the_font_with_the_short_side_and_keeps_the_margins() -> None:
    wide = burn.layout(1920, 1080)
    assert wide == Layout(1920, 1080, 59, 1698, 208, 811, 15, "bottom")
    # the text ends 7% of the height over the bottom edge (the band's pad is under it)
    assert 1080 - (wide.y + wide.band - wide.pad) == round(1080 * 0.07)
    # a Short: the same font as 1080p, 80% of the width, the text over the player's lowest fifth
    tall = burn.layout(1080, 1920)
    assert (tall.font, tall.text_width) == (59, 834)
    assert 1920 - (tall.y + tall.band - tall.pad) == round(1920 * 0.2)
    assert burn.layout(1080, 1920, place="top").y == round(1920 * 0.1) - tall.pad
    assert burn.layout(1920, 1080, "large").font == 76
    assert burn.layout(1280, 720, "small").font == 32
    # a tiny picture: the smallest font, a band inside the picture
    tiny = burn.layout(64, 48)
    assert tiny.font == burn.MIN_FONT and 0 <= tiny.y <= 48 - tiny.band
    with pytest.raises(ValueError, match="Unknown subtitle size"):
        burn.layout(640, 360, "huge")
    with pytest.raises(ValueError, match="Unknown subtitle size or place"):
        burn.layout(640, 360, place="left")


def test_plain_removes_the_tags_that_the_parser_keeps() -> None:
    assert burn.plain(" <i>İkinci</i> <b>satır</b> <u>x</u> ") == "İkinci satır x"
    assert burn.plain("a < b > c") == "a < b > c"


def wrap(width: int):
    """A stand-in for Painter.lines: one character is 1 px, lines break at spaces (greedy), the break's space stays
    at the end of its line."""

    def at(text: str, w: int) -> list[str]:
        lines, line = [], ""
        for word in text.split(" "):
            if line and len(line) + len(word) > w:
                lines.append(line)
                line = ""
            line += word + " "
        lines.append(line)
        lines[-1] = lines[-1][:-1]
        return lines

    def lines_of(text: str, count: int) -> list[str]:
        if not count:
            return at(text, width)
        w = width
        while w > 1 and len(at(text, w - 1)) <= count:
            w -= 1
        return at(text, w)

    return lines_of


def test_split_keeps_a_short_cue_and_shares_the_time_of_a_long_one() -> None:
    lines_of = wrap(20)
    assert "".join(lines_of("one two three four five six", 0)) == (
        "one two three four five six"
    )
    short = (1.0, 2.0, "two lines of\ntext")
    assert burn.split([short], lambda t, n: t.split("\n")) == [short]
    # 3 lines at 20 px: the text is wrapped again into 4 narrower lines, 2 for each part, so no part has one
    # short line; each part gets a share of the time by its characters
    text = "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj"
    assert len(lines_of(text, 0)) == 3
    parts = burn.split([(10.0, 20.0, text)], lines_of)
    assert [p[2] for p in parts] == [
        "aaaa bbbb cccc dddd eeee ffff",
        "gggg hhhh iiii jjjj",
    ]
    assert parts[0][:2] == (10.0, pytest.approx(10.0 + 10.0 * 29 / 48))
    assert parts[1][0] == parts[0][1] and parts[1][1] == 20.0
    # 5 lines give parts of 2, 2 and 1 when the lines cannot be evened out; the time follows the characters
    five = ["a" * 10 + " ", "b" * 10 + " ", "c" * 10 + " ", "d" * 10 + " ", "e" * 10]
    parts = burn.split([(0.0, 5.4, "".join(five))], lambda t, n: five)
    assert [p[2] for p in parts] == [
        "a" * 10 + " " + "b" * 10,
        "c" * 10 + " " + "d" * 10,
        "e" * 10,
    ]
    assert [round(p[1] - p[0], 3) for p in parts] == [
        2.181,
        2.181,
        1.038,
    ]  # 21, 21 and 10 characters
    assert parts[-1][1] == 5.4


def test_timeline_fills_the_gaps_and_cuts_an_overlap() -> None:
    parts = [
        (0.0, 1.0, "a"),
        (1.0, 2.5, "b"),  # starts when a ends: no gap between them
        (2.0, 3.0, "c"),  # starts before b ends: b stops at 2.0
        (3.5, 9.0, "d"),  # runs past the 5 s video
        (6.0, 7.0, "e"),  # after the video's end
    ]
    entries = burn.timeline(parts, 5.0)
    assert [(i, round(s, 6)) for i, s in entries] == [
        (0, 1.0),
        (1, 1.0),
        (2, 1.0),
        (None, 0.5),
        (3, 1.5),
        (None, 1.0),  # the last image: blank to past the end
    ]
    assert burn.timeline([(0.5, 1.0, "x")], 1.0) == [(None, 0.5), (0, 0.5), (None, 1.0)]
    # a long subtitle comes back when a short one inside its time ends
    assert burn.timeline([(0.0, 10.0, "outer"), (2.0, 3.0, "inner")], 12.0) == [
        (0, 2.0),
        (1, 1.0),
        (0, 7.0),
        (None, 3.0),
    ]


def test_together_joins_the_lines_of_two_speakers() -> None:
    cues = [(1.0, 2.0, "- Hi"), (1.0, 2.0, "- Hello"), (1.0, 3.0, "later end")]
    assert burn.together(cues) == [(1.0, 2.0, "- Hi\n- Hello"), (1.0, 3.0, "later end")]


def test_ffconcat_names_the_images_and_stretches_the_times() -> None:
    text = burn.ffconcat([(None, 1.25), (0, 0.5), (11, 0.001), (None, 1.0)])
    assert text == (
        "ffconcat version 1.0\n"
        "file blank.png\nduration 50.000000\n"
        "file cue_00001.png\nduration 20.000000\n"
        "file cue_00012.png\nduration 0.040000\n"
        "file blank.png\nduration 40.000000\n"
    )
    # only names the script makes: the concat demuxer's safe mode takes them
    names = re.findall(r"^file (.+)$", text, re.MULTILINE)
    assert all(re.fullmatch(r"(blank|cue_\d{5})\.png", n) for n in names)


def test_many_subtitles_give_one_list_and_one_image_each() -> None:
    parts = [(i * 0.5, i * 0.5 + 0.4, f"cue {i}") for i in range(300)]
    entries = burn.timeline(parts, 150.0)
    assert (
        len(entries) == 300 + 299 + 1
    )  # the parts, the gaps between them, the last blank
    assert sum(s for _, s in entries) == pytest.approx(151.0)
    assert {i for i, _ in entries if i is not None} == set(range(300))


def rgba_rows(path: Path) -> tuple[int, int, bytes]:
    """The width, height and RGBA bytes of a PNG (ffmpeg decodes it)."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    w, h = map(int, probe.strip().split(","))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return w, h, raw


@pytest.mark.skipif(
    importlib.util.find_spec("AppKit") is None or shutil.which("ffmpeg") is None,
    reason="AppKit (pyobjc) draws the text; ffmpeg reads the PNGs",
)
def test_the_script_draws_turkish_text_in_the_band_and_writes_the_list(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "in_burn"
    folder.mkdir()
    (tmp_path / "notes.json").write_text(json.dumps({"subtitled.mp4": "2 subtitles"}))
    lay = burn.layout(640, 360)
    long = " ".join(["Işık İğne şeker çiçek öğün üzüm"] * 6)
    cues = [(0.5, 1.5, "Merhaba dünya"), (2.0, 6.0, long)]
    spec = folder / "spec.json"
    spec.write_text(burn.spec(lay, "outline", 8.0, cues, "subtitled.mp4"))
    burn.main(spec)
    painter = burn.Painter(lay, "outline")
    assert painter.lines("Merhaba dünya") == ["Merhaba dünya"]
    assert len(painter.lines(long)) > burn.MAX_LINES
    assert all(
        len(painter.lines(t)) <= burn.MAX_LINES
        for _, _, t in burn.split(cues, painter.lines)
    )
    note = json.loads((tmp_path / "notes.json").read_text())["subtitled.mp4"]
    parts = int(re.search(r"shown in (\d+) parts", note)[1])
    assert note == f"2 subtitles; 1 long subtitle shown in {parts} parts"
    names = re.findall(
        r"^file (.+)$", (folder / "cues.ffconcat").read_text(), re.MULTILINE
    )
    assert names[:4] == ["blank.png", "cue_00001.png", "blank.png", "cue_00002.png"]
    assert len(set(names)) == 2 + parts  # blank, the short cue and the parts
    w, h, raw = rgba_rows(folder / "blank.png")
    assert (w, h) == (640, lay.band) and max(raw[3::4]) == 0  # all see-through
    w, h, raw = rgba_rows(folder / "cue_00001.png")
    alpha = raw[3::4]
    rows = [y for y in range(h) if max(alpha[y * w : (y + 1) * w]) > 0]
    cols = [x for x in range(w) if max(alpha[x::w]) > 0]
    # one line at the band's bottom, centred, with white letters and a dark outline
    assert rows[-1] >= h - lay.pad - 2 and rows[0] > lay.band // 3
    assert abs((cols[0] + cols[-1]) / 2 - w / 2) < 3
    pixels = [raw[i : i + 4] for i in range(0, len(raw), 4)]
    assert any(p[0] > 250 and p[3] == 255 for p in pixels)
    assert any(p[0] < 30 and p[3] > 200 for p in pixels)
    # a part of the long cue has two lines at most: two bands of rows with ink
    w, h, raw = rgba_rows(folder / "cue_00002.png")
    alpha = raw[3::4]
    ink = [max(alpha[y * w : (y + 1) * w]) > 0 for y in range(h)]
    starts = sum(1 for y in range(1, h) if ink[y] and not ink[y - 1]) + ink[0]
    assert 1 <= starts <= 2
