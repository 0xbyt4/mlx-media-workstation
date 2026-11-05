"""Burns subtitles into a video's picture (open captions), step 1 of 2: draws each subtitle as a see-through PNG with
macOS's text system (AppKit, through pyobjc) and writes an ffconcat list that shows each PNG for its time. Step 2, the
job's ffmpeg command (app/tools.py plan_burn), lays that list over the picture with the overlay filter. Homebrew's
ffmpeg has no libass and no drawtext, so the text is drawn here.

The app's own Python runs this script (pyobjc is in the app's environment); the app also imports its pure parts
(layout, split, timeline, ffconcat) when it plans the job.

  python burn_subtitles.py in_burn/spec.json

spec.json holds the layout (layout()), the style, the video's length and the subtitles ([start, end, text], plain
text). The script writes cue_<n>.png, blank.png and cues.ffconcat next to it. When it splits a long subtitle, it adds
that to the output's note in the job's notes.json.
"""

import heapq
import itertools
import json
import math
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# the font size: a share of the picture's short side (the height of a landscape video, the width of a Short)
SIZES = {"small": 0.045, "medium": 0.055, "large": 0.07}
PLACES = ("bottom", "top")
STYLES = ("outline", "box")
MIN_FONT = 12  # px
MAX_LINES = 2  # a longer subtitle is shown in parts of at most this many lines
LINE_ROOM = 1.5  # the height of one line in the PNG, in font sizes (a fallback font for another script is taller)
# the space from the picture's edge to the text, as a share of the picture's height. A tall video (a Short) gets more
# at the bottom: the player's buttons and the title cover about its lowest fifth.
MARGIN = {
    ("bottom", False): 0.07,
    ("bottom", True): 0.2,
    ("top", False): 0.07,
    ("top", True): 0.1,
}
# the share of the picture's width that the text may use (a Short's player has buttons at the right edge)
TEXT_WIDTH = {False: 0.9, True: 0.8}
# the concat demuxer times an image in steps of 1/25 s. The list's times are STRETCH times longer (steps of 1 ms in
# the video), and ffmpeg's setpts makes them short again (app/tools.py plan_burn).
STRETCH = 40
OUTLINE = 20.0  # the outline's stroke, in percent of the font size (half of it is outside the letters)
BOX_ALPHA = 0.6
TAGS = re.compile(r"</?[ibu]>")


@dataclass
class Layout:
    """Where the text goes: a band of the picture's width and band px high, with its top at y."""

    width: int  # the picture's width and height
    height: int
    font: int  # px
    text_width: int  # the widest line, px
    band: int
    y: int
    pad: int  # room around the text for the outline, the shadow and the box
    place: str


def layout(
    width: int, height: int, size: str = "medium", place: str = "bottom"
) -> Layout:
    """The text's size and place for a picture: the font size is a share of the short side; the margins are a
    share of the height."""
    if size not in SIZES or place not in PLACES:
        raise ValueError(f"Unknown subtitle size or place: {size!r}, {place!r}.")
    tall = height > width
    font = max(MIN_FONT, round(min(width, height) * SIZES[size]))
    pad = math.ceil(font * 0.25)
    band = min(height, MAX_LINES * math.ceil(font * LINE_ROOM) + 2 * pad)
    text_width = max(font, round(width * TEXT_WIDTH[tall]) - 2 * pad)
    if place == "top":
        y = round(height * MARGIN["top", tall]) - pad
    else:
        y = height - round(height * MARGIN["bottom", tall]) + pad - band
    return Layout(
        width, height, font, text_width, band, max(0, min(y, height - band)), pad, place
    )


def plain(text: str) -> str:
    """A subtitle's text without the <i>, <b> and <u> tags that app/runners/subtitles.py keeps."""
    return TAGS.sub("", text).strip()


def split(
    cues: list[tuple[float, float, str]], lines_of: Callable[[str, int], list[str]]
) -> list[tuple[float, float, str]]:
    """Each subtitle in parts of at most MAX_LINES lines. lines_of(text, count) gives the lines that the text wraps
    into, in order and with the spaces where they break (joined, they give the text again): at the widest line for
    count 0, else at the narrowest width that gives at most count lines. A part gets a share of the subtitle's time by
    its characters."""
    parts = []
    for start, end, text in cues:
        lines = lines_of(text, 0)
        if len(lines) <= MAX_LINES:
            parts.append((start, end, text))
            continue
        n = math.ceil(len(lines) / MAX_LINES)
        # 3 lines at the widest line become 2 parts of 2 lines of about the same length, not 2 lines and 1
        even = lines_of(text, n * MAX_LINES)
        lines = even if len(even) == n * MAX_LINES else lines
        # the lines go to n parts as evenly as they can: 5 lines give parts of 2, 2 and 1
        sizes = [len(lines) // n + (i < len(lines) % n) for i in range(n)]
        texts, at = [], 0
        for k in sizes:
            texts.append("".join(lines[at : at + k]).strip())
            at += k
        texts = [t for t in texts if t]
        total = sum(len(t) for t in texts) or 1
        t0 = start
        for i, t in enumerate(texts):
            t1 = end if i == len(texts) - 1 else t0 + (end - start) * len(t) / total
            parts.append((t0, t1, t))
            t0 = t1
    return parts


def timeline(
    parts: list[tuple[float, float, str]], length: float
) -> list[tuple[int | None, float]]:
    """What shows from 0 to past the video's end: (the part's index, or None for no text; seconds). When parts
    overlap, the one that started last shows; an earlier one that still runs comes back when it ends."""
    times = sorted({0.0, length, *(min(t, length) for p in parts for t in p[:2])})
    order = sorted(range(len(parts)), key=lambda i: parts[i][0])
    running: list[
        tuple[float, int, float]
    ] = []  # (-start, -index, end): the newest part first
    entries: list[tuple[int | None, float]] = []
    k = 0
    for at, nxt in itertools.pairwise(times):
        while k < len(order) and parts[order[k]][0] <= at:
            i = order[k]
            heapq.heappush(running, (-parts[i][0], -i, parts[i][1]))
            k += 1
        while running and running[0][2] <= at:
            heapq.heappop(running)  # ended; a part under it can show again
        shown = -running[0][1] if running else None
        if entries and entries[-1][0] == shown:
            entries[-1] = (shown, entries[-1][1] + nxt - at)
        elif nxt - at >= 0.0005:
            entries.append((shown, nxt - at))
    if entries and entries[-1][0] is None:
        entries[-1] = (None, entries[-1][1] + 1.0)
    else:
        entries.append((None, 1.0))  # the last image: blank to past the end
    return entries


def together(
    cues: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """Subtitles with the same start and end as one (two speakers' lines): their texts on separate lines."""
    out: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        if out and out[-1][:2] == (start, end):
            out[-1] = (start, end, out[-1][2] + "\n" + text)
        else:
            out.append((start, end, text))
    return out


def image_name(i: int | None) -> str:
    return "blank.png" if i is None else f"cue_{i + 1:05d}.png"


def ffconcat(entries: list[tuple[int | None, float]]) -> str:
    """The concat demuxer's list for a timeline: each image with its time, STRETCH times longer."""
    lines = ["ffconcat version 1.0"]
    for i, seconds in entries:
        lines += [f"file {image_name(i)}", f"duration {seconds * STRETCH:.6f}"]
    return "\n".join(lines) + "\n"


class Painter:
    """Lays out and draws subtitle text with AppKit: the bold system font (macOS picks another font for a script
    that it does not cover), centred lines, white text with a dark outline or on a see-through dark box."""

    def __init__(self, lay: Layout, style: str) -> None:
        import AppKit
        import objc
        import Quartz

        self.ak, self.q, self.lay, self.style = AppKit, Quartz, lay, style
        # each call drains its own Cocoa objects: memory stays flat with thousands of subtitles
        self.pool = objc.autorelease_pool
        font = AppKit.NSFont.systemFontOfSize_weight_(
            lay.font, AppKit.NSFontWeightSemibold
        )
        para = AppKit.NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(AppKit.NSTextAlignmentCenter)
        para.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        self.attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSParagraphStyleAttributeName: para,
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
        }
        self.dark = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.9)
        self.shadow = AppKit.NSShadow.alloc().init()
        self.shadow.setShadowColor_(
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.6)
        )
        self.shadow.setShadowBlurRadius_(lay.font * 0.12)
        self.shadow.setShadowOffset_(AppKit.NSMakeSize(0, lay.font * 0.04))
        self.storage = AppKit.NSTextStorage.alloc().init()
        self.manager = AppKit.NSLayoutManager.alloc().init()
        self.container = AppKit.NSTextContainer.alloc().initWithSize_(
            AppKit.NSMakeSize(lay.text_width, 1e7)
        )
        self.container.setLineFragmentPadding_(0)
        self.manager.addTextContainer_(self.container)
        self.storage.addLayoutManager_(self.manager)
        self.space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)

    def _set(self, text: str, width: float) -> list[tuple[object, object]]:
        """Lay text out at width: (glyph range, character range) of each line."""
        ak = self.ak
        self.storage.setAttributedString_(
            ak.NSAttributedString.alloc().initWithString_attributes_(text, self.attrs)
        )
        self.container.setSize_(ak.NSMakeSize(width, 1e7))
        glyphs = self.manager.glyphRangeForTextContainer_(self.container)
        lines, i = [], glyphs.location
        while i < glyphs.location + glyphs.length:
            _, rng = self.manager.lineFragmentUsedRectForGlyphAtIndex_effectiveRange_(
                i, None
            )
            chars, _ = self.manager.characterRangeForGlyphRange_actualGlyphRange_(
                rng, None
            )
            lines.append((rng, chars))
            i = rng.location + rng.length
        return lines

    def lines(self, text: str, count: int = 0) -> list[str]:
        """The lines that text wraps into (for split): at the widest line, or with count at the narrowest width that
        gives at most count lines (lines of about the same length)."""
        width = float(self.lay.text_width)
        if count and len(self._set(text, width)) <= count:
            lo = float(self.lay.font)
            for _ in range(12):
                mid = (lo + width) / 2
                if len(self._set(text, mid)) <= count:
                    width = mid
                else:
                    lo = mid
        lines = self._set(text, width)
        ns = self.storage.string()
        return [str(ns.substringWithRange_(chars)) for _, chars in lines]

    def balanced(self, text: str) -> float:
        """The narrowest width that keeps the line count of the widest line: two lines of about the same length."""
        lo, hi = self.lay.font, float(self.lay.text_width)
        count = len(self._set(text, hi))
        if count < 2:
            return hi
        for _ in range(12):
            mid = (lo + hi) / 2
            if len(self._set(text, mid)) == count:
                hi = mid
            else:
                lo = mid
        return math.ceil(hi)

    def _write(self, ctx: object, path: Path) -> None:
        q, ak = self.q, self.ak
        image = q.CGBitmapContextCreateImage(ctx)
        dest = q.CGImageDestinationCreateWithURL(
            ak.NSURL.fileURLWithPath_(str(path)), "public.png", 1, None
        )
        q.CGImageDestinationAddImage(dest, image, None)
        if not q.CGImageDestinationFinalize(dest):
            raise OSError(f"Could not write {path.name}.")

    def _context(self) -> object:
        q = self.q
        ctx = q.CGBitmapContextCreate(
            None,
            self.lay.width,
            self.lay.band,
            8,
            0,
            self.space,
            q.kCGImageAlphaPremultipliedLast,
        )
        # top-left origin, as the layout manager draws
        q.CGContextTranslateCTM(ctx, 0, self.lay.band)
        q.CGContextScaleCTM(ctx, 1, -1)
        return ctx

    def blank(self, path: Path) -> None:
        self._write(self._context(), path)

    def draw(self, text: str, path: Path) -> None:
        """One PNG of the band with the text centred: at its bottom (place bottom) or top."""
        ak, q, lay = self.ak, self.q, self.lay
        width = self.balanced(text)
        lines = self._set(text, width)
        used = self.manager.usedRectForTextContainer_(self.container).size.height
        top = lay.band - lay.pad - used if lay.place == "bottom" else lay.pad
        origin = ak.NSMakePoint((lay.width - width) / 2, max(0.0, top))
        ctx = self._context()
        ak.NSGraphicsContext.saveGraphicsState()
        ak.NSGraphicsContext.setCurrentContext_(
            ak.NSGraphicsContext.graphicsContextWithCGContext_flipped_(ctx, True)
        )
        everything = ak.NSMakeRange(0, self.storage.length())
        glyphs = self.manager.glyphRangeForTextContainer_(self.container)
        if self.style == "box":
            ak.NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, BOX_ALPHA).setFill()
            ns = self.storage.string()
            for line, chars in lines:
                # the box ends at the last letter, not at the space or line break after it
                body = str(ns.substringWithRange_(chars)).rstrip()
                if not body:
                    continue
                inked, _ = (
                    self.manager.glyphRangeForCharacterRange_actualCharacterRange_(
                        ak.NSMakeRange(
                            chars.location, len(body.encode("utf-16-le")) // 2
                        ),
                        None,
                    )
                )
                box = self.manager.boundingRectForGlyphRange_inTextContainer_(
                    inked, self.container
                )
                frag, _ = self.manager.lineFragmentRectForGlyphAtIndex_effectiveRange_(
                    line.location, None
                )
                gap = lay.font * 0.3
                rect = ak.NSMakeRect(
                    origin.x + box.origin.x - gap,
                    origin.y + frag.origin.y,
                    box.size.width + 2 * gap,
                    frag.size.height,
                )
                radius = lay.font * 0.12
                ak.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    rect, radius, radius
                ).fill()
        else:
            q.CGContextSetLineJoin(ctx, q.kCGLineJoinRound)
            self.storage.addAttributes_range_(
                {
                    ak.NSStrokeWidthAttributeName: OUTLINE,
                    ak.NSStrokeColorAttributeName: self.dark,
                    ak.NSShadowAttributeName: self.shadow,
                },
                everything,
            )
            self.manager.drawGlyphsForGlyphRange_atPoint_(glyphs, origin)
            for name in (ak.NSStrokeWidthAttributeName, ak.NSShadowAttributeName):
                self.storage.removeAttribute_range_(name, everything)
        self.manager.drawGlyphsForGlyphRange_atPoint_(glyphs, origin)
        ak.NSGraphicsContext.restoreGraphicsState()
        self._write(ctx, path)


def note_split(folder: Path, output: str, split_count: int, parts: int) -> None:
    """Add the split subtitles to the output's note in the job's notes.json."""
    path = folder.parent / "notes.json"
    try:
        notes = json.loads(path.read_text())
    except (OSError, ValueError):
        notes = {}
    note = notes.get(output, "")
    word = "subtitle" if split_count == 1 else "subtitles"
    notes[output] = note + f"; {split_count} long {word} shown in {parts} parts"
    path.write_text(json.dumps(notes))


def main(spec_path: Path) -> None:
    t0 = time.time()
    spec = json.loads(spec_path.read_text())
    lay = Layout(**spec["layout"])
    folder = spec_path.parent
    painter = Painter(lay, spec["style"])
    cues = together([(float(s), float(e), str(t)) for s, e, t in spec["cues"]])
    counts = []  # the line count of each subtitle

    def lines_of(text: str, count: int) -> list[str]:
        with painter.pool():
            lines = painter.lines(text, count)
        if not count:
            counts.append(len(lines))
        return lines

    parts = sorted(split(cues, lines_of), key=lambda p: p[0])
    entries = timeline(parts, float(spec["length"]))
    painter.blank(folder / image_name(None))
    shown = sorted({i for i, _ in entries if i is not None})
    for i in shown:
        with painter.pool():
            painter.draw(parts[i][2], folder / image_name(i))
    (folder / "cues.ffconcat").write_text(ffconcat(entries))
    if len(parts) > len(cues):
        longer = sum(n > MAX_LINES for n in counts)
        note_split(folder, spec["output"], longer, longer + len(parts) - len(cues))
    print(
        f"{len(cues)} subtitles, {len(shown)} images ({lay.width}x{lay.band}, font {lay.font} px) in "
        f"{time.time() - t0:.1f} s",
        flush=True,
    )


def spec(
    lay: Layout,
    style: str,
    length: float,
    cues: list[tuple[float, float, str]],
    output: str,
) -> str:
    """The text of spec.json (app/tools.py writes it when it plans the job)."""
    return json.dumps(
        {
            "layout": asdict(lay),
            "style": style,
            "length": length,
            "cues": cues,
            "output": output,
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
