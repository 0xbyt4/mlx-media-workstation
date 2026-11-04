"""Draw the app icon: the sidebar's brand mark (four tiles in the media colours of web/css/tokens.css) on a macOS
icon plate. Writes assets/icon.png (1024 px, the window's Dock icon) and assets/AppIcon.icns (the .app bundles).

Uses AppKit, which the app's environment has through pywebview, and iconutil. Run it from the repository folder:

  .venv/bin/python scripts/make_icon.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSGradient,
    NSGraphicsContext,
    NSPNGFileType,
    NSShadow,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# the media colours (tokens.css, light theme): image, voice, music, video
TILES = ("#a85c17", "#ad3765", "#6546c2", "#0b7a80")
# Apple's icon grid on a 1024 canvas: an 824 px plate with a 185 px corner radius, room for its shadow around it
PLATE, PLATE_RADIUS = 824, 185.4
# the brand mark's proportions (shell.css .brand-mark): 4 px padding and a 2 px gap on a 26 px mark
PADDING, GAP, TILE_RADIUS = 128, 56, 64
# smaller icons show the tiles only: a glyph of a few pixels would be noise
GLYPH_MIN = 64
# iconutil's file names and sizes
ICONSET = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def color(hex_rgb: str, alpha: float = 1.0) -> NSColor:
    r, g, b = (int(hex_rgb[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return NSColor.colorWithDeviceRed_green_blue_alpha_(r, g, b, alpha)


def lighter(hex_rgb: str, amount: float) -> NSColor:
    """The colour mixed with white: the top of a tile, so it reads as lit from above."""
    r, g, b = (int(hex_rgb[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return NSColor.colorWithDeviceRed_green_blue_alpha_(
        r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount, 1.0
    )


def glyph(kind: int, x: float, y: float, k: float) -> None:
    """The white sign of one tile, drawn on a 256-unit tile whose lower left corner is (x, y) on the 1024 canvas:
    0 image (a sun over two hills), 1 voice (a waveform), 2 music (two beamed notes), 3 video (a play sign)."""

    def pt(u: float, v: float):
        return NSMakePoint((x + u) * k, (y + v) * k)

    def box(u: float, v: float, w: float, h: float):
        return NSMakeRect((x + u) * k, (y + v) * k, w * k, h * k)

    def shape(*points: tuple[float, float]) -> NSBezierPath:
        path = NSBezierPath.bezierPath()
        path.moveToPoint_(pt(*points[0]))
        for u, v in points[1:]:
            path.lineToPoint_(pt(u, v))
        path.closePath()
        path.setLineJoinStyle_(1)  # round
        path.setLineWidth_(14 * k)
        return path

    color("#ffffff").setFill()
    color("#ffffff").setStroke()
    if kind == 0:
        NSBezierPath.bezierPathWithOvalInRect_(box(150, 148, 48, 48)).fill()
        hills = shape((52, 66), (106, 146), (140, 104), (160, 126), (206, 66))
        hills.fill()
        hills.stroke()
    elif kind == 1:
        for u, h in ((62, 52), (92, 100), (122, 140), (152, 100), (182, 52)):
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                box(u, 128 - h / 2, 18, h), 9 * k, 9 * k
            ).fill()
    elif kind == 2:
        NSBezierPath.bezierPathWithOvalInRect_(box(58, 62, 62, 46)).fill()
        NSBezierPath.bezierPathWithOvalInRect_(box(138, 78, 62, 46)).fill()
        NSBezierPath.fillRect_(box(106, 86, 14, 94))
        NSBezierPath.fillRect_(box(186, 102, 14, 94))
        shape((106, 168), (200, 184), (200, 210), (106, 194)).fill()
    else:
        play = shape((98, 70), (98, 186), (194, 128))
        play.fill()
        play.stroke()


def draw(size: int) -> NSBitmapImageRep:
    """The icon at size x size pixels: everything is laid out on a 1024 canvas and scaled."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    rep.setSize_(NSMakeSize(size, size))
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setImageInterpolation_(3)  # high
    ctx.setCompositingOperation_(NSCompositingOperationSourceOver)
    k = size / 1024

    def rect(x: float, y: float, w: float, h: float):
        return NSMakeRect(x * k, y * k, w * k, h * k)

    # the plate, with a soft shadow under it; AppKit's y axis points up
    edge = (1024 - PLATE) / 2
    plate = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect(edge, edge, PLATE, PLATE), PLATE_RADIUS * k, PLATE_RADIUS * k
    )
    NSGraphicsContext.saveGraphicsState()
    shadow = NSShadow.alloc().init()
    shadow.setShadowOffset_(NSMakeSize(0, -10 * k))
    shadow.setShadowBlurRadius_(20 * k)
    shadow.setShadowColor_(color("#161b26", 0.28))
    shadow.set()
    color("#ffffff").setFill()
    plate.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient.alloc().initWithStartingColor_endingColor_(
        color("#ffffff"), color("#eceef3")
    ).drawInBezierPath_angle_(plate, -90)
    color("#161b26", 0.10).setStroke()
    plate.setLineWidth_(max(1.0, 2 * k))
    plate.stroke()

    # the four tiles: image and voice on top, music and video below, as in the sidebar
    tile = (PLATE - 2 * PADDING - GAP) / 2
    left, top = edge + PADDING, edge + PLATE - PADDING - tile
    spots = (
        (left, top),
        (left + tile + GAP, top),
        (left, top - tile - GAP),
        (left + tile + GAP, top - tile - GAP),
    )
    for hex_rgb, (x, y) in zip(TILES, spots, strict=True):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect(x, y, tile, tile), TILE_RADIUS * k, TILE_RADIUS * k
        )
        NSGradient.alloc().initWithStartingColor_endingColor_(
            lighter(hex_rgb, 0.14), color(hex_rgb)
        ).drawInBezierPath_angle_(path, -90)
        if size >= GLYPH_MIN:
            glyph(TILES.index(hex_rgb), x, y, k)
    NSGraphicsContext.restoreGraphicsState()
    return rep


def save(rep: NSBitmapImageRep, path: Path) -> None:
    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    if not data.writeToFile_atomically_(str(path), True):
        sys.exit(f"Could not write {path}")


def main() -> None:
    if shutil.which("iconutil") is None:
        sys.exit("iconutil is missing (it comes with macOS).")
    ASSETS.mkdir(exist_ok=True)
    save(draw(1024), ASSETS / "icon.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for name, size in ICONSET.items():
            save(draw(size), iconset / name)
        subprocess.run(
            [
                "iconutil",
                "-c",
                "icns",
                "-o",
                str(ASSETS / "AppIcon.icns"),
                str(iconset),
            ],
            check=True,
        )
    print(f"Wrote {ASSETS / 'icon.png'} and {ASSETS / 'AppIcon.icns'}")


if __name__ == "__main__":
    main()
