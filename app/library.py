"""The Library screen's endpoints: every output of finished jobs, Show in Finder, and deleting one output.

Only files listed in a finished job's `outputs` are reachable here: never inputs (in_* files), never other names.
Writes pass the server's same-origin check. Media facts (size, duration) come from the file header (PNG, WAV) or from
ffprobe, once per file version, and stay in memory, so the list stays fast for thousands of outputs.
"""

import json
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/library")

TEXT_KEYS = ("prompt", "text", "caption")  # the job's main text, in this order
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
PROBE_WORKERS = 8

probed: dict[
    str, tuple[int, int, dict[str, Any]]
] = {}  # path -> (size, mtime_ns, facts)
probed_lock = threading.Lock()


class FileRef(BaseModel):
    job: str
    name: str


def server():  # imported here: app.server includes this router while it loads
    from app import server

    return server


def main_text(params: dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def probe(path: Path) -> dict[str, Any]:
    """Width, height and duration of a media file; None for what ffprobe does not report."""
    facts: dict[str, Any] = {"width": None, "height": None, "duration": None}
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return facts
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video and video.get("width") and video.get("height"):
        facts["width"], facts["height"] = int(video["width"]), int(video["height"])
    try:
        facts["duration"] = round(float(data.get("format", {})["duration"]), 2)
    except (KeyError, TypeError, ValueError):
        pass
    return facts


def header_facts(path: Path) -> dict[str, Any] | None:
    """The facts of a PNG (its size) or a WAV file (its length) from the file's header, without ffprobe; None for
    another type or a header that is not plain. ffprobe costs about 25 ms a file: 9 s for 2,400 outputs."""
    facts: dict[str, Any] = {"width": None, "height": None, "duration": None}
    try:
        with path.open("rb") as f:
            head = f.read(24)
            if path.suffix.lower() == ".png":
                if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
                    return None
                facts["width"] = int.from_bytes(head[16:20], "big")
                facts["height"] = int.from_bytes(head[20:24], "big")
                return facts if facts["width"] and facts["height"] else None
            if (
                path.suffix.lower() != ".wav"
                or head[:4] != b"RIFF"
                or head[8:12] != b"WAVE"
            ):
                return None
            # the chunks: an id, a length (little-endian) and the data, padded to an even length. The "fmt " data
            # has the format at offset 0 (its subformat at 24 for 0xFFFE) and the bytes a second at offset 8; for
            # PCM or float samples, the "data" length over it is the length in seconds.
            size, at, rate = f.seek(0, 2), 12, 0
            while at + 8 <= size:
                f.seek(at)
                chunk = f.read(8 + 26)
                length = int.from_bytes(chunk[4:8], "little")
                if chunk[:4] == b"fmt ":
                    code = int.from_bytes(chunk[8:10], "little")
                    if code == 0xFFFE:
                        code = int.from_bytes(chunk[32:34], "little")
                    if code not in (1, 3):  # a compressed format: ffprobe decides
                        return None
                    rate = int.from_bytes(chunk[16:20], "little")
                elif chunk[:4] == b"data":
                    # no format before it, or a length that a streaming writer left open: ffprobe decides
                    if not rate or at + 8 + length > size:
                        return None
                    facts["duration"] = round(length / rate, 2)
                    return facts
                at += 8 + length + length % 2
    except OSError:
        return None
    return None


def facts_of(path: Path, size: int, mtime_ns: int, kind: str) -> dict[str, Any]:
    if kind == "text":  # a transcript: ffprobe finds nothing to report
        return {"width": None, "height": None, "duration": None}
    with probed_lock:
        hit = probed.get(str(path))
    if hit and hit[:2] == (size, mtime_ns):
        facts = hit[2]
    else:
        facts = header_facts(path) or probe(path)
        with probed_lock:
            probed[str(path)] = (size, mtime_ns, facts)
    if kind == "image":
        return {**facts, "duration": None}  # a still has no length
    if kind == "audio":
        return {**facts, "width": None, "height": None}  # cover art is not the output
    return facts


def entries() -> list[dict[str, Any]]:
    srv = server()
    keys = ("id", "tool", "tool_label", "created", "finished", "elapsed", "peak_gb")
    with srv.lock:
        done = [
            (
                {k: job.get(k) for k in keys},
                dict(job.get("params") or {}),
                list(job.get("outputs") or []),
            )
            for job in srv.jobs.values()
            if job.get("status") == "done"
        ]
    items = []
    for job, params, outs in done:
        for o in outs:
            name = o.get("name", "")
            path = srv.OUT / job["id"] / name
            if not NAME.match(name) or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue  # removed outside the app
            items.append(
                {
                    "job": job["id"],
                    "tool": job["tool"],
                    "tool_label": job["tool_label"] or job["tool"],
                    "kind": o.get("kind"),
                    "name": name,
                    "url": f"/files/{job['id']}/{name}",
                    "note": o.get("note", ""),
                    "size": st.st_size,
                    "created": job["created"],
                    "finished": job["finished"],
                    "elapsed": job["elapsed"],
                    "peak_gb": job["peak_gb"],
                    "text": main_text(params),
                    "params": params,
                    "path": str(path),
                    "_stat": (st.st_size, st.st_mtime_ns),
                }
            )
    items.sort(key=lambda i: (i["created"] or "", i["job"], i["name"]), reverse=True)
    return items


def output_path(job_id: str, name: str) -> tuple[dict[str, Any], Path]:
    """The job and the path of one of its listed outputs, or an HTTP error. Call under the server lock."""
    srv = server()
    job = srv.jobs.get(job_id)
    if job is None or not NAME.match(name):
        raise HTTPException(404, "No such output")
    if name.startswith("in_") or name in srv.INPUT_NAMES:
        raise HTTPException(400, "Inputs of a job are not outputs")
    if job["status"] in ("queued", "running") or job_id in srv.procs:
        raise HTTPException(409, "The job is still running")
    if not any(o.get("name") == name for o in job.get("outputs") or []):
        raise HTTPException(404, "No such output")
    job_dir = (srv.OUT / job_id).resolve()
    path = srv.OUT / job_id / name
    if path.is_symlink() or path.resolve().parent != job_dir or not path.is_file():
        raise HTTPException(404, "No such output")
    return job, path


@router.get("")
def get_library() -> list[dict[str, Any]]:
    items = entries()
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        facts = list(
            pool.map(lambda i: facts_of(Path(i["path"]), *i["_stat"], i["kind"]), items)
        )
    for item, f in zip(items, facts, strict=True):
        del item["_stat"]
        item.update(f)
    return items


@router.post("/reveal")
def reveal(ref: FileRef) -> dict[str, str]:
    srv = server()
    with srv.lock:
        _, path = output_path(ref.job, ref.name)
    done = subprocess.run(
        ["open", "-R", str(path)], capture_output=True, timeout=10, check=False
    )
    if done.returncode != 0:
        raise HTTPException(500, "Finder could not show the file")
    return {"status": "shown"}


@router.delete("/{job_id}/{name}")
def delete_output(job_id: str, name: str) -> dict[str, str]:
    srv = server()
    with srv.lock:
        job, path = output_path(job_id, name)
        path.unlink()
        job["outputs"] = [o for o in job["outputs"] if o.get("name") != name]
        srv.save(job)
    with probed_lock:
        probed.pop(str(path), None)
    return {"status": "deleted"}
