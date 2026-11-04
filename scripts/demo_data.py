"""Fill an outputs folder with a demo state for the README and guide screenshots (scripts/screenshots.py).

It writes finished jobs of most tools with real media files, and three chats in the chat.json format of app/chat.py:
a Workstation chat with "Use in" prompt blocks, a General chat and an Agent chat with a plan and tool calls. No model
runs and nothing downloads. Core Graphics draws the images and the video frames, macOS `say` speaks the voice takes,
this script synthesizes the piano takes, and ffmpeg encodes the audio and the videos. All content is made here.

A running or queued job cannot be stored: the server marks such jobs as cancelled when it starts. LIVE_JOBS holds the
jobs that scripts/screenshots.py submits after the start, and agent_turn() the agent answer it asks for.

Run it from the repository folder (the folder must be empty or missing):

  .venv/bin/python scripts/demo_data.py /path/to/demo/outputs
"""

import hashlib
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import Quartz as Q
from Foundation import NSURL

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import tools  # after the path above

# the home folder that the job logs show (scripts/screenshots.py shows the demo home under this name)
HOME = "/Users/demo"
KINDS = {".png": "image", ".wav": "audio", ".mp4": "video"}  # as app/server.py KINDS
FPS = 24
FRAMES = 121  # 5 s at 24 fps, as LTX-2.5 makes it (8 n + 1 frames)
PEAK_LINE = "{:>20}  peak memory footprint"  # the last lines of /usr/bin/time -l

# ---------- prompts ----------

BOAT = (
    "Low close-up at street level in the evening. A small white paper boat drifts slowly down a narrow gutter full "
    "of rainwater, turning gently in the current. Raindrops make rings on the water and the wet asphalt reflects "
    "warm orange street lights. The camera tracks beside the boat at the same speed. Sound: steady soft rain, water "
    "trickling, a distant car passing."
)
LIGHTHOUSE = (
    "Paper-cut illustration of a lighthouse on a dark cliff at dusk. Layered paper shapes, a warm beam reaching over "
    "a calm sea, the first stars in a deep blue sky that fades to peach at the horizon. Clean flat colours and soft "
    "shadows between the layers."
)
LIGHTHOUSE_VIDEO = (
    "The lighthouse beam sweeps slowly across the sea. Small waves roll in below the cliff and the stars twinkle. "
    "The camera holds still and the frame ends on the full lighthouse. Sound: soft waves and a light wind."
)
MOUNTAINS = (
    "Layered mountain ridges at sunset in a flat vector style. A large pale sun sits low behind the ridges, the sky "
    "fades from violet to warm orange, and thin mist lies between the layers. A few birds fly over the far ridge. "
    "Calm, minimal poster composition, soft grain, no text."
)
DUNES = (
    "Minimal travel poster of desert dunes at golden hour under a huge ringed planet. Smooth flat shapes, warm orange "
    "and deep teal palette, a thin crescent moon, no text."
)
MUG = (
    "Put the teal mug from <ref_image_1> on a table with the terrazzo pattern of <ref_image_0>. Soft morning light "
    "from the left, a plain warm wall behind. Keep the mug's shape and colour unchanged."
)
VOICE = (
    "The lighthouse keeper climbed the stairs one last time. [quiet] Below him the sea was calm, and "
    "the first stars came out over the water."
)
PIANO = "Calm solo piano, slow arpeggios, warm room reverb, gentle and hopeful, film score for a quiet night by the sea"

# what scripts/screenshots.py submits after the server starts: one runs, the others wait in the queue
LIVE_JOBS: list[dict[str, str]] = [
    {
        "tool": "image.moss",
        "prompt": (
            "Paper-cut illustration of a red hot air balloon over rolling green hills at sunrise. Layered paper "
            "shapes with soft shadows, a pale gold sky, small round trees and a winding river. Calm, flat colours."
        ),
        "size": "1024x1024",
        "steps": "50",
    },
    {
        "tool": "video.t2v",
        "prompt": (
            "Slow aerial shot over a foggy pine forest at sunrise. Low clouds drift between the trees and the first "
            "light turns the fog gold. The camera glides forward and the frame ends on a small lake. Sound: birds, "
            "a soft wind."
        ),
        "size": "704x1280",
        "seconds": "5",
        "audio": "true",
    },
    {
        "tool": "music.ace",
        "caption": "Light acoustic guitar and soft shaker, warm and hopeful, travel vlog morning, 100 bpm feel",
        "instrumental": "true",
        "duration": "30",
        "takes": "2",
    },
]
# the text that the Create screen's Text to Video form shows
CREATE_FORM = {"prompt": BOAT, "size": "704x1280", "seconds": "5", "audio": "true"}


# ---------- drawing (Core Graphics, y down) ----------


def rgb(hex_rgb: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    return (*(int(hex_rgb[i : i + 2], 16) / 255 for i in (1, 3, 5)), alpha)


class Canvas:
    """A BGRA bitmap with y pointing down, and the few shapes the scenes need."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.space = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
        info = Q.kCGImageAlphaPremultipliedFirst | Q.kCGBitmapByteOrder32Little
        self.ctx = Q.CGBitmapContextCreate(None, w, h, 8, w * 4, self.space, info)
        Q.CGContextTranslateCTM(self.ctx, 0, h)
        Q.CGContextScaleCTM(self.ctx, 1, -1)
        Q.CGContextSetShouldAntialias(self.ctx, True)

    def gradient(self, stops: list[tuple[float, str, float]]) -> Any:
        comps = [c for _, hx, a in stops for c in rgb(hx, a)]
        return Q.CGGradientCreateWithColorComponents(
            self.space, comps, [p for p, _, _ in stops], len(stops)
        )

    def path(self, points: list[tuple[float, float]]) -> Any:
        p = Q.CGPathCreateMutable()
        Q.CGPathMoveToPoint(p, None, *points[0])
        for pt in points[1:]:
            Q.CGPathAddLineToPoint(p, None, *pt)
        Q.CGPathCloseSubpath(p)
        return p

    def ellipse(self, x: float, y: float, rx: float, ry: float) -> Any:
        p = Q.CGPathCreateMutable()
        Q.CGPathAddEllipseInRect(p, None, Q.CGRectMake(x - rx, y - ry, 2 * rx, 2 * ry))
        return p

    def rect(self, x: float, y: float, w: float, h: float) -> Any:
        p = Q.CGPathCreateMutable()
        Q.CGPathAddRect(p, None, Q.CGRectMake(x, y, w, h))
        return p

    def fill(
        self, path: Any, color: str, alpha: float = 1.0, shadow: float = 0.0
    ) -> None:
        """Fill a shape; shadow > 0 drops a soft shadow under it, as paper layers do."""
        c = self.ctx
        Q.CGContextSaveGState(c)
        if shadow:
            s = self.w / 768
            Q.CGContextSetShadowWithColor(
                c, Q.CGSizeMake(0, -5 * s), 14 * s, Q.CGColorCreateSRGB(0, 0, 0, shadow)
            )
        Q.CGContextSetRGBFillColor(c, *rgb(color, alpha))
        Q.CGContextAddPath(c, path)
        Q.CGContextFillPath(c)
        Q.CGContextRestoreGState(c)

    def linear(
        self,
        path: Any | None,
        stops: list[tuple[float, str, float]],
        p0: tuple,
        p1: tuple,
    ) -> None:
        """A linear gradient inside a shape (the whole canvas when path is None)."""
        c = self.ctx
        Q.CGContextSaveGState(c)
        if path is not None:
            Q.CGContextAddPath(c, path)
            Q.CGContextClip(c)
        Q.CGContextDrawLinearGradient(
            c, self.gradient(stops), Q.CGPointMake(*p0), Q.CGPointMake(*p1), 3
        )
        Q.CGContextRestoreGState(c)

    def glow(self, x: float, y: float, r: float, color: str, alpha: float) -> None:
        """A soft round light: the colour at the centre fading out at radius r."""
        stops = [(0.0, color, alpha), (0.4, color, alpha * 0.35), (1.0, color, 0.0)]
        Q.CGContextDrawRadialGradient(
            self.ctx,
            self.gradient(stops),
            Q.CGPointMake(x, y),
            0,
            Q.CGPointMake(x, y),
            r,
            0,
        )

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        color: str,
        alpha: float,
        width: float,
    ) -> None:
        c = self.ctx
        Q.CGContextSetRGBStrokeColor(c, *rgb(color, alpha))
        Q.CGContextSetLineWidth(c, width)
        Q.CGContextSetLineCap(c, Q.kCGLineCapRound)
        Q.CGContextMoveToPoint(c, x0, y0)
        Q.CGContextAddLineToPoint(c, x1, y1)
        Q.CGContextStrokePath(c)

    def stroke(self, path: Any, color: str, alpha: float, width: float) -> None:
        c = self.ctx
        Q.CGContextSetRGBStrokeColor(c, *rgb(color, alpha))
        Q.CGContextSetLineWidth(c, width)
        Q.CGContextAddPath(c, path)
        Q.CGContextStrokePath(c)

    def image(self) -> Any:
        return Q.CGBitmapContextCreateImage(self.ctx)

    def bgra(self) -> bytes:
        return bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(self.image())))

    def save(self, path: Path) -> None:
        url = NSURL.fileURLWithPath_(str(path))
        dest = Q.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        Q.CGImageDestinationAddImage(dest, self.image(), None)
        if not Q.CGImageDestinationFinalize(dest):
            raise SystemExit(f"Could not write {path}")


def ridge(
    w: float, base: float, amp: float, seed: int, bottom: float, steps: int = 90
) -> list[tuple[float, float]]:
    """A ridge or dune line from left to right (a sum of slow sines), closed along the bottom."""
    rnd = random.Random(seed)
    waves = [
        (rnd.uniform(1.0, 3.5) * (k + 1), rnd.uniform(0, 6.28), 1 / (k + 1) ** 1.3)
        for k in range(4)
    ]
    pts = []
    for i in range(steps + 1):
        x = w * i / steps
        u = i / steps
        y = base - amp * sum(a * math.sin(f * math.pi * u + p) for f, p, a in waves)
        pts.append((x, y))
    return [*pts, (w, bottom), (0, bottom)]


def lighthouse(cv: Canvas, t: float = 0.0) -> None:
    """A paper-cut lighthouse on a cliff at dusk; t (seconds) moves the beam, the stars and the waves."""
    w, h = cv.w, cv.h
    s = w / 768
    horizon = 0.66 * h
    cv.linear(
        None,
        [
            (0.0, "#0c1734", 1),
            (0.38, "#2b3f78", 1),
            (0.78, "#d98a8c", 1),
            (1.0, "#ffd3a1", 1),
        ],
        (0, 0),
        (0, horizon),
    )
    rnd = random.Random(7)
    for _ in range(90):
        x, y, r, ph = (
            rnd.uniform(0, w),
            rnd.uniform(0, 0.5 * h),
            rnd.uniform(0.7, 2.1) * s,
            rnd.uniform(0, 6.3),
        )
        a = 0.55 + 0.4 * math.sin(2.2 * t + ph)
        cv.fill(cv.ellipse(x, y, r, r), "#fff6e0", a * (1 - y / (0.55 * h)) + 0.1)
    cv.glow(0.3 * w, horizon, 0.5 * w, "#ffd9a8", 0.55)
    cv.linear(
        cv.rect(0, horizon, w, h - horizon),
        [(0, "#46598f", 1), (1, "#0d1730", 1)],
        (0, horizon),
        (0, h),
    )
    for i in range(14):  # light on the water under the glow
        y = horizon + (i + 1) * 0.018 * h
        half = (0.16 - 0.008 * i) * w * (1 + 0.08 * math.sin(3 * t + i))
        cv.line(
            0.3 * w - half, y, 0.3 * w + half, y, "#ffd9a8", 0.35 - 0.02 * i, 2.2 * s
        )
    # the beam from the lamp, sweeping a little
    lamp = (0.72 * w, 0.355 * h)
    ang = math.radians(196 + 9 * math.sin(0.7 * t))
    far = 1.25 * w
    spread = math.radians(8)
    tip1 = (
        lamp[0] + far * math.cos(ang - spread),
        lamp[1] + far * math.sin(ang - spread),
    )
    tip2 = (
        lamp[0] + far * math.cos(ang + spread),
        lamp[1] + far * math.sin(ang + spread),
    )
    beam = cv.path([lamp, tip1, tip2])
    end = (lamp[0] + far * math.cos(ang), lamp[1] + far * math.sin(ang))
    cv.linear(beam, [(0, "#fff1c4", 0.6), (1, "#fff1c4", 0.0)], lamp, end)
    # a low island on the left, then the cliff, each a paper layer
    island = [
        (0.42 * w * i / 40, 0.8 * h - 0.035 * h * math.sin(math.pi * i / 40) ** 0.6)
        for i in range(41)
    ]
    cv.fill(
        cv.path([(0, 0.79 * h), *island, (0.42 * w, h), (0, h)]), "#1d2a52", 1, 0.35
    )
    cliff = [
        (0.42 * w, h),
        (0.5 * w, 0.8 * h),
        (0.56 * w, 0.7 * h),
        (0.62 * w, 0.66 * h),
        (0.85 * w, 0.64 * h),
        (w, 0.63 * h),
        (w, h),
    ]
    cv.fill(cv.path(cliff), "#151d3b", 1, 0.45)
    cv.fill(
        cv.path(
            [
                (0.55 * w, h),
                (0.64 * w, 0.82 * h),
                (0.8 * w, 0.76 * h),
                (w, 0.74 * h),
                (w, h),
            ]
        ),
        "#202c55",
        1,
        0.4,
    )
    # the tower: white with red bands, a gallery, the lamp room and the roof
    base_y, top_y = 0.645 * h, 0.395 * h
    bw, tw = 0.05 * w, 0.033 * w
    cx = lamp[0]

    def half_width(y: float) -> float:
        return tw + (bw - tw) * (y - top_y) / (base_y - top_y)

    cv.fill(
        cv.path(
            [(cx - bw, base_y), (cx - tw, top_y), (cx + tw, top_y), (cx + bw, base_y)]
        ),
        "#f3ede2",
        1,
        0.4,
    )
    for k in range(3):
        y0 = top_y + (0.18 + 0.27 * k) * (base_y - top_y)
        y1 = y0 + 0.11 * (base_y - top_y)
        band = [
            (cx - half_width(y0), y0),
            (cx + half_width(y0), y0),
            (cx + half_width(y1), y1),
            (cx - half_width(y1), y1),
        ]
        cv.fill(cv.path(band), "#c9473d")
    cv.fill(
        cv.path([(cx, top_y), (cx + bw, base_y), (cx, base_y)]), "#000000", 0.12
    )  # shade on the right side
    cv.fill(cv.rect(cx - 1.35 * tw, top_y - 0.008 * h, 2.7 * tw, 0.012 * h), "#2a2f45")
    cv.glow(lamp[0], lamp[1], 0.12 * w, "#ffe7a6", 0.9)
    cv.fill(cv.rect(cx - 0.8 * tw, top_y - 0.05 * h, 1.6 * tw, 0.043 * h), "#ffe8a8")
    for k in (-1, 0, 1):
        cv.line(
            cx + 0.45 * k * tw,
            top_y - 0.05 * h,
            cx + 0.45 * k * tw,
            top_y - 0.008 * h,
            "#3a3f55",
            0.9,
            1.6 * s,
        )
    cv.fill(
        cv.path(
            [
                (cx - 1.1 * tw, top_y - 0.05 * h),
                (cx, top_y - 0.085 * h),
                (cx + 1.1 * tw, top_y - 0.05 * h),
            ]
        ),
        "#b23c33",
        1,
        0.3,
    )
    # small house at the foot of the tower, with one lit window
    cv.fill(
        cv.path(
            [
                (0.79 * w, 0.645 * h),
                (0.79 * w, 0.615 * h),
                (0.83 * w, 0.592 * h),
                (0.87 * w, 0.615 * h),
                (0.87 * w, 0.645 * h),
            ]
        ),
        "#e9e2d4",
        1,
        0.3,
    )
    cv.fill(cv.rect(0.815 * w, 0.618 * h, 0.018 * w, 0.012 * h), "#ffd27a")
    # waves: paper strips at the bottom
    for k, (col, base) in enumerate(
        (("#30447a", 0.83), ("#24366a", 0.88), ("#1a2856", 0.93))
    ):
        pts = []
        for i in range(61):
            x = w * i / 60
            y = base * h + 0.008 * h * math.sin(2 * math.pi * (i / 12) + 1.3 * t + k)
            pts.append((x, y))
        cv.fill(cv.path([*pts, (w, h), (0, h)]), col, 1, 0.35)
        for i in range(0, 60, 7):
            x = w * (i + 2 + k) / 60
            cv.line(
                x,
                base * h + 0.02 * h,
                x + 0.04 * w,
                base * h + 0.02 * h,
                "#8fa4d8",
                0.35,
                2 * s,
            )


def mountains(cv: Canvas) -> None:
    """Layered ridges at sunset, a low sun and mist."""
    w, h = cv.w, cv.h
    cv.linear(
        None,
        [
            (0, "#2b1d5e", 1),
            (0.42, "#8a4c9b", 1),
            (0.66, "#f08a6c", 1),
            (0.8, "#ffd59e", 1),
        ],
        (0, 0),
        (0, h),
    )
    cv.glow(0.5 * w, 0.6 * h, 0.55 * w, "#ffe2b2", 0.6)
    cv.fill(cv.ellipse(0.5 * w, 0.6 * h, 0.17 * w, 0.17 * w), "#fff0d0", 0.95)
    layers = [
        ("#b46b9f", 0.62, 0.07),
        ("#8c4f8f", 0.7, 0.08),
        ("#64397a", 0.78, 0.07),
        ("#432863", 0.86, 0.06),
        ("#2a1946", 0.94, 0.05),
    ]
    for k, (col, base, amp) in enumerate(layers):
        cv.fill(cv.path(ridge(w, base * h, amp * h, 11 + k, h)), col, 1, 0.4)
        if k < 3:
            y = (base + 0.03) * h
            cv.linear(
                cv.rect(0, y - 0.04 * h, w, 0.08 * h),
                [(0, "#ffffff", 0), (0.5, "#ffe9f0", 0.18), (1, "#ffffff", 0)],
                (0, y - 0.04 * h),
                (0, y + 0.04 * h),
            )
    rnd = random.Random(5)
    for _ in range(6):
        x, y, r = (
            rnd.uniform(0.25, 0.75) * w,
            rnd.uniform(0.25, 0.42) * h,
            rnd.uniform(0.008, 0.014) * w,
        )
        cv.line(x - r, y - 0.6 * r, x, y, "#3a2450", 0.85, 0.0035 * w)
        cv.line(x, y, x + r, y - 0.6 * r, "#3a2450", 0.85, 0.0035 * w)


def dunes(cv: Canvas) -> None:
    """Dunes at golden hour under a ringed planet."""
    w, h = cv.w, cv.h
    cv.linear(
        None,
        [
            (0, "#0f2f47", 1),
            (0.5, "#2d5a6b", 1),
            (0.78, "#e7a46b", 1),
            (1, "#f7d29c", 1),
        ],
        (0, 0),
        (0, 0.72 * h),
    )
    rnd = random.Random(9)
    for _ in range(60):
        x, y, r = rnd.uniform(0, w), rnd.uniform(0, 0.45 * h), rnd.uniform(0.6, 1.6)
        cv.fill(cv.ellipse(x, y, r, r), "#fdf3dd", rnd.uniform(0.3, 0.8))
    px, py, pr = 0.66 * w, 0.33 * h, 0.2 * h
    ring = cv.ellipse(px, py, 1.9 * pr, 0.42 * pr)
    cv.stroke(ring, "#f6dcae", 0.55, 0.035 * h)
    cv.glow(px, py, 2.2 * pr, "#ffd9a0", 0.35)
    planet = cv.ellipse(px, py, pr, pr)
    cv.linear(
        planet,
        [(0, "#f6d7a3", 1), (0.6, "#e39a62", 1), (1, "#b45c45", 1)],
        (px - pr, py - pr),
        (px + pr, py + pr),
    )
    for k in range(3):
        band = cv.rect(px - pr, py - 0.35 * pr + 0.32 * k * pr, 2 * pr, 0.09 * pr)
        Q.CGContextSaveGState(cv.ctx)
        Q.CGContextAddPath(cv.ctx, planet)
        Q.CGContextClip(cv.ctx)
        cv.fill(band, "#ffffff", 0.12)
        Q.CGContextRestoreGState(cv.ctx)
    Q.CGContextSaveGState(cv.ctx)  # the front half of the ring passes over the planet
    Q.CGContextAddPath(cv.ctx, cv.rect(0, py, w, h))
    Q.CGContextClip(cv.ctx)
    cv.stroke(ring, "#fbe6bf", 0.85, 0.035 * h)
    Q.CGContextRestoreGState(cv.ctx)
    cv.fill(cv.ellipse(0.16 * w, 0.18 * h, 0.035 * h, 0.035 * h), "#fdf0d0", 0.9)
    cv.fill(cv.ellipse(0.168 * w, 0.17 * h, 0.032 * h, 0.032 * h), "#143349")
    layers = [
        ("#f2b06b", 0.7, 0.05),
        ("#e2814f", 0.78, 0.06),
        ("#b9553d", 0.87, 0.05),
        ("#6e2f2c", 0.96, 0.04),
    ]
    for k, (col, base, amp) in enumerate(layers):
        cv.fill(cv.path(ridge(w, base * h, amp * h, 21 + k, h)), col, 1, 0.35)


def terrazzo(cv: Canvas) -> None:
    """A cream terrazzo surface with coloured chips (Klein Edit reference 0)."""
    w, h = cv.w, cv.h
    cv.fill(cv.rect(0, 0, w, h), "#efe6d6")
    rnd = random.Random(4)
    for _ in range(260):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        r = rnd.uniform(0.006, 0.02) * w
        pts = [
            (
                x + r * math.cos(a) * rnd.uniform(0.6, 1.2),
                y + r * math.sin(a) * rnd.uniform(0.6, 1.2),
            )
            for a in [i * 1.2566 for i in range(5)]
        ]
        cv.fill(
            cv.path(pts),
            rnd.choice(["#d9714e", "#2f6f73", "#e8b04a", "#9aa6a0", "#c84f5f"]),
            0.9,
        )


def mug_shape(cv: Canvas, cx: float, base: float, size: float, shadow: float) -> None:
    """A teal mug with a handle and a little steam."""
    mw, mh = size, 1.05 * size
    handle = cv.ellipse(cx + 0.55 * mw, base - 0.52 * mh, 0.24 * mw, 0.26 * mh)
    cv.stroke(handle, "#1f6b70", 1, 0.09 * mw)
    body = cv.path(
        [
            (cx - 0.5 * mw, base - mh),
            (cx + 0.5 * mw, base - mh),
            (cx + 0.46 * mw, base - 0.05 * mh),
            (cx + 0.38 * mw, base),
            (cx - 0.38 * mw, base),
            (cx - 0.46 * mw, base - 0.05 * mh),
        ]
    )
    cv.fill(body, "#2a8a8f", 1, shadow)
    cv.linear(
        body,
        [(0, "#ffffff", 0.0), (0.7, "#ffffff", 0.0), (1, "#000000", 0.18)],
        (cx - 0.5 * mw, 0),
        (cx + 0.5 * mw, 0),
    )
    cv.fill(cv.ellipse(cx, base - mh, 0.5 * mw, 0.08 * mh), "#1c5c61")
    cv.fill(cv.ellipse(cx, base - mh + 0.01 * mh, 0.44 * mw, 0.055 * mh), "#3b2418")
    for k in (-1, 0, 1):
        x = cx + 0.18 * k * mw
        cv.line(
            x,
            base - 1.15 * mh,
            x + 0.04 * mw,
            base - 1.45 * mh,
            "#ffffff",
            0.45,
            0.03 * mw,
        )


def mug_reference(cv: Canvas) -> None:
    cv.fill(cv.rect(0, 0, cv.w, cv.h), "#f7f7f5")
    mug_shape(cv, 0.47 * cv.w, 0.72 * cv.h, 0.36 * cv.w, 0.25)


def mug_on_table(cv: Canvas) -> None:
    """The Klein Edit result: the mug on a terrazzo table in morning light."""
    w, h = cv.w, cv.h
    table = 0.62 * h
    cv.linear(
        cv.rect(0, 0, w, table),
        [(0, "#e8d9c3", 1), (1, "#d6c2a6", 1)],
        (0, 0),
        (w, table),
    )
    cv.glow(0.1 * w, 0.25 * h, 0.9 * w, "#fff4dc", 0.55)
    Q.CGContextSaveGState(cv.ctx)
    Q.CGContextAddPath(cv.ctx, cv.rect(0, table, w, h - table))
    Q.CGContextClip(cv.ctx)
    terrazzo(cv)
    Q.CGContextRestoreGState(cv.ctx)
    cv.linear(
        cv.rect(0, table, w, h - table),
        [(0, "#000000", 0.18), (0.15, "#000000", 0.0)],
        (0, table),
        (0, h),
    )
    cv.fill(cv.rect(0, table - 0.004 * h, w, 0.008 * h), "#cdb796")
    cv.fill(cv.ellipse(0.58 * w, 0.8 * h, 0.3 * w, 0.03 * h), "#000000", 0.18)
    mug_shape(cv, 0.5 * w, 0.8 * h, 0.42 * w, 0.0)


def boat(cv: Canvas, t: float) -> None:
    """A paper boat in a rainy gutter at night, t in seconds."""
    w, h = cv.w, cv.h
    s = w / 704
    cv.linear(
        None,
        [(0, "#0a111d", 1), (0.4, "#18233a", 1), (1, "#0d1420", 1)],
        (0, 0),
        (0, h),
    )
    for x, y, r in (
        (0.18, 0.1, 0.5),
        (0.78, 0.18, 0.4),
        (0.5, 0.05, 0.3),
    ):  # street lights far away
        cv.glow(x * w, y * h, r * w, "#ffad5c", 0.5)
        cv.fill(cv.ellipse(x * w, y * h, 0.012 * w, 0.012 * w), "#ffe0b0")
    kerb = 0.5 * h
    cv.linear(
        cv.rect(0, 0.36 * h, w, kerb - 0.36 * h),
        [(0, "#1b2333", 1), (1, "#262f40", 1)],
        (0, 0.36 * h),
        (0, kerb),
    )
    cv.fill(cv.rect(0, kerb - 0.006 * h, w, 0.006 * h), "#59606f")
    water_top, water_bottom = kerb + 0.02 * h, 0.86 * h
    cv.fill(cv.rect(0, kerb, w, 0.02 * h), "#3c4352")
    cv.linear(
        cv.rect(0, water_top, w, water_bottom - water_top),
        [(0, "#20354d", 1), (1, "#0f1b2a", 1)],
        (0, water_top),
        (0, water_bottom),
    )
    for x, r in (
        (0.18, 0.5),
        (0.78, 0.4),
        (0.5, 0.3),
    ):  # the lights mirrored in the water, a little restless
        for k in range(10):
            y = water_top + (k + 1) * 0.026 * h
            half = (0.07 - 0.004 * k) * w * (1 + 0.25 * math.sin(4 * t + k + 3 * x))
            cv.line(
                x * w - half, y, x * w + half, y, "#ffb46a", 0.32 - 0.025 * k, 4 * s
            )
    cv.linear(
        cv.rect(0, water_bottom, w, h - water_bottom),
        [(0, "#1a1f2a", 1), (1, "#0b0e14", 1)],
        (0, water_bottom),
        (0, h),
    )
    # ripples where drops land
    rnd = random.Random(3)
    for _ in range(14):
        x, y, ph = (
            rnd.uniform(0, w),
            rnd.uniform(water_top + 0.02 * h, water_bottom - 0.02 * h),
            rnd.uniform(0, 1),
        )
        k = (t * 0.9 + ph) % 1.0
        cv.stroke(
            cv.ellipse(x, y, 0.05 * w * k, 0.012 * h * k),
            "#cfe3ff",
            0.5 * (1 - k),
            1.6 * s,
        )
    # the boat: drifts right and bobs
    bx = (0.28 + 0.06 * t) * w
    by = 0.69 * h + 0.006 * h * math.sin(2.4 * t)
    tilt = 0.04 * math.sin(1.7 * t)
    c = cv.ctx
    Q.CGContextSaveGState(c)
    Q.CGContextTranslateCTM(c, bx, by)
    Q.CGContextRotateCTM(c, tilt)
    u = 0.2 * w
    cv.fill(
        cv.path(
            [
                (-0.9 * u, 0.12 * u),
                (0.9 * u, 0.12 * u),
                (0.6 * u, 0.42 * u),
                (-0.6 * u, 0.42 * u),
            ]
        ),
        "#ffffff",
        0.12,
    )  # reflection
    cv.fill(
        cv.path(
            [(-u, -0.1 * u), (u, -0.1 * u), (0.62 * u, 0.22 * u), (-0.62 * u, 0.22 * u)]
        ),
        "#f4f1ea",
        1,
        0.5,
    )
    cv.fill(
        cv.path([(-0.55 * u, -0.1 * u), (0.0, -0.95 * u), (0.0, -0.1 * u)]),
        "#ebe6da",
        1,
        0.3,
    )
    cv.fill(
        cv.path([(0.0, -0.95 * u), (0.55 * u, -0.1 * u), (0.0, -0.1 * u)]),
        "#d4cebf",
        1,
        0.3,
    )
    cv.line(0, -0.95 * u, 0, -0.1 * u, "#bdb6a6", 0.9, 1.5 * s)
    Q.CGContextRestoreGState(c)
    # rain
    rnd = random.Random(1)
    for _ in range(140):
        x0, y0, speed = (
            rnd.uniform(-0.1, 1.1) * w,
            rnd.uniform(0, 1),
            rnd.uniform(0.9, 1.4),
        )
        y = ((y0 + speed * t * 0.9) % 1.0) * h
        x = x0 - 0.05 * y
        cv.line(x, y, x - 0.008 * w, y + 0.03 * h, "#d6e6ff", 0.22, 1.4 * s)


# ---------- audio and video ----------


def run(cmd: list[str], **kw: Any) -> None:
    subprocess.run(cmd, check=True, **kw)


def ffmpeg(*args: str) -> None:
    run(["ffmpeg", "-v", "error", "-y", *args])


def duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out.strip())


def render_video(
    path: Path, w: int, h: int, scene: Callable[[Canvas, float], None], audio: list[str]
) -> None:
    """Draw FRAMES frames of a scene and encode them with an audio track (ffmpeg input arguments)."""
    enc = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgra",
            "-s",
            f"{w}x{h}",
            "-r",
            str(FPS),
            "-i",
            "-",
            *audio,
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-t",
            f"{FRAMES / FPS:.3f}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "20",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(path),
        ],
        stdin=subprocess.PIPE,
    )
    assert enc.stdin is not None
    for n in range(FRAMES):
        cv = Canvas(w, h)
        scene(cv, n / FPS)
        enc.stdin.write(cv.bgra())
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit(f"ffmpeg could not encode {path}")


RAIN = [
    "-f",
    "lavfi",
    "-i",
    "anoisesrc=color=pink:amplitude=0.25:seed=3,highpass=f=300,lowpass=f=6000,volume=0.8",
]
WAVES = [
    "-f",
    "lavfi",
    "-i",
    "anoisesrc=color=brown:amplitude=0.6:seed=8,lowpass=f=900,tremolo=f=0.18:d=0.7,volume=1.4",
]


def speak(text: str, path: Path, rate: int) -> None:
    """A voice take with macOS `say` (Samantha when the Mac has it), as 44.1 kHz mono WAV."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "take.aiff"
        voices = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, check=True
        ).stdout
        voice = ["-v", "Samantha"] if "Samantha" in voices else []
        run(["say", *voice, "-r", str(rate), "-o", str(aiff), text])
        ffmpeg("-i", str(aiff), "-ar", "44100", "-ac", "1", str(path))


NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def freq(name: str) -> float:
    """The frequency of a note like "C4" or "A3"."""
    return 440.0 * 2 ** ((NOTE[name[0]] + 12 * (int(name[1]) + 1) - 69) / 12)


def piano(
    path: Path, seconds: float, bpm: float, chords: list[list[str]], seed: int
) -> None:
    """A soft piano take: one arpeggiated chord a bar, a low root under it and a simple echo, 44.1 kHz stereo."""
    rate = 44100
    n = int(seconds * rate)
    left, right = array("f", bytes(4 * n)), array("f", bytes(4 * n))
    eighth = 60 / bpm / 2
    rnd = random.Random(seed)
    tables: dict[tuple[str, float], list[float]] = {}

    def tone(name: str, length: float, level: float) -> list[float]:
        key = (name, length)
        if key not in tables:
            f, m = freq(name), int(length * rate)
            tables[key] = [
                level
                * min(1.0, i / 220)
                * math.exp(-i / (rate * 0.9))
                * (
                    math.sin(2 * math.pi * f * i / rate)
                    + 0.35 * math.sin(4 * math.pi * f * i / rate)
                    + 0.12 * math.sin(6 * math.pi * f * i / rate)
                )
                for i in range(m)
            ]
        return tables[key]

    def add(start: float, samples: list[float], pan: float) -> None:
        i0 = int(start * rate)
        gl, gr = 1 - 0.5 * pan, 0.5 + 0.5 * pan
        for i, v in enumerate(samples[: max(0, n - i0)]):
            left[i0 + i] += gl * v
            right[i0 + i] += gr * v

    bar, t = 0, 0.0
    while t < seconds - 1:
        chord = chords[bar % len(chords)]
        add(t, tone(chord[0][0] + "2", 3.0, 0.22), 0.4)
        pattern = [0, 1, 2, 3, 2, 1, 2, 3]
        for k, idx in enumerate(pattern):
            name = chord[idx]
            add(
                t + k * eighth + rnd.uniform(0, 0.012),
                tone(name, 2.2, 0.16),
                0.2 + 0.6 * (idx / 3),
            )
        t += 8 * eighth
        bar += 1
    delay = int(0.21 * rate)
    for i in range(
        delay, n
    ):  # a soft room: one feedback echo, crossed between the channels
        left[i] += 0.28 * right[i - delay]
        right[i] += 0.28 * left[i - delay]
    fade = int(2.5 * rate)
    for i in range(fade):
        g = i / fade
        left[n - 1 - i] *= g
        right[n - 1 - i] *= g
    peak = max(max(abs(v) for v in left), max(abs(v) for v in right)) or 1.0
    pcm = array(
        "h", (int(32000 * v / peak * 0.85) for pair in zip(left, right) for v in pair)
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


# ---------- jobs ----------


def stamp(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def make_id(t: datetime, salt: str) -> str:
    return t.strftime("%Y%m%d-%H%M%S-") + hashlib.sha1(salt.encode()).hexdigest()[:4]


def params_for(tool_id: str, given: dict[str, str]) -> dict[str, Any]:
    """Every field that is not a file, as the Create form sends it: the given text, else the default."""
    out: dict[str, Any] = {}
    for f in tools.BY_ID[tool_id]["fields"]:
        if f["type"] in ("file", "files"):
            continue
        if f["name"] in given:
            out[f["name"]] = given[f["name"]]
        elif f.get("default") is None:
            out[f["name"]] = None
        elif isinstance(f["default"], bool):
            out[f["name"]] = "true" if f["default"] else "false"
        else:
            out[f["name"]] = str(f["default"])
    return out


def bar(desc: str, total: int, seconds: float) -> str:
    """A finished tqdm bar line, as the model CLIs print it."""
    rate = seconds / total
    return f"{desc}100%|██████████| {total}/{total} [{int(seconds) // 60:02d}:{int(seconds) % 60:02d}<00:00, {rate:5.2f}s/it]"


def write_job(
    out: Path,
    order: int,
    tool_id: str,
    created: datetime,
    elapsed: float,
    given: dict[str, str],
    outputs: list[str],
    log: list[str],
    peak_gb: float,
    notes: dict[str, str] | None = None,
    files: dict[str, list[str]] | None = None,
    chat: str | None = None,
) -> str:
    """Write one finished job's job.json and job.log; its media files are already in out/<id>/."""
    job_id = out.name
    started = created + timedelta(seconds=2)
    log = [
        *log,
        f"{elapsed + 0.4:>14.2f} real {elapsed * 0.6:>14.2f} user {elapsed * 0.1:>14.2f} sys",
        PEAK_LINE.format(int(peak_gb * 2**30)),
    ]
    (out / "job.log").write_text("\n".join(log) + "\n")
    job = {
        "id": job_id,
        "tool": tool_id,
        "tool_label": tools.BY_ID[tool_id]["label"],
        "status": "done",
        "created": stamp(created),
        "started": stamp(started),
        "finished": stamp(started + timedelta(seconds=elapsed)),
        "elapsed": elapsed,
        "params": params_for(tool_id, given),
        "files": files or {},
        "outputs": [
            {
                "url": f"/files/{job_id}/{n}",
                "kind": KINDS[Path(n).suffix],
                "name": n,
                "note": (notes or {}).get(n, ""),
            }
            for n in outputs
        ],
        "peak_gb": peak_gb,
        "error": None,
        "log_tail": "\n".join(log[-25:]),
        "order": order,
    }
    if chat:
        job["chat"] = chat
    if notes:
        (out / "notes.json").write_text(json.dumps(notes, indent=1))
    (out / "job.json").write_text(json.dumps(job, indent=1))
    return job_id


def cmd(*parts: str) -> str:
    return "$ " + " ".join(parts)


def make_jobs(out: Path, now: datetime, agent_chat: str) -> dict[str, str]:
    """The finished jobs, oldest first. Returns their ids by a short name."""
    ids: dict[str, str] = {}
    mflux = f"{HOME}/mlx-media/mflux/.venv/bin"
    ltx = f"{HOME}/mlx-media/venv-ltx/bin/ltx-2-mlx"
    model = f"{HOME}/mlx-media/models/ltx-2.5-mlx-q8"
    plan = [  # name, minutes before now
        ("mountains", 26 * 60 + 12),
        ("dunes", 25 * 60 + 40),
        ("mug", 24 * 60 + 5),
        ("piano", 22 * 60 + 30),
        ("boat", 21 * 60 + 50),
        ("voice", 3 * 60 + 15),
        ("lighthouse", 2 * 60 + 40),
        ("lighthouse_video", 2 * 60 + 20),
        ("mix", 95),
    ]
    when = {name: now - timedelta(minutes=m) for name, m in plan}

    def folder(name: str) -> Path:
        d = out / make_id(when[name], name)
        d.mkdir(parents=True)
        ids[name] = d.name
        return d

    order = 0

    def next_order() -> int:
        nonlocal order
        order += 1
        return order

    # MOSS-Image: mountains, 1024x1024, 50 steps (the same settings as the live MOSS job, so its time left shows)
    d = folder("mountains")
    cv = Canvas(1024, 1024)
    mountains(cv)
    cv.save(d / "image.png")
    write_job(
        d,
        next_order(),
        "image.moss",
        when["mountains"],
        236.4,
        {"prompt": MOUNTAINS, "size": "1024x1024", "seed": "1838"},
        ["image.png"],
        [
            cmd(
                f"{mflux}/mflux-generate-moss-image",
                "--prompt",
                "...",
                "--width",
                "1024",
                "--height",
                "1024",
                "--steps",
                "50",
            ),
            bar("", 50, 221.0),
            "Saved image.png",
        ],
        22.4,
    )
    # FLUX.3 Klein: dunes, 1344x768
    d = folder("dunes")
    cv = Canvas(1344, 768)
    dunes(cv)
    cv.save(d / "image.png")
    write_job(
        d,
        next_order(),
        "image.klein",
        when["dunes"],
        38.7,
        {"prompt": DUNES, "size": "1344x768", "seed": "90211"},
        ["image.png"],
        [
            cmd(
                f"{mflux}/mflux-generate-flux3",
                "--prompt",
                "...",
                "--width",
                "1344",
                "--height",
                "768",
                "--steps",
                "16",
            ),
            bar("", 16, 31.5),
            "Saved image.png",
        ],
        13.8,
    )
    # Klein Edit: the mug on a terrazzo table, from two references
    d = folder("mug")
    for name, size, scene in (
        ("in_references_0.png", 1024, terrazzo),
        ("in_references_1.png", 1024, mug_reference),
    ):
        cv = Canvas(size, size)
        scene(cv)
        cv.save(d / name)
    cv = Canvas(768, 1344)
    mug_on_table(cv)
    cv.save(d / "image.png")
    write_job(
        d,
        next_order(),
        "image.klein_edit",
        when["mug"],
        64.2,
        {"prompt": MUG, "size": "768x1344"},
        ["image.png"],
        [
            cmd(
                f"{mflux}/mflux-generate-flux3-edit",
                "--prompt",
                "...",
                "--image-paths",
                "in_references_0.png",
                "in_references_1.png",
            ),
            bar("", 16, 52.0),
            "Saved image.png",
        ],
        14.6,
        files={"references": ["in_references_0.png", "in_references_1.png"]},
    )
    # ACE-Step: two piano takes
    d = folder("piano")
    takes = [
        (
            72,
            [
                ["C4", "E4", "G4", "C5"],
                ["A3", "C4", "E4", "A4"],
                ["F3", "A3", "C4", "F4"],
                ["G3", "B3", "D4", "G4"],
            ],
            4127,
        ),
        (
            72,
            [
                ["A3", "C4", "E4", "A4"],
                ["F3", "A3", "C4", "F4"],
                ["C4", "E4", "G4", "C5"],
                ["G3", "B3", "D4", "G4"],
            ],
            4128,
        ),
    ]
    notes = {}
    for k, (bpm, chords, seed) in enumerate(takes, start=1):
        piano(d / f"take{k}.wav", 30.0, bpm, chords, seed)
        notes[f"take{k}.wav"] = f"seed {seed}, bpm {bpm}, key C major"
    write_job(
        d,
        next_order(),
        "music.ace",
        when["piano"],
        41.6,
        {
            "caption": PIANO,
            "instrumental": "true",
            "duration": "30",
            "bpm": "72",
            "key": "C major",
            "takes": "2",
            "seed": "4127",
        },
        ["take1.wav", "take2.wav"],
        [
            cmd(
                f"{HOME}/mlx-media/ace-step/.venv/bin/python",
                "app/runners/ace.py",
                "music_job.json",
            ),
            "[initialize_service] loading models",
            "Phase 1: planning",
            "Phase 2: audio codes",
            "Starting generation",
            "Decoding latents with VAE",
            "2 takes written",
        ],
        22.9,
        notes,
    )
    # Text to Video: the paper boat in the rain
    d = folder("boat")
    render_video(d / "video.mp4", 704, 1280, boat, RAIN)
    write_job(
        d,
        next_order(),
        "video.t2v",
        when["boat"],
        128.3,
        {
            "prompt": BOAT,
            "size": "704x1280",
            "seconds": "5",
            "audio": "true",
            "seed": "52817",
        },
        ["video.mp4"],
        [
            cmd(
                ltx,
                "generate",
                "--distilled",
                "--model",
                model,
                "--prompt=...",
                "-W",
                "704",
                "-H",
                "1280",
            ),
            "[Loading text encoder...]",
            "[estimate] denoising stage 1: 8 steps x 1 passes over 3520 video tokens",
            bar("Denoising: ", 8, 31.2),
            "[estimate] denoising stage 2: 3 steps x 1 passes over 14080 video tokens",
            bar("Denoising: ", 3, 58.1),
            "Saved to: video.mp4",
        ],
        34.6,
    )
    # Fish Drama 3: two voice takes
    d = folder("voice")
    spoken = "The lighthouse keeper climbed the stairs one last time. Below him the sea was calm, and the first stars came out over the water."
    notes = {}
    for k, (rate, seed) in enumerate(((168, 31907), (156, 32007)), start=1):
        speak(spoken, d / f"take{k}.wav", rate)
        notes[f"take{k}.wav"] = (
            f"seed {seed}, {duration(d / f'take{k}.wav'):.1f} s, checked ok. Heard: {spoken}"
        )
    log = [
        cmd(
            f"{HOME}/mlx-media/venv-drama/bin/python",
            "app/runners/drama3.py",
            "drama_job.json",
        )
    ]
    log += [
        f"take {k} seed {s}: {duration(d / f'take{k}.wav'):.1f} s, ok | {spoken}"
        for k, s in ((1, 31907), (2, 32007))
    ]
    log.append("2/2 takes passed the check; peak 37.8 GB")
    write_job(
        d,
        next_order(),
        "voice.drama3",
        when["voice"],
        27.9,
        {"text": VOICE, "takes": "2"},
        ["take1.wav", "take2.wav"],
        log,
        38.6,
        notes,
    )
    # FLUX.3 Klein: the lighthouse, queued by the agent chat
    d = folder("lighthouse")
    cv = Canvas(768, 1344)
    lighthouse(cv)
    cv.save(d / "image.png")
    write_job(
        d,
        next_order(),
        "image.klein",
        when["lighthouse"],
        41.2,
        {"prompt": LIGHTHOUSE, "size": "768x1344", "seed": "77031"},
        ["image.png"],
        [
            cmd(
                f"{mflux}/mflux-generate-flux3",
                "--prompt",
                "...",
                "--width",
                "768",
                "--height",
                "1344",
                "--steps",
                "16",
            ),
            bar("", 16, 33.9),
            "Saved image.png",
        ],
        13.9,
        chat=agent_chat,
    )
    # Image to Video: the lighthouse with a moving beam
    d = folder("lighthouse_video")
    shutil.copyfile(out / ids["lighthouse"] / "image.png", d / "in_image_0.png")
    render_video(d / "video.mp4", 704, 1280, lighthouse, WAVES)
    write_job(
        d,
        next_order(),
        "video.i2v",
        when["lighthouse_video"],
        131.0,
        {
            "prompt": LIGHTHOUSE_VIDEO,
            "size": "704x1280",
            "seconds": "5",
            "audio": "true",
            "seed": "6150",
        },
        ["video.mp4"],
        [
            cmd(
                ltx,
                "generate",
                "--distilled",
                "--model",
                model,
                "--image",
                "start.png",
                "--prompt=...",
                "-W",
                "704",
                "-H",
                "1280",
            ),
            "[Loading text encoder...]",
            "[estimate] denoising stage 1: 8 steps x 1 passes over 3520 video tokens",
            bar("Denoising: ", 8, 32.0),
            "[estimate] denoising stage 2: 3 steps x 1 passes over 14080 video tokens",
            bar("Denoising: ", 3, 59.4),
            "Saved to: video.mp4",
        ],
        34.8,
        files={"image": ["in_image_0.png"]},
    )
    # Mix: the boat clip with the first piano take under it
    d = folder("mix")
    shutil.copyfile(out / ids["boat"] / "video.mp4", d / "in_video_0.mp4")
    shutil.copyfile(out / ids["piano"] / "take1.wav", d / "in_music_0.wav")
    ffmpeg(
        "-i",
        str(d / "in_video_0.mp4"),
        "-i",
        str(d / "in_music_0.wav"),
        "-filter_complex",
        "[1:a]volume=0.35,afade=t=out:st=3.5:d=1.5[m];[0:a][m]amix=inputs=2:duration=first[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(d / "mixed.mp4"),
    )
    write_job(
        d,
        next_order(),
        "edit.mix",
        when["mix"],
        3.1,
        {},
        ["mixed.mp4"],
        [
            cmd(
                "ffmpeg",
                "-i",
                "in_video_0.mp4",
                "-i",
                "in_music_0.wav",
                "...",
                "mixed.mp4",
            )
        ],
        0.3,
        {"mixed.mp4": "no voice in the video: music at -16 LUFS; final mix -14 LUFS"},
        files={"video": ["in_video_0.mp4"], "music": ["in_music_0.wav"]},
    )
    return ids


# ---------- chats ----------


def message(role: str, content: str, at: datetime, **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": role, "content": content}
    if role == "user":
        msg["images"] = []
    else:
        msg.setdefault("reasoning", "")
    msg.update(extra)
    msg["time"] = stamp(at)
    return msg


def save_chat(out: Path, chat: dict[str, Any]) -> None:
    d = out / "chats" / chat["id"]
    d.mkdir(parents=True)
    (d / "chat.json").write_text(json.dumps(chat, ensure_ascii=False, indent=1))


def workstation_chat(out: Path, at: datetime) -> str:
    chat_id = make_id(at, "workstation chat")
    music_caption = "Calm solo piano, slow arpeggios, soft rain in the background, warm and a little melancholic, late night city"
    msgs = [
        message(
            "user",
            "I want a short vertical clip: a paper boat in a rainy street at night, close to the ground. "
            "Can you write the prompt for Text to Video?",
            at,
        ),
        message(
            "assistant",
            "Here is a prompt for **Text to Video**. It keeps the camera low and moving with the boat, and it names "
            "every sound, because LTX-2.5 makes the audio too.\n\n"
            f"```prompt video.t2v\n{BOAT}\n```\n\n"
            "- For a calmer clip, leave out the passing car.\n"
            "- For 8 s, add one more action, for example the boat bumping into a leaf.",
            at + timedelta(seconds=24),
            reasoning="The user wants an LTX-2.5 prompt. The tool advice: 4-8 sentences with the shot, the place and "
            "light, the action, the camera and every sound. Keep it at street level and say how the frame ends.",
            finish="stop",
            stats={
                "seconds": 23.6,
                "generation_tokens": 241,
                "generation_tps": 14.2,
                "prompt_tokens": 1830,
            },
        ),
        message(
            "user",
            "Nice. And a calm piano track to go under it?",
            at + timedelta(minutes=3),
        ),
        message(
            "assistant",
            "Use this caption in **ACE-Step 1.5** with Instrumental on and 30 s. Mix it under the clip afterwards.\n\n"
            f"```prompt music.ace caption\n{music_caption}\n```",
            at + timedelta(minutes=3, seconds=15),
            reasoning="A caption for ACE-Step: genre, mood, instruments, tempo feel and use.",
            finish="stop",
            stats={
                "seconds": 11.8,
                "generation_tokens": 96,
                "generation_tps": 14.6,
                "prompt_tokens": 2214,
            },
        ),
    ]
    save_chat(
        out,
        {
            "id": chat_id,
            "title": "Paper boat in the rain: video prompt",
            "mode": "workstation",
            "created": stamp(at),
            "updated": msgs[-1]["time"],
            "messages": msgs,
            "effort": "low",
        },
    )
    return chat_id


def general_chat(out: Path, at: datetime) -> str:
    chat_id = make_id(at, "general chat")
    msgs = [
        message("user", "Which aspect ratio should I use for a Short, and why?", at),
        message(
            "assistant",
            "Use **9:16** (for example 1080x1920). Shorts fill the phone screen in that shape, so a 9:16 video uses the "
            "whole screen. A 16:9 video shows with bars above and below it and looks small.",
            at + timedelta(seconds=9),
            finish="stop",
            stats={"seconds": 8.7, "generation_tokens": 58, "generation_tps": 15.1},
        ),
    ]
    save_chat(
        out,
        {
            "id": chat_id,
            "title": "Aspect ratio for Shorts",
            "mode": "general",
            "created": stamp(at),
            "updated": msgs[-1]["time"],
            "messages": msgs,
        },
    )
    return chat_id


def tool_message(
    call_id: str,
    name: str,
    label: str,
    content: str,
    describe: str,
    at: datetime,
    risk: int,
    **extra: Any,
) -> dict[str, Any]:
    """A tool result as app/chat.py Turn.result saves it."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "label": label,
        "status": "ok",
        "content": content,
        "describe": describe,
        "files": [],
        "seconds": extra.pop("seconds", 0.0),
        "approval": extra.pop("approval", None),
        "risk": risk,
        "network": False,
        **extra,
        "time": stamp(at),
    }


def agent_chat(out: Path, at: datetime, chat_id: str, lighthouse: str) -> None:
    """An agent chat whose first answer queued and waited for the lighthouse image (scripts/screenshots.py adds the
    second answer, which waits for an approval)."""
    ask = "Make a tall paper-cut image of a lighthouse at dusk for a Short, and tell me when it is ready."
    plan1 = [
        {
            "step": "Queue a FLUX.3 Klein image of the lighthouse",
            "status": "in_progress",
        },
        {"step": "Wait for the image", "status": "pending"},
    ]
    plan2 = [
        {"step": "Queue a FLUX.3 Klein image of the lighthouse", "status": "done"},
        {"step": "Wait for the image", "status": "done"},
    ]
    t = at + timedelta(seconds=6)
    describe = (
        f"Queues a job for image.klein (prompt={LIGHTHOUSE[:80]!r}, size='768x1344'). It uses this Mac for "
        "minutes."
    )
    status = (
        f"job {lighthouse}: FLUX.3 Klein, done\nelapsed: 41.2 s\noutput: image.png (image), file reference "
        f"library:{lighthouse}/image.png"
    )
    msgs = [
        message("user", ask, at),
        message(
            "assistant",
            "",
            t,
            reasoning="Two steps: queue the image with FLUX.3 Klein at 768x1344, then wait for it.",
            finish="tool_calls",
            stats={"seconds": 6.1, "generation_tokens": 74},
            tool_calls=[
                {
                    "id": "call_demo01",
                    "name": "update_plan",
                    "arguments": {"items": plan1},
                },
                {
                    "id": "call_demo02",
                    "name": "create_job",
                    "arguments": {
                        "tool": "image.klein",
                        "fields": {"prompt": LIGHTHOUSE, "size": "768x1344"},
                    },
                },
            ],
        ),
        tool_message(
            "call_demo01",
            "update_plan",
            "Plan",
            "Plan saved: 0 of 2 done.",
            "Shows the plan.",
            t,
            0,
            plan=plan1,
        ),
        tool_message(
            "call_demo02",
            "create_job",
            "Queue a media job",
            f"Queued job {lighthouse} (FLUX.3 Klein). It runs in the background.",
            describe,
            t + timedelta(seconds=9),
            2,
            approval="once",
            job=lighthouse,
            seconds=0.1,
        ),
        message(
            "assistant",
            "",
            t + timedelta(seconds=12),
            finish="tool_calls",
            stats={"seconds": 2.4, "generation_tokens": 31},
            tool_calls=[
                {
                    "id": "call_demo03",
                    "name": "wait_for_job",
                    "arguments": {"job_id": lighthouse, "seconds": 120},
                }
            ],
        ),
        tool_message(
            "call_demo03",
            "wait_for_job",
            "Wait for a job",
            status,
            f"Waits up to 120 s for job {lighthouse}.",
            t + timedelta(seconds=56),
            0,
            seconds=43.8,
        ),
        message(
            "assistant",
            "",
            t + timedelta(seconds=58),
            finish="tool_calls",
            stats={"seconds": 1.9, "generation_tokens": 40},
            tool_calls=[
                {
                    "id": "call_demo04",
                    "name": "update_plan",
                    "arguments": {"items": plan2},
                }
            ],
        ),
        tool_message(
            "call_demo04",
            "update_plan",
            "Plan",
            "Plan saved: 2 of 2 done.",
            "Shows the plan.",
            t + timedelta(seconds=58),
            0,
            plan=plan2,
        ),
        message(
            "assistant",
            f"The image is ready: **image.png** in job {lighthouse} (768x1344, 41 s). It is in the "
            "Library, and you can send it to Image to Video from there.",
            t + timedelta(seconds=63),
            finish="stop",
            stats={
                "seconds": 63.0,
                "steps": 4,
                "tool_calls": 4,
                "generation_tokens": 188,
                "generation_tps": 13.9,
            },
        ),
    ]
    tools_on = [
        "update_plan",
        "list_workstation_tools",
        "job_status",
        "list_jobs",
        "wait_for_job",
        "search_library",
        "create_job",
        "cancel_job",
        "transcribe_audio",
        "web_search",
        "fetch_url",
        "media_info",
        "view_image",
    ]
    save_chat(
        out,
        {
            "id": chat_id,
            "title": "Lighthouse image and video",
            "mode": "agent",
            "created": stamp(at),
            "updated": msgs[-1]["time"],
            "messages": msgs,
            "effort": "low",
            "agent": {
                "tools": tools_on,
                "folder": None,
                "grants": [],
                "tainted": False,
            },
        },
    )


def agent_turn(ids: dict[str, str]) -> tuple[str, list[Any]]:
    """The second agent answer: its user text and the model steps (the STEPS format of tests/fake_llm_worker.py).
    It ends with a create_job call, so the chat shows an approval card."""
    items = [
        "Check what is running and queued",
        "Check the lighthouse image",
        "Queue Image to Video with it",
    ]

    def plan(*states: str) -> dict[str, Any]:
        return {
            "name": "update_plan",
            "arguments": {
                "items": [{"step": s, "status": st} for s, st in zip(items, states)]
            },
        }

    text = "Now animate it into a 5 second vertical video with the sound of the sea. Check the queue first."
    prompt = "The beam sweeps slowly across the sea and the stars twinkle. The camera holds still. Sound: soft waves."
    steps = [
        {
            "think": "Three steps: look at the queue, check the lighthouse image, then queue Image to Video with it.",
            "calls": [
                plan("in_progress", "pending", "pending"),
                {"name": "list_jobs", "arguments": {"limit": 5}},
            ],
        },
        {
            "calls": [
                plan("done", "in_progress", "pending"),
                {"name": "job_status", "arguments": {"job_id": ids["lighthouse"]}},
            ]
        },
        {
            "text": "MOSS-Image is running and two jobs wait, so the video starts after them.",
            "calls": [
                plan("done", "done", "in_progress"),
                {
                    "name": "create_job",
                    "arguments": {
                        "tool": "video.i2v",
                        "fields": {
                            "image": f"library:{ids['lighthouse']}/image.png",
                            "prompt": prompt,
                        },
                    },
                },
            ],
        },
        "The video is queued.",
    ]
    return text, steps


# ---------- main ----------


def build(out: Path, now: datetime | None = None) -> dict[str, Any]:
    """Fill `out` (empty or missing). Returns the ids of the jobs and the chats."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise SystemExit(f"{out} is not empty: give an empty or new folder.")
    now = (now or datetime.now()).replace(microsecond=0)
    agent_at = now - timedelta(minutes=2 * 60 + 41)
    agent_id = make_id(agent_at, "agent chat")
    ids = make_jobs(out, now, agent_id)
    chats = {
        "general": general_chat(out, now - timedelta(days=1, minutes=20)),
        "workstation": workstation_chat(out, now - timedelta(hours=22, minutes=5)),
        "agent": agent_id,
    }
    agent_chat(out, agent_at, agent_id, ids["lighthouse"])
    return {"jobs": ids, "chats": chats}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    made = build(Path(sys.argv[1]).expanduser().resolve())
    print(f"{len(made['jobs'])} jobs and {len(made['chats'])} chats in {sys.argv[1]}")


if __name__ == "__main__":
    main()
