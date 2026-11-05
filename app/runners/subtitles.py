"""Subtitle cues (standard library only): read an SRT or WebVTT file, build cues from timed words, write SRT and
WebVTT.

The app imports it as app.runners.subtitles (edit.subtitle checks and cleans the uploaded file); the transcription
runner, which runs in the Drama 3 environment, imports it as subtitles from its own folder.
"""

import html
import re
from dataclasses import dataclass

MAX_BYTES = 5 * 1024 * 1024  # a subtitle file larger than this is refused
MAX_CUES = 20000
MAX_CUE_TEXT = 1000  # characters of one cue
# a cue that is built from words: at most 2 lines of 42 characters (16 in a language without spaces, whose
# characters are wide), 1-6 s; a pause of 0.8 s starts a new cue
LINE, WIDE_LINE = 42, 16
CUE_MIN, CUE_MAX = 1.0, 6.0
PAUSE = 0.8
SENTENCE_END = ".?!…。？！"
# hours are optional (WebVTT); the milliseconds take a comma (SRT) or a full stop (WebVTT)
TIME = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
TIME_LINE = re.compile(rf"^\s*{TIME}\s*-->\s*{TIME}(?:\s.*)?$")
# tags other than <i>, <b> and <u> (WebVTT voices, classes, inline times; SRT font tags) and {\an8}-style overrides
OTHER_TAGS = re.compile(r"<(?!/?[ibu]>)[^>]*>|\{\\[^}]*\}")
KEPT_TAGS = re.compile(r"(</?[ibu]>)")
# ffmpeg's SRT reader takes <x...> for a tag and {\...}, \N, \n and \h for ASS codes. A word joiner (invisible, no
# width) after the < or the \ keeps such text as it is.
SRT_CODES = re.compile(r"<(?=\S)|\{(?=\\)|\\(?=[Nnh])")
JOINER = "\u2060"


@dataclass
class Cue:
    start: float  # seconds
    end: float
    text: str  # one or more lines


def seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def decode(data: bytes) -> str:
    """The text of a subtitle file: UTF-8 (with or without a BOM) or UTF-16 with a BOM. Raises ValueError."""
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"The subtitle file is larger than {MAX_BYTES // (1024 * 1024)} MB."
        )
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(
            "The subtitle file is not UTF-8 text. Save it as UTF-8 and try again."
        ) from None


def parse(text: str) -> list[Cue]:
    """The cues of an SRT or WebVTT text, sorted by start. Tags other than <i>, <b> and <u> are removed; empty cues are
    left out. Raises ValueError (shown to the user) with the cue number when a cue cannot be read."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for n, block in enumerate(re.split(r"\n[ \t]*\n", text.strip()), 1):
        lines = [line.rstrip() for line in block.split("\n")]
        head = lines[0].strip()
        if n == 1 and head.startswith("WEBVTT"):
            continue
        if head.startswith(("NOTE", "STYLE", "REGION")):
            continue
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if at is None:
            if cues and not head.isdigit():  # a blank line inside a cue's text
                cues[-1].text += "\n" + "\n".join(lines)
                continue
            raise ValueError(
                f"Subtitle block {n} has no time line (00:00:01,000 --> 00:00:02,500)."
            )
        m = TIME_LINE.match(lines[at])
        if m is None:
            raise ValueError(f"Subtitle block {n}: cannot read the time line {lines[at].strip()[:60]!r}.")  # fmt: skip
        start, end = seconds(*m.groups()[:4]), seconds(*m.groups()[4:])
        if end <= start:
            raise ValueError(f"Subtitle block {n} ends before it starts.")
        body = "\n".join(
            s
            for s in (OTHER_TAGS.sub("", line).strip() for line in lines[at + 1 :])
            if s
        )
        if len(body) > MAX_CUE_TEXT:
            raise ValueError(
                f"Subtitle block {n} has more than {MAX_CUE_TEXT} characters."
            )
        if body:
            cues.append(Cue(start, end, body))
        if len(cues) > MAX_CUES:
            raise ValueError(f"The subtitle file has more than {MAX_CUES} cues.")
    if not cues:
        raise ValueError("The subtitle file has no cues.")
    return sorted(cues, key=lambda c: c.start)


def vtt_to_srt(text: str) -> str:
    """A WebVTT cue's text (from parse) for an SRT file that ffmpeg reads: character references such as &amp;, &lt;,
    &nbsp; and &lrm; become their characters, and the <i>, <b> and <u> tags stay. A < or a backslash that ffmpeg would read as
    a tag or a code gets a word joiner, so it shows as it is."""
    parts = KEPT_TAGS.split(text)  # text, tag, text, ...
    for i in range(0, len(parts), 2):
        parts[i] = SRT_CODES.sub(lambda m: m[0] + JOINER, html.unescape(parts[i]))
    return "".join(parts)


def clock(t: float, sep: str = ",") -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def srt(cues: list[Cue]) -> str:
    """SRT: a text line cannot hold "-->" (a reader takes it for a time line)."""
    return "".join(
        f"{i}\n{clock(c.start)} --> {clock(c.end)}\n{c.text.replace('-->', '->')}\n\n"
        for i, c in enumerate(cues, 1)
    )


def vtt(cues: list[Cue]) -> str:
    """WebVTT: the text is escaped (a plain transcript has no tags) and cannot hold "-->"."""

    def escape(text: str) -> str:
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return text.replace("--&gt;", "-- &gt;")

    body = "".join(
        f"{clock(c.start, '.')} --> {clock(c.end, '.')}\n{escape(c.text)}\n\n"
        for c in cues
    )
    return "WEBVTT\n\n" + body


def spread(
    units: list[str], start: float, end: float
) -> list[tuple[str, float, float]]:
    """Timed words when only a stretch of speech has a time: each unit gets a share of start..end by its length."""
    weights = [len(u) + 1 for u in units]
    total = sum(weights) or 1
    out, at = [], start
    for u, w in zip(units, weights, strict=True):
        step = (end - start) * w / total
        out.append((u, at, at + step))
        at += step
    return out


def wrap(text: str, sep: str) -> str | None:
    """The text on one line, or on two at the space (with no spaces, the character) nearest the middle that keeps
    both lines short enough. None when it does not fit on two lines."""
    width = LINE if sep else WIDE_LINE
    if len(text) <= width:
        return text
    cuts = (
        [i for i, ch in enumerate(text) if ch == " "]
        if sep
        else list(range(1, len(text)))
    )
    skip = len(sep)
    cuts = [i for i in cuts if i <= width and len(text) - i - skip <= width]
    if not cuts:
        return None
    cut = min(cuts, key=lambda i: abs(i - (len(text) - skip) / 2))
    return text[:cut] + "\n" + text[cut + skip :]


def build(words: list[tuple[str, float, float]], sep: str = " ") -> list[Cue]:
    """Cues from timed words (in order): a new cue at a pause, at the length or line limit, and after a sentence. A
    short cue stays up for CUE_MIN, but not past the next one."""
    groups: list[list[tuple[str, float, float]]] = []
    for w in words:
        cur = groups[-1] if groups else None
        if cur is not None:
            text = sep.join(x[0] for x in cur) + sep + w[0]
            done = cur[-1][0][-1:] in SENTENCE_END
            if (
                done
                or wrap(text, sep) is None
                or w[1] - cur[-1][2] > PAUSE
                or w[2] - cur[0][1] > CUE_MAX
            ):
                cur = None
        if cur is None:
            groups.append([w])
        else:
            cur.append(w)
    # a single word longer than two lines stays on one
    cues = []
    for g in groups:
        text = sep.join(x[0] for x in g)
        cues.append(Cue(g[0][1], g[-1][2], wrap(text, sep) or text))
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else None
        end = max(c.end, c.start + CUE_MIN)
        c.end = min(end, nxt) if nxt is not None and nxt > c.start else end
    return cues
