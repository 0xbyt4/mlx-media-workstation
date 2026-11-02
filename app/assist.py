"""Prompt help: turns a short idea into the text a tool's field needs, following docs/PROMPTING.md.

The default provider is the local LLM (app/llm.py): nothing leaves the Mac. A provider is a function that takes the
tool, the field, the form values and the input images (label, JPEG path) and returns the new text, notes and the model
name; PROVIDERS maps the names that config.toml's [assist] provider may use. Video inputs go as the frame where the
clip ends; audio is never sent.
"""

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import llm
from app.config import ASSIST, ROOT

GUIDE = ROOT / "docs" / "PROMPTING.md"
IMAGE_SIDE = 1024
MAX_TOKENS = 2048
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["text", "notes"],
    "additionalProperties": False,
}
SYSTEM = """You write the text inputs of local generative models (image, voice, music and video) in a media app. The \
user gives a tool, the field to write, the tool's other settings, sometimes input images, and a draft: a rough idea or \
a short phrase in any language, or a prompt to improve. You return the final text for that field, ready to use.

- Follow the prompting guide below for the tool's model. It comes from the models' docs and tests on this app, and it \
wins over general prompting habits.
- Keep the draft's intent, subjects, names and quoted words. Add the concrete detail the guide asks for (framing, \
light, action, camera, sound), but no new characters, on-screen text or story turns that the draft does not imply.
- Write in English, except the words that are spoken or sung (dialogue in quotes, voice scripts, lyrics): keep those \
in the draft's language.
- Fit the settings: the length in seconds (about 2.5 spoken words a second), the size, sound on or off, and the \
field's help text.
- When input images are given, use what they show. For image or voice to video, describe what changes, not what the \
image already shows. For extend, the image is the frame where the clip ends and the new part starts.
- "text" is plain text only: no markdown, headings, surrounding quotes or comments.
- "notes" says in one or two short sentences what you added or assumed, in the draft's language.
- Answer with one JSON object: {{"text": ..., "notes": ...}}.

<prompting_guide>
{guide}
</prompting_guide>"""

Provider = Callable[
    [dict[str, Any], dict[str, Any], dict[str, str], list[tuple[str, Path]]],
    dict[str, Any],
]


class AssistError(Exception):
    """A failure the user can act on; the message is shown in the app."""


def still(src: Path, dst: Path, video: bool, at: float | None = None) -> Path:
    """A JPEG of at most IMAGE_SIDE px: the image itself, or the video frame at `at` (default: its last frame)."""
    seek = ["-sseof", "-0.2"] if at is None else ["-ss", f"{max(at - 0.05, 0):.3f}"]
    cmd = ["ffmpeg", "-v", "error", "-y", *(seek if video else []), "-i", str(src)]
    cmd += [
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({IMAGE_SIDE},iw)':'min({IMAGE_SIDE},ih)':force_original_aspect_ratio=decrease",
        "-q:v",
        "3",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not dst.exists():
        why = (
            " ".join(r.stderr.strip().splitlines()[-2:]) or f"exit code {r.returncode}"
        )
        raise AssistError(f"Could not read the input file for prompt help: {why}")
    return dst


def request_text(
    tool: dict[str, Any], target: dict[str, Any], values: dict[str, str]
) -> str:
    settings = [
        f"- {f['label']}: {values[f['name']]}"
        for f in tool["fields"]
        if f["name"] != target["name"] and values.get(f["name"], "").strip()
    ]
    return "\n".join(
        [
            f"Tool: {tool['label']} ({tool['group']}). {tool.get('description', '')}",
            f"Field to write: {target['label']}. Its help text: {target.get('help', 'none')}",
            "Other settings:",
            *(settings or ["- none"]),
            "",
            "<draft>",
            values.get(target["name"], ""),
            "</draft>",
        ]
    )


def parse(text: str) -> tuple[str, str]:
    try:
        answer = json.loads(text)
        return answer["text"].strip(), answer["notes"].strip()
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        raise AssistError(
            "The answer was not in the expected form. Try again."
        ) from None


def local(
    tool: dict[str, Any],
    target: dict[str, Any],
    values: dict[str, str],
    images: list[tuple[str, Path]],
) -> dict[str, Any]:
    """The local LLM, with the answer held to SCHEMA and thinking off (fast, and JSON from the first token)."""
    content: list[dict[str, Any]] = []
    for label, _ in images:
        content.append({"type": "image"})
        content.append({"type": "text", "text": f"Input image above: {label}"})
    content.append({"type": "text", "text": request_text(tool, target, values)})
    messages = [
        {"role": "system", "content": SYSTEM.format(guide=GUIDE.read_text())},
        {"role": "user", "content": content},
    ]
    t0 = time.time()
    try:
        done = llm.complete(
            messages, [str(p) for _, p in images], schema=SCHEMA, max_tokens=MAX_TOKENS
        )
    except llm.LLMError as e:
        raise AssistError(str(e)) from None
    if done["finish"] == "length":
        raise AssistError("The answer was cut off. Try a shorter draft.")
    text, notes = parse(done["content"])
    return {
        "text": text,
        "notes": notes,
        "model": llm.MODEL.label,
        "cost_usd": None,
        "seconds": round(time.time() - t0, 1),
    }


def local_status() -> dict[str, Any]:
    s = llm.status()
    # on a Mac with too little memory the model never loads, so the Improve buttons stay hidden
    return {
        "available": s["installed"] and llm.fits_with(0.0),
        "model": s["model"],
        "llm": s,
    }


PROVIDERS: dict[str, tuple[Provider, Callable[[], dict[str, Any]]]] = {
    "local": (local, local_status),
}


def provider() -> tuple[Provider, Callable[[], dict[str, Any]]]:
    if ASSIST.provider not in PROVIDERS:
        raise AssistError(
            f"Unknown prompt help provider {ASSIST.provider!r} in config.toml. Known: {', '.join(PROVIDERS)}."
        )
    return PROVIDERS[ASSIST.provider]


def status() -> dict[str, Any]:
    try:
        _, info = provider()
    except AssistError as e:
        return {"available": False, "provider": ASSIST.provider, "error": str(e)}
    return {"provider": ASSIST.provider, **info()}


def enhance(
    tool: dict[str, Any],
    field_name: str,
    values: dict[str, str],
    images: list[tuple[str, Path]],
) -> dict[str, Any]:
    """The field's new text from the configured provider. images: (label, JPEG path) pairs."""
    if not values.get(field_name, "").strip():
        raise AssistError("Write a short idea in the field first.")
    target = next(f for f in tool["fields"] if f["name"] == field_name)
    run, _ = provider()
    return run(tool, target, values, images)
