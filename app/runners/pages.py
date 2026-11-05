"""Page images for the Read Text (OCR) job (runs in the app's own Python: macOS Quartz reads PDF and image files).

A PDF page is drawn at the job's DPI on white; an image is turned upright by its EXIF orientation and laid on white
(the OCR model drops alpha). The long side of a page image is at most MAX_SIDE pixels, the largest image the OCR model
card uses. Each page goes to the "image" path the job gives it (app/tools.py names them in_page_NNNN.png: an in_*
name, so never an output).

  python pages.py job.json        the "source" file's "pages" ({"page": number from 1, "image": path}), at "dpi"
  python pages.py --count FILE    prints the number of pages of FILE (1 for an image); exit code 1 when unreadable
"""

import json
import sys
from pathlib import Path

import Quartz
from Foundation import NSURL

MAX_SIDE = 2880
PDF_MAGIC = b"%PDF-"


def is_pdf(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(1024).lstrip()[:5] == PDF_MAGIC


def url(path: Path):
    return NSURL.fileURLWithPath_(str(path))


def open_pdf(path: Path):
    """The PDF document, or SystemExit with the reason."""
    doc = Quartz.CGPDFDocumentCreateWithURL(url(path))
    if doc is None:
        raise SystemExit("The PDF file could not be read.")
    if not Quartz.CGPDFDocumentIsUnlocked(doc):
        raise SystemExit("The PDF file is protected with a password.")
    return doc


def open_image(path: Path):
    """The image source with at least one image, or SystemExit."""
    src = Quartz.CGImageSourceCreateWithURL(url(path), None)
    if src is None or Quartz.CGImageSourceGetCount(src) < 1:
        raise SystemExit("The file could not be read as an image or a PDF.")
    return src


def count(path: Path) -> int:
    if is_pdf(path):
        return Quartz.CGPDFDocumentGetNumberOfPages(open_pdf(path))
    open_image(path)
    return 1


def canvas(w: int, h: int):
    """A white sRGB bitmap of w x h pixels."""
    space = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, space, Quartz.kCGImageAlphaNoneSkipLast
    )
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    return ctx


def save_png(ctx, path: Path) -> None:
    dest = Quartz.CGImageDestinationCreateWithURL(url(path), "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(
        dest, Quartz.CGBitmapContextCreateImage(ctx), None
    )
    if not Quartz.CGImageDestinationFinalize(dest):
        raise SystemExit(f"Could not write {path.name}.")


def fit(w: float, h: float) -> float:
    """The factor that brings the long side down to MAX_SIDE (1 when it is shorter)."""
    return min(1.0, MAX_SIDE / max(w, h, 1))


def render_pdf_page(doc, n: int, dpi: float, dst: Path) -> tuple[int, int]:
    page = Quartz.CGPDFDocumentGetPage(doc, n)
    if page is None:
        raise SystemExit(f"The PDF has no page {n}.")
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFCropBox)
    pw, ph = box.size.width, box.size.height
    if pw <= 0 or ph <= 0:
        raise SystemExit(f"Page {n} of the PDF has no size.")
    if Quartz.CGPDFPageGetRotationAngle(page) % 180 == 90:
        pw, ph = ph, pw
    scale = dpi / 72 * fit(pw * dpi / 72, ph * dpi / 72)
    w, h = max(1, round(pw * scale)), max(1, round(ph * scale))
    ctx = canvas(w, h)
    Quartz.CGContextScaleCTM(ctx, w / pw, h / ph)
    # the page's own rotation and the crop box's origin, at 1:1 in points
    target = Quartz.CGRectMake(0, 0, pw, ph)
    Quartz.CGContextConcatCTM(
        ctx,
        Quartz.CGPDFPageGetDrawingTransform(
            page, Quartz.kCGPDFCropBox, target, 0, True
        ),
    )
    Quartz.CGContextDrawPDFPage(ctx, page)
    save_png(ctx, dst)
    return w, h


def render_image(path: Path, dst: Path) -> tuple[int, int]:
    src = open_image(path)
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
    side = max(int(props.get("PixelWidth", 0)), int(props.get("PixelHeight", 0)))
    options = {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceCreateThumbnailWithTransform: True,  # EXIF orientation
        Quartz.kCGImageSourceThumbnailMaxPixelSize: min(side or MAX_SIDE, MAX_SIDE),
    }
    img = Quartz.CGImageSourceCreateThumbnailAtIndex(src, 0, options)
    if img is None:
        raise SystemExit("The image could not be decoded.")
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    ctx = canvas(w, h)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
    save_png(ctx, dst)
    return w, h


def main() -> None:
    if sys.argv[1] == "--count":
        try:
            print(count(Path(sys.argv[2])))
        except (OSError, SystemExit) as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        return
    job = json.loads(Path(sys.argv[1]).read_text())
    src, pages = Path(job["source"]), job["pages"]
    doc = open_pdf(src) if is_pdf(src) else None
    for i, page in enumerate(pages, 1):
        n, dst = int(page["page"]), Path(page["image"])
        if doc is not None:
            w, h = render_pdf_page(doc, n, float(job["dpi"]), dst)
        else:
            w, h = render_image(src, dst)
        print(f"render {i}/{len(pages)}: page {n}, {w}x{h}", flush=True)


if __name__ == "__main__":
    main()
