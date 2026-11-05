"""Read Text (OCR) runner (runs in the local LLM environment, mlx-vlm 0.7.11): OvisOCR3 reads each page image that
pages.py made, one page per request.

Mode "markdown" asks for the page as Markdown (tables as HTML, formulas as LaTeX) and writes document.md; mode "text"
asks for plain text and writes document.txt. The prompts are the model card's document_markdown and plain_ocr prompts
(ATH-MaaS/OvisOCR3, Apache-2.0). The Markdown is cleaned as the card says: image-region tags are dropped (this job
cuts no figure images) and a repeated tail is cut. A page that reaches max_tokens is named in notes.json.

The log shows "page i/n" after each page, with its time and tokens.

  python ocr.py job.json
"""

import json
import sys
import time
from pathlib import Path

import mlx.core as mx
from mlx_vlm import apply_chat_template, load, stream_generate

PROMPTS = {
    "markdown": (
        "Extract all readable content from the image in natural human reading order and output the result as a "
        "single Markdown document. For charts or images, represent them using an HTML image tag: "
        '<img src="images/bbox_{left}_{top}_{right}_{bottom}.jpg" />, where left, top, right, bottom are bounding '
        "box coordinates scaled to [0, 1000). Format formulas as LaTeX. Format tables as HTML: <table>...</table>. "
        "Transcribe all other text as standard Markdown. Preserve the original text without translation or "
        "paraphrasing."
    ),
    "text": (
        "Read all the text in the image. Directly output the content.\n\n"
        "Rules:\n"
        "1. Preserve the original text exactly without translation, paraphrasing, correction, or completion.\n"
        "2. Follow the natural human reading order.\n"
        "3. Output plain text only. Use spaces and line breaks only to preserve text separation and reading order.\n"
        "4. Do not output Markdown, HTML, JSON, bounding boxes, category labels, explanations, or code fences."
    ),
}
OUTPUT = {"markdown": "document.md", "text": "document.txt"}
# the image sizes of the model card: smaller pages are scaled up, larger ones down
MIN_PIXELS, MAX_PIXELS = 448 * 448, 2880 * 2880
IMAGE_TAG = '<img src="images/bbox_'


def clean_repeats(
    text: str,
    min_text_len: int = 8000,
    max_period: int = 200,
    min_repeat_chars: int = 100,
    min_repeat_times: int = 5,
) -> str:
    """Cut a tail that repeats one unit at least min_repeat_times (the model card's _clean_truncated_repeats)."""
    n = len(text)
    if n < min_text_len:
        return text
    for unit in range(1, min(max_period, n - 1) + 1):
        if text[n - 1] != text[n - 1 - unit]:
            continue
        match, i = 1, n - 2
        while i >= unit and text[i] == text[i - unit]:
            match += 1
            i -= 1
        total = match + unit
        if total // unit >= min_repeat_times and total >= min_repeat_chars:
            return text[: n - total + unit] + text[n - total % unit :]
    return text


def clean(text: str, mode: str) -> str:
    text = text.strip()
    if mode == "markdown":
        blocks = text.split("\n\n")
        text = "\n\n".join(b for b in blocks if not b.strip().startswith(IMAGE_TAG))
    return clean_repeats(text).strip()


def join(pages: list[tuple[int, str]], numbered: bool) -> str:
    """The pages' text in order; with numbered, each page starts with a <!-- page N --> line."""
    if not numbered:
        return pages[0][1] if pages else ""
    return "\n\n".join(f"<!-- page {n} -->\n\n{text}".rstrip() for n, text in pages)


def main() -> None:
    job = json.loads(Path(sys.argv[1]).read_text())
    out, mode = Path(job["out"]), job["mode"]
    pages = job["pages"]
    mx.set_cache_limit(1 << 30)
    model, processor = load(job["model"])
    prompt = apply_chat_template(
        processor,
        model.config,
        [{"role": "user", "content": PROMPTS[mode]}],
        num_images=1,
        enable_thinking=False,
    )
    texts, cut = [], []
    for i, page in enumerate(pages, 1):
        t0, last, parts = time.time(), None, []
        for r in stream_generate(
            model,
            processor,
            prompt,
            image=[page["image"]],
            max_tokens=int(job["max_tokens"]),
            temperature=0.0,
            min_pixels=MIN_PIXELS,
            max_pixels=MAX_PIXELS,
        ):
            last = r
            if r.finish_reason is None:
                parts.append(r.text)
        mx.clear_cache()
        if last is not None and last.finish_reason == "length":
            cut.append(page["page"])
        texts.append((page["page"], clean("".join(parts), mode)))
        tokens = last.generation_tokens if last else 0
        print(
            f"page {i}/{len(pages)}: page {page['page']}, {time.time() - t0:.1f} s, {tokens} tokens",
            flush=True,
        )
    body = join(texts, bool(job.get("numbered")))
    if not any(text for _, text in texts):
        body = "(No text was found.)"
    name = OUTPUT[mode]
    (out / name).write_text(body + "\n")
    note = f"{len(pages)} page{'s' if len(pages) != 1 else ''}, {'Markdown' if mode == 'markdown' else 'plain text'}"
    if cut:
        note += f"; cut at the token limit: page {', '.join(map(str, cut))}"
    (out / "notes.json").write_text(json.dumps({name: note}))


if __name__ == "__main__":
    main()
