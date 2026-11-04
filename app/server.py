"""The workstation server: the tool list, one job queue (one model at a time), output files and system info.

Listens on 127.0.0.1 only and answers only requests addressed to it (Host check) from its own page (Origin check), so
other web pages cannot reach it through the browser, and no page may show it in a frame (frame_headers). At start it
writes a random token to outputs/.server-token (0600); GET /api/system?nonce= proves it knows the token, so start.py
opens a window only on this app. Every request needs the session cookie, which GET /auth?t=<token> sets: start.py
opens the window there. So another program or user on this Mac, which cannot read the token file, gets 401.
In --browser mode the browser also sends the cookie to other servers on 127.0.0.1 (cookies ignore the port). Jobs live in outputs/<id>/ with job.json (state and parameters),
job.log (the model's output) and the result files; the list is rebuilt from those folders at start.

One model at a time: the worker starts a job's process (its own process group) under the lock, and does not take the
next job until that whole group is gone; a cancel stops the group (SIGTERM, then SIGKILL after 10 s). shutdown()
stops the queue and every model before the app exits; a group left over from a crash is killed at the next start.

The queue runs in `order` (a number saved in job.json; POST /api/jobs/{id}/move changes it). Jobs from before that
key have none and sort first, by `created`. While a job is queued or running, app/macos.py keeps the Mac awake, and a
job that ends done or failed shows a notification (both switches are in Settings > Jobs, config.toml [app]).
"""

import asyncio
import hmac
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import (
    assist,
    chat,
    config,
    library,
    llm,
    macos,
    models,
    models_api,
    progress,
    settings_api,
    tools,
)
from app.agent import load_settings as load_agent_settings
from app.agent.ports import MCP, InputFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
WEB = ROOT / "web"
PORT = 7870
TAIL_LINES = 25  # log lines in a job's view
TAIL_BYTES = 64 * 1024  # the part of a running job's log that is read for its tail
# the report /usr/bin/time -l adds after each command: the times line, then one "<number>  <name>" line per counter.
# peak_gb reads it from the log; the tail leaves it out, so the model's own last lines fill the job's view.
TIME_REPORT = re.compile(
    r"^ *[\d.]+ real +[\d.]+ user +[\d.]+ sys\n(?: *-?\d+ +[a-z][a-z ]*(?:\n|$))*",
    re.MULTILINE,
)
TIME_COUNTERS = re.compile(
    r"(?: *-?\d+ +[a-z][a-z ]*(?:\n|$))*"
)  # a report's counter lines, at the start
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
KINDS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".heic": "image",
    ".heif": "image",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".ogg": "audio",
    ".opus": "audio",
    ".aiff": "audio",
    ".aif": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".txt": "text",
}
INPUT_NAMES = {
    "start.png",
    "person.png",
    "voice_padded.wav",
    "speech.wav",
    "source.mp4",
    "tail.mp4",
    "extended_raw.mp4",
}  # files we make from the inputs, not results
MAX_UPLOAD = 200 * 1024 * 1024
MAX_REQUEST = 1024 * 1024 * 1024
KILL_AFTER = 10.0  # seconds between SIGTERM and SIGKILL
# a prompt-help request id that the client makes
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# the nonce start.py sends to GET /api/system to check that the server on the port is this app (see start())
NONCE = re.compile(r"[0-9a-f]{32,128}")
TOKEN_NAME = ".server-token"
token: str | None = (
    None  # this run's secret; only start.py (the same user) can read the file that holds it
)
# where /auth may go: a path of this app (not //host) in printable ASCII, with no spaces or backslashes
NEXT = re.compile(r"/(?![/\\])[!-\[\]-~]*")
SESSION_COOKIE = f"workstation_{PORT}"  # holds the token; HttpOnly, SameSite=Strict
SIGN_IN = """<!doctype html><meta charset="utf-8"><title>Workstation</title>
<p style="font: 15px -apple-system, sans-serif; margin: 3em">Open Workstation with Workstation.app or
<code>start.py</code>. This address needs the link that start.py opens or prints.</p>"""

app = FastAPI(title="Workstation", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
lock = threading.Lock()
wake = threading.Event()
stopping = threading.Event()
jobs: dict[str, dict[str, Any]] = {}
procs: dict[str, subprocess.Popen] = {}
claiming: set[str] = set()  # the queued job whose memory claim waits for LLM answers
awake = macos.Awake()


NO_FRAMES = "frame-ancestors 'none'"


def frame_headers(response: Response) -> Response:
    """No other page may show this app in a frame (clickjacking of the approval buttons): X-Frame-Options for older
    engines and the CSP directive, added to a policy the response already has (the workspace files' sandbox)."""
    response.headers["X-Frame-Options"] = "DENY"
    csp = response.headers.get("Content-Security-Policy")
    response.headers["Content-Security-Policy"] = (
        f"{csp.rstrip('; ')}; {NO_FRAMES}" if csp else NO_FRAMES
    )
    return response


def signed_in(request: Request) -> bool:
    return (
        token is not None
        and hmac.compare_digest(  # bytes: any text compares, ASCII or not
            request.cookies.get(SESSION_COOKIE, "").encode(), token.encode()
        )
    )


def open_route(request: Request) -> bool:
    """The routes that work without the session: the sign-in link, and start.py's check of the server (it answers
    only the proof and the profile without the session; see system())."""
    path = request.url.path
    return request.method == "GET" and (
        path == "/auth" or (path == "/api/system" and "nonce" in request.query_params)
    )


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Every request needs the session cookie (signed_in), except open_route. Changes (every method but GET and
    HEAD) only from this app's own page, and no oversized bodies. Every response, a refusal too, gets the frame
    headers."""
    if not signed_in(request) and not open_route(request):
        if request.url.path.startswith("/api/") or request.url.path.startswith(
            "/files/"
        ):
            refusal: Response = JSONResponse(
                {
                    "detail": "Not signed in: open the app with Workstation.app or start.py."
                },
                status_code=401,
            )
        else:
            refusal = Response(SIGN_IN, status_code=401, media_type="text/html")
        return frame_headers(refusal)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ORIGINS:
            return frame_headers(
                JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
            )
        if int(request.headers.get("content-length") or 0) > MAX_REQUEST:
            return frame_headers(
                JSONResponse({"detail": "Request too large"}, status_code=413)
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if not request.url.path.startswith(
        "/api/"
    ):  # the page's own files: check for a newer copy after an update
        response.headers.setdefault("Cache-Control", "no-cache")
    return frame_headers(response)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save(job: dict[str, Any]) -> None:
    """Write job.json through a temporary file, so a failed or concurrent write never leaves half a file."""
    path = OUT / job["id"] / "job.json"
    tmp = path.with_name(f".job.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(job, indent=1))
    os.replace(tmp, path)


def queue_key(job: dict[str, Any]) -> tuple[float, str, str]:
    return (job.get("order") or 0, job.get("created") or "", job["id"])


def queued_jobs() -> list[dict[str, Any]]:
    """The queued jobs in the order they run (call with the lock held)."""
    return sorted((j for j in jobs.values() if j["status"] == "queued"), key=queue_key)


def next_order() -> int:
    """An order after every job (call with the lock held)."""
    return 1 + max((int(j.get("order") or 0) for j in jobs.values()), default=0)


def has_work() -> bool:
    """Whether to keep the Mac awake: the switch is on and a job is queued or running."""
    if not config.APP.keep_awake or stopping.is_set():
        return False
    with lock:
        return any(j["status"] in ("queued", "running") for j in jobs.values())


def sync_awake() -> None:
    awake.sync(has_work)


def ended(job: dict[str, Any]) -> None:
    """After a job: the notification (done or failed, never cancelled) and the keep-awake state."""
    if config.APP.notify and job["status"] in ("done", "failed"):
        label = job.get("tool_label") or job["tool"]
        if job["status"] == "done":
            took = round(job.get("elapsed") or 0)
            text = f"{label} is done. It took {took // 60}:{took % 60:02d}."
        else:
            text = f"{label} failed. Open Jobs to see why."
        macos.notify(text)
    sync_awake()


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # the id was reused by someone else's process
        return False
    return True


def stop_group(pgid: int, timeout: float = KILL_AFTER) -> None:
    """SIGTERM a job's process group, SIGKILL it if it lingers, and return only when it is gone."""
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        end = time.time() + wait
        while time.time() < end:
            if not group_alive(pgid):
                return
            time.sleep(0.2)


def load_jobs() -> None:
    for meta in OUT.glob("*/job.json"):
        try:
            job = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(job, dict) or job.get("id") != meta.parent.name:
            continue  # not a job file this app wrote (the id names the folder that delete removes)
        if job.get("status") in (
            "queued",
            "running",
        ):  # the app stopped while it was waiting or running
            if job.get("pgid") and group_alive(job["pgid"]):
                stop_group(job["pgid"])  # a model left running by a crash
            job["status"], job["error"] = (
                "cancelled",
                "The app was closed before this job finished.",
            )
            save(job)
        jobs[job["id"]] = job


def outputs(job_dir: Path, notes: dict[str, str]) -> list[dict[str, str]]:
    found = []
    for f in sorted(job_dir.iterdir()):
        kind = KINDS.get(f.suffix.lower())
        if kind and f.name not in INPUT_NAMES and not f.name.startswith("in_"):
            found.append(
                {
                    "url": f"/files/{job_dir.name}/{f.name}",
                    "kind": kind,
                    "name": f.name,
                    "note": notes.get(f.name, ""),
                }
            )
    return found


def peak_gb(log: str) -> float | None:
    m = re.findall(r"(\d+)\s+peak memory footprint", log)
    return round(max(int(x) for x in m) / 2**30, 1) if m else None


def tail(log: str) -> str:
    return "\n".join(TIME_REPORT.sub("", log).splitlines()[-TAIL_LINES:])


def live_tail(path: Path) -> str:
    """The last lines of a running job's log. Only the end of the file is read, because a tqdm log can be large."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            text = f.read().decode(errors="replace")
    except OSError:  # not yet written while the job's inputs are prepared
        return ""
    if size > TAIL_BYTES:
        # the first line is cut: drop it, and the counter lines of a report whose times line it was
        text = text.split("\n", 1)[-1]
        text = text[TIME_COUNTERS.match(text).end() :]
    lines = TIME_REPORT.sub("", text).splitlines()
    return "\n".join(lines[-TAIL_LINES:])


class JobFailed(Exception):
    """A job failure the app itself describes: its message is the whole error, with no exception name."""


def step_failed(cmd: list[str], code: int, log_path: Path, start: int) -> str:
    """The error of a failed step. For ffmpeg its line that names the cause (from start, where the step's output
    begins in the log); a model's last lines are mostly progress, so its log has the details."""
    if Path(cmd[0]).name == "ffmpeg":
        with log_path.open("rb") as f:
            end = f.seek(0, os.SEEK_END)
            f.seek(max(start, end - TAIL_BYTES))
            why = tools.ffmpeg_error(
                TIME_REPORT.sub("", f.read().decode(errors="replace"))
            )
        return (
            f"ffmpeg failed (exit code {code}): {why}"
            if why
            else f"ffmpeg failed (exit code {code}). See the log."
        )
    return f"The model process exited with code {code}. See the log."


def run_job(job: dict[str, Any]) -> None:
    job_dir = OUT / job["id"]
    log_path = job_dir / "job.log"
    files = {k: [job_dir / n for n in v] for k, v in job.get("files", {}).items()}
    t0 = time.time()
    try:
        try:
            plan = tools.plan(job["tool"], job["params"], files, job_dir)
        except ValueError as e:  # bad input, an unreadable file, a time limit: the message is for the user
            raise JobFailed(str(e)) from None
        with log_path.open("w") as log:
            for cmd in plan.commands:
                log.write("$ " + " ".join(cmd)[:2000] + "\n")
                log.flush()
                start = log.tell()
                with (
                    lock
                ):  # check and start under the lock, so a cancel cannot slip in between
                    if job["status"] == "cancelled" or stopping.is_set():
                        return
                    proc = subprocess.Popen(
                        ["/usr/bin/time", "-l", *cmd],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=plan.cwd,
                        env={**os.environ, **plan.env},
                        start_new_session=True,
                    )
                    procs[job["id"]] = proc
                    job["pgid"] = proc.pid
                    save(job)
                code = proc.wait()
                stop_group(
                    proc.pid, timeout=2.0
                )  # /usr/bin/time has returned; make sure the model is gone too
                with lock:
                    procs.pop(job["id"], None)
                if job["status"] == "cancelled":
                    return
                if code != 0:
                    raise JobFailed(step_failed(cmd, code, log_path, start))
        notes_file = job_dir / "notes.json"
        notes = json.loads(notes_file.read_text()) if notes_file.exists() else {}
        job["outputs"] = outputs(job_dir, notes)
        if not job["outputs"]:
            raise JobFailed("The model finished but wrote no output.")
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - any failure belongs to this job; the worker must keep running
        if job["status"] != "cancelled":
            job["status"], job["error"] = (
                "failed",
                str(e) if isinstance(e, JobFailed) else f"{type(e).__name__}: {e}",
            )
    finally:
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        job["log_tail"] = tail(log)
        job["peak_gb"] = peak_gb(log)
        job["finished"], job["elapsed"] = now(), round(time.time() - t0, 1)
        job.pop("pgid", None)
        save(job)


def worker() -> None:
    while not stopping.is_set():
        wake.wait(2)
        wake.clear()
        job: dict[str, Any] | None = None
        try:
            with lock:
                if stopping.is_set():
                    return
                queued = queued_jobs()
                job = queued[0] if queued else None
            if job:
                run_next(job)
        except Exception as e:  # noqa: BLE001 - e.g. a full disk: this job fails, the queue keeps running
            traceback.print_exc()
            with lock:
                orphan = procs.pop(job["id"], None) if job is not None else None
            if (
                orphan is not None
            ):  # a save failed while its model ran: stop it, so no two models run at once
                stop_group(orphan.pid)
            with lock:
                if job is not None:
                    claiming.discard(job["id"])
                    if job["status"] in ("queued", "running"):
                        job["status"], job["error"] = (
                            "failed",
                            f"{type(e).__name__}: {e}",
                        )
            sync_awake()
            time.sleep(
                1
            )  # a disk that stays full must not turn this loop into a busy loop
        if job:
            wake.set()


def run_next(job: dict[str, Any]) -> None:
    """Claim the memory for the first queued job, run it and announce the end."""
    go = False
    try:
        # before the job counts as running: prompt help and chat answers sent before it finish first
        with lock:
            claiming.add(job["id"])
        go = llm.claim(
            models.tool_peak(job["tool"]),
            lambda j=job: j["status"] != "queued" or stopping.is_set(),
        )
        with lock:
            claiming.discard(job["id"])
            go = go and job["status"] == "queued" and not stopping.is_set()
            # a move during the claim may have put another job first: the next round picks that one
            go = go and queued_jobs()[0] is job
            if go:
                job["status"], job["started"] = "running", now()
                save(job)
        if go:
            run_job(job)
    finally:
        llm.release()
    if go:
        ended(job)
    with lock:
        idle = not queued_jobs()
    if idle:
        llm.reload_after_jobs()


def shutdown() -> None:
    """Stop taking jobs, cancel everything queued or running, and wait until every model process is gone. Running
    chat answers get Stop (an agent's waiting approval is denied) and a few seconds to save what they have."""
    stopping.set()
    answers = chat.stop_all()
    with lock:
        for job in jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"], job["error"] = "cancelled", "The app was closed."
                save(job)
        pgids = [p.pid for p in procs.values()]
    awake.release()
    for pgid in pgids:
        stop_group(pgid)
    models.stop()
    llm.close("The app was closed.")
    wake.set()
    chat.wait_all(answers)
    remove_token()


def view(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "tool",
        "tool_label",
        "status",
        "created",
        "started",
        "finished",
        "params",
        "outputs",
        "peak_gb",
        "error",
        "log_tail",
    )
    v = {k: job.get(k) for k in keys}
    if (
        job["id"] in claiming
    ):  # first in line, but answers of prompt help or chat finish first
        v["waiting"] = "Waiting for the prompt help or chat answer to finish."
    v["outputs"] = v["outputs"] or []
    if job["status"] == "running":  # run_job saves the tail only at the end
        v["log_tail"] = live_tail(OUT / job["id"] / "job.log")
    if job["status"] == "running" and job.get("started"):
        v["elapsed"] = round(
            (datetime.now() - datetime.fromisoformat(job["started"])).total_seconds(), 1
        )
    else:
        v["elapsed"] = job.get("elapsed")
    v["progress"] = (
        progress.estimate(job, jobs.values(), OUT, time.time())
        if job["status"] == "running" and job.get("started")
        else None
    )
    v["position"] = 0
    if job["status"] == "queued":
        v["position"] = 1 + [j["id"] for j in queued_jobs()].index(job["id"])
    return v


# the keys of a job in GET /api/status (and "chat")
STATUS_KEYS = (
    "id",
    "tool",
    "tool_label",
    "status",
    "created",
    "position",
    "waiting",
    "progress",
    "elapsed",
)


def check_value(f: dict[str, Any], value: str | None) -> str | None:
    """Validate one form value against its field; returns the value to store (None = use the default)."""
    if value is None or value == "":
        if f.get("required"):
            raise HTTPException(400, f"{f['label']} is required")
        # a text field the user cleared stays empty; a missing one, or an empty number or choice, takes the default
        return value if f["type"] in ("text", "textarea") else None
    if f.get("required") and not value.strip():
        raise HTTPException(400, f"{f['label']} is required")
    if f["type"] == "number":
        try:
            x = float(value)
        except ValueError:
            raise HTTPException(400, f"{f['label']} must be a number") from None
        if not math.isfinite(x):
            raise HTTPException(400, f"{f['label']} must be a finite number")
        if ("min" in f and x < f["min"]) or ("max" in f and x > f["max"]):
            if "min" in f and "max" in f:
                bound = f"between {f['min']} and {f['max']}"
            else:
                bound = f"at least {f['min']}" if "min" in f else f"at most {f['max']}"
            raise HTTPException(400, f"{f['label']} must be {bound}")
        if f["name"] == "seed" and not 0 <= x < 2**32:
            raise HTTPException(400, "Seed must be between 0 and 4294967295")
    if f["type"] == "select" and value not in {o["value"] for o in f["options"]}:
        raise HTTPException(400, f"{f['label']}: unknown option")
    if f["type"] == "checkbox" and value not in ("true", "false"):
        raise HTTPException(400, f"{f['label']} must be true or false")
    return value


@app.get("/api/tools")
def get_tools(installed: bool = False) -> list[dict[str, Any]]:
    """The media tools. With ?installed=true each one also has "installed": whether its model files are on disk."""
    return workstation.list_tools() if installed else tools.TOOLS


@app.get("/api/jobs")
def get_jobs(limit: int | None = Query(None, ge=1, le=100)) -> list[dict[str, Any]]:
    """Every job, newest first. With ?limit=, the queued and running jobs, then the newest finished ones up to `limit`
    jobs in all, each with "chat" (the agent's list_jobs)."""
    if limit is not None:
        return workstation.list_jobs(limit)
    with lock:
        return [
            view(j)
            for j in sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        ]


def new_job_dir() -> tuple[str, Path]:
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    job_dir = OUT / job_id
    job_dir.mkdir(parents=True)
    return job_id, job_dir


def form_value(f: dict[str, Any], raw: Any) -> Any:
    """The value to store for a field that is not a file: the form's text checked (HTTP 400), or the default."""
    value = (
        None if raw is None else str(raw).replace("\r\n", "\n")
    )  # browsers send CRLF
    checked = check_value(f, value)
    return checked if checked is not None else f.get("default")


def prepare_job(
    tool: dict[str, Any],
    job_dir: Path,
    value: Callable[[str], Any],
    inputs: dict[str, list[InputFile]],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The checks of a new job: its field values (value(name) gives the raw value of a field) and its input files,
    which are copied into job_dir as in_<field>_<n><suffix>. The name a file came with is never trusted: only a media
    suffix of it is kept. Returns (params, files). Raises HTTPException(400) or ValueError; the caller removes
    job_dir."""
    params: dict[str, Any] = {}
    files: dict[str, list[str]] = {}
    for f in tool["fields"]:
        name = f["name"]
        if f["type"] in ("file", "files"):
            given = inputs.get(name, [])
            limit = 1 if f["type"] == "file" else f.get("max", 10)
            if len(given) > limit:
                raise HTTPException(400, f"{f['label']}: at most {limit} file(s)")
            saved = []
            for i, inp in enumerate(given):
                if inp.size > MAX_UPLOAD:
                    raise HTTPException(
                        400, f"{f['label']}: file too large (max 200 MB)"
                    )
                suffix = Path(inp.name).suffix.lower()
                suffix = (
                    suffix if suffix in KINDS else ".bin"
                )  # only media types are ever served back
                fname = f"in_{name}_{i}{suffix}"
                inp.write(job_dir / fname)
                saved.append(fname)
            if f.get("required") and not saved:
                raise HTTPException(400, f"{f['label']} is required")
            if saved:
                files[name] = saved
        else:
            params[name] = form_value(f, value(name))
    tools.validate(
        tool["id"], params, {k: [job_dir / n for n in v] for k, v in files.items()}
    )
    return params, files


def queue_job(
    job_id: str,
    tool: dict[str, Any],
    params: dict[str, Any],
    files: dict[str, list[str]],
    chat: str | None = None,
) -> None:
    """Add a checked job to the end of the queue and wake the worker. chat: the agent chat that queued it."""
    job = {
        "id": job_id,
        "tool": tool["id"],
        "tool_label": tool["label"],
        "status": "queued",
        "created": now(),
        "started": None,
        "finished": None,
        "elapsed": None,
        "params": params,
        "files": files,
        "outputs": [],
        "peak_gb": None,
        "error": None,
        "log_tail": "",
    }
    if chat is not None:
        job["chat"] = chat
    with lock:
        job["order"] = next_order()
        jobs[job_id] = job
        save(job)
    sync_awake()
    wake.set()


def upload_writer(up: Any) -> Callable[[Path], None]:
    """Writes an uploaded file (already spooled by the form parser) to a path."""

    def write(path: Path) -> None:
        up.file.seek(0)
        with path.open("wb") as out:
            shutil.copyfileobj(up.file, out)

    return write


@app.post("/api/jobs")
async def create_job(request: Request) -> dict[str, str]:
    if stopping.is_set():
        raise HTTPException(503, "The app is closing")
    form = await request.form()
    tool_id = str(form.get("tool", ""))
    tool = tools.BY_ID.get(tool_id)
    if tool is None:
        raise HTTPException(400, f"Unknown tool {tool_id!r}")
    client = str(form.get("client") or "")
    if client not in ("", MCP):
        raise HTTPException(400, f"Unknown client {client!r}")
    uploads = {
        f["name"]: [
            InputFile(u.filename, u.size or 0, upload_writer(u))
            for u in form.getlist(f["name"])
            if hasattr(u, "filename") and u.filename
        ]
        for f in tool["fields"]
        if f["type"] in ("file", "files")
    }
    # a job from an MCP client: the agent's checks (a media type that the field accepts), and "chat" is MCP
    if client == MCP:
        values = {
            f["name"]: str(form.get(f["name"]))
            for f in tool["fields"]
            if f["type"] not in ("file", "files") and form.get(f["name"]) is not None
        }
        try:
            return {"id": workstation.create_job(tool_id, values, uploads, MCP)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        except Exception as e:  # noqa: BLE001 - as below; create_job removed the job folder
            raise HTTPException(
                500, f"Could not create the job: {type(e).__name__}: {e}"
            ) from None
    job_id, job_dir = new_job_dir()
    try:
        params, files = prepare_job(tool, job_dir, form.get, uploads)
    except ValueError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, str(e)) from None
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001 - report it instead of a bare 500, and leave no half-made job folder
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            500, f"Could not create the job: {type(e).__name__}: {e}"
        ) from None
    queue_job(job_id, tool, params, files)
    return {"id": job_id}


def needs_memory(job: dict[str, Any]) -> bool:
    """Whether a queued job, or a job before it in the queue, needs the memory that the chat model holds: such a job
    starts only after the agent turn's memory lease ends, so a wait inside the turn would only time out (call with the
    lock held)."""
    queued = queued_jobs()
    ahead = queued[: queued.index(job) + 1] if job in queued else [job]
    return any(not llm.fits_with(models.tool_peak(j["tool"])) for j in ahead)


class Workstation:
    """The server's side of the agent's Workstation tools (app/agent/ports.py WorkstationPort). create_job runs the
    checks of POST /api/jobs (prepare_job) and queues the job; it returns at once and never waits for the job, which
    waits for the agent turn's memory lease. wait_job does not wait for a job that needs that memory (needs_memory)."""

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {**t, "installed": all(p.exists() for p in tools.REQUIRES.get(t["id"], []))}
            for t in tools.TOOLS
        ]

    def job_status(self, job_id: str) -> dict[str, Any] | None:
        with lock:
            job = jobs.get(job_id)
            return {**view(job), "chat": job.get("chat")} if job is not None else None

    def list_jobs(self, limit: int) -> list[dict[str, Any]]:
        with lock:
            newest = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
            active = [j for j in newest if j["status"] in ("queued", "running")]
            ended = [j for j in newest if j["status"] not in ("queued", "running")]
            shown = active + ended[: max(0, limit - len(active))]
            shown.sort(key=lambda j: j["created"], reverse=True)
            return [{**view(j), "chat": j.get("chat")} for j in shown]

    def wait_job(
        self, job_id: str, seconds: float, abort: threading.Event
    ) -> dict[str, Any] | None:
        end = time.monotonic() + seconds
        while True:
            with lock:
                job = jobs.get(job_id)
                status = job["status"] if job is not None else None
                if status == "queued" and needs_memory(job):
                    return {**view(job), "chat": job.get("chat"), "needs_memory": True}
            left = end - time.monotonic()
            if status in (None, "done", "failed", "cancelled") or left <= 0:
                break
            if abort.wait(min(left, 0.5)):
                break
        return self.job_status(job_id)

    def cancel_job(self, job_id: str) -> str:
        try:
            return cancel_job(job_id)["status"]
        except HTTPException as e:
            raise ValueError(str(e.detail)) from None

    def search_library(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Outputs whose prompt, tool or file name holds words of the query: the ones with the most of the words
        first, then the newest. A query word also matches inside a longer word: "rain" finds "raining", but
        "extended" does not find "extend"."""
        words = set(query.lower().split())
        keys = ("job", "tool", "tool_label", "kind", "name", "created", "text")
        scored = []
        for i, e in enumerate(library.entries()):  # newest first
            text = " ".join((e["text"], e["tool"], e["tool_label"], e["name"])).lower()
            score = sum(w in text for w in words)
            if score:
                scored.append((-score, i, {k: e[k] for k in keys}))
        return [hit for _, _, hit in sorted(scored, key=lambda s: s[:2])[:limit]]

    def output_file(self, job_id: str, name: str) -> Path | None:
        """A listed output of a done job only (library.output_path refuses inputs, links and paths out of the job
        folder); a failed or cancelled job's files are not offered."""
        with lock:
            job = jobs.get(job_id)
            if job is None or job["status"] != "done":
                return None
            try:
                return library.output_path(job_id, name)[1]
            except HTTPException:
                return None

    def create_job(
        self,
        tool_id: str,
        fields: dict[str, str],
        inputs: dict[str, list[InputFile]] | None = None,
        chat: str | None = None,
    ) -> str:
        if stopping.is_set():
            raise ValueError("The app is closing.")
        tool = tools.BY_ID.get(tool_id)
        if tool is None:
            raise ValueError(f"Unknown tool {tool_id!r}")
        inputs = inputs or {}
        uploads = {
            f["name"]: f for f in tool["fields"] if f["type"] in ("file", "files")
        }
        for name, given in inputs.items():
            f = uploads.get(name)
            if f is None:
                raise ValueError(f"{tool['label']} has no file field {name!r}.")
            # stricter than a browser upload: the suffix must be a media type that the field accepts
            accepts = {a.strip().split("/")[0] for a in f.get("accept", "").split(",")}
            for inp in given:
                kind = KINDS.get(Path(inp.name).suffix.lower())
                if kind is None or (accepts - {""} and kind not in accepts):
                    wanted = " or ".join(sorted(accepts - {""})) or "media"
                    raise ValueError(
                        f"{f['label']} takes {wanted} files; {inp.name} is not one."
                    )
        for f in uploads.values():
            if f.get("required") and not inputs.get(f["name"]):
                raise ValueError(f"{f['label']} needs an uploaded file.")
        job_id, job_dir = new_job_dir()
        try:
            params, files = prepare_job(tool, job_dir, fields.get, inputs)
        except HTTPException as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise ValueError(str(e.detail)) from None
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        queue_job(job_id, tool, params, files, chat)
        return job_id

    def system_status(self) -> dict[str, Any]:
        """GET /api/status. A tool's "fit" uses the queue's rule (models.fits, llm.fits_with): "fits" (it runs next to
        the chat model, or the model is off), "unloads_chat_model" (the job unloads the model first) or "too_big"
        (its peak plus the reserve is more than this Mac's memory)."""
        mem = memory()
        model = llm.status()
        loaded = model["state"] != "off"

        def short(job: dict[str, Any]) -> dict[str, Any]:
            v = view(job)
            return {**{k: v.get(k) for k in STATUS_KEYS}, "chat": job.get("chat")}

        with lock:
            running = next(
                (short(j) for j in jobs.values() if j["status"] == "running"), None
            )
            queue = [short(j) for j in queued_jobs()]
        fit = []
        for t in self.list_tools():
            peak = models.tool_peak(t["id"])
            if not models.fits(peak, mem["mem_total_gb"]):
                state = "too_big"
            elif not loaded or llm.fits_with(peak):
                state = "fits"
            else:
                state = "unloads_chat_model"
            fit.append(
                {
                    "id": t["id"],
                    "label": t["label"],
                    "installed": t["installed"],
                    "peak_gb": peak,
                    "fit": state,
                }
            )
        return {
            "memory": {**mem, "reserve_gb": models.RESERVE_GB},
            "running": running,
            "queue": queue,
            "chat_model": {
                "label": model["model"],
                "installed": model["installed"],
                "state": model["state"],  # off, loading or ready
                "busy": model["busy"],
                "peak_gb": model["peak_gb"],
                "held_gb": model["peak_gb"] if loaded else 0.0,  # its peak while loaded
            },
            "tools": fit,
            "profile": config.APP.profile,
        }


# the server's own port: the agent chats and the endpoints below use it
workstation = Workstation()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    """One job as GET /api/jobs shows it, plus "chat"."""
    job = workstation.job_status(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return job


@app.get("/api/library/search")
def library_search(
    q: str = Query(min_length=1, max_length=200), limit: int = Query(10, ge=1, le=100)
) -> list[dict[str, Any]]:
    """Finished outputs that hold words of q, most words first, then newest (the agent's search_library)."""
    return workstation.search_library(q, limit)


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    """The memory, the running job, the queue, the chat model and whether each media tool fits now."""
    return workstation.system_status()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, str]:
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        if job["status"] not in ("queued", "running"):
            return {"status": job["status"]}
        job["status"], job["error"] = "cancelled", "Cancelled."
        proc = procs.get(job_id)
        save(job)
    if proc is not None:
        threading.Thread(
            target=stop_group, args=(proc.pid,), daemon=True
        ).start()  # the worker waits for it to end
    sync_awake()
    return {"status": "cancelled"}


class Move(BaseModel):
    to: Literal["up", "down", "top", "bottom"]


@app.post("/api/jobs/{job_id}/move")
def move_job(job_id: str, body: Move) -> dict[str, int]:
    """Move a queued job one place up or down, or to the top or bottom of the queue. Under the lock, so the worker
    picks either the old first job or the new one, never a half-made order."""
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        if job["status"] != "queued":
            raise HTTPException(409, "Only a queued job can move")
        queued = queued_jobs()
        i = queued.index(job)
        to = {"up": i - 1, "down": i + 1, "top": 0, "bottom": len(queued) - 1}[body.to]
        to = max(0, min(to, len(queued) - 1))
        if to != i:
            queued.insert(to, queued.pop(i))
            # number the queue again from its lowest order; next_order() stays above all of them
            start = min(int(j.get("order") or 0) for j in queued)
            for n, j in enumerate(queued, start):
                if j.get("order") != n:
                    j["order"] = n
                    save(j)
    wake.set()
    return {"position": to + 1}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        if job["status"] in ("queued", "running") or job_id in procs:
            raise HTTPException(400, "Cancel the job first")
        jobs.pop(job_id)
    shutil.rmtree(OUT / job_id, ignore_errors=True)
    return {"status": "deleted"}


@app.get("/files/{job_id}/{name}")
def get_file(job_id: str, name: str) -> FileResponse:
    if (
        job_id not in jobs
        or "/" in name
        or name.startswith(".")
        or Path(name).suffix.lower() not in KINDS
    ):
        raise HTTPException(404, "Not found")
    path = (OUT / job_id / name).resolve()
    if path.parent != (OUT / job_id).resolve() or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


@app.get("/api/assist")
def assist_status() -> dict[str, Any]:
    return assist.status()


async def until_disconnect(
    request: Request, fn: Callable[[], Any], on_disconnect: Callable[[], None]
) -> Any:
    """Run fn in a thread; call on_disconnect when the client goes away first, then still wait for fn to end.

    Waits on receive() instead of polling request.is_disconnected(): behind @app.middleware("http") the poll never
    sees the disconnect, while receive() returns http.disconnect once the body has been read and the client is gone.
    """
    work = asyncio.ensure_future(run_in_threadpool(fn))
    gone = asyncio.ensure_future(request.receive())
    try:
        await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
        # after the body, receive() returns only when the client is gone
        if not work.done():
            on_disconnect()
        return await work
    finally:
        if gone.done():
            gone.exception()  # retrieved, so asyncio does not log it
        gone.cancel()
        # this coroutine was cancelled: stop the thread's request too
        if not work.done():
            on_disconnect()


@app.post("/api/assist/enhance")
async def assist_enhance(request: Request) -> dict[str, Any]:
    """The same form as a job, plus `field` and `request_id`: the prompt helper writes that field's text. Images and
    video frames go along. POST /api/assist/cancel with the same request_id cancels it (HTTP 409 "Cancelled.")."""
    form = await request.form()
    tool = tools.BY_ID.get(str(form.get("tool", "")))
    if tool is None:
        raise HTTPException(400, "Unknown tool")
    field_name = str(form.get("field", ""))
    if not any(f["name"] == field_name and f.get("assist") for f in tool["fields"]):
        raise HTTPException(400, "This field has no prompt help")
    request_id = str(form.get("request_id") or "") or secrets.token_hex(8)
    if not REQUEST_ID.fullmatch(request_id):
        raise HTTPException(400, "Bad request_id: 1 to 64 letters, digits, - or _")
    values = {
        f["name"]: str(form.get(f["name"]) or "").replace("\r\n", "\n")
        for f in tool["fields"]
        if f["type"] not in ("file", "files")
    }
    uploads: list[tuple[dict[str, Any], int, bytes]] = []
    for f in tool["fields"]:
        if f["type"] not in ("file", "files") or not f.get("accept", "").startswith(
            ("image/", "video/")
        ):
            continue
        parts = [
            u for u in form.getlist(f["name"]) if hasattr(u, "filename") and u.filename
        ]
        for i, up in enumerate(parts[: f.get("max", 1)]):
            if (up.size or 0) > MAX_UPLOAD:
                raise HTTPException(400, f"{f['label']}: file too large (max 200 MB)")
            uploads.append((f, i, await up.read()))

    try:
        end_at = float(values["end_at"]) if values.get("end_at") else None
    except ValueError:
        raise HTTPException(400, "'Use the clip up to' must be a number") from None

    def run(req: assist.Running) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            images = []
            for f, i, data in uploads:
                src = Path(tmp) / f"in_{f['name']}_{i}"
                src.write_bytes(data)
                video = f["accept"].startswith("video/")
                label = f["label"] + (
                    f" #{i} in upload order" if f["type"] == "files" else ""
                )
                if video:
                    label += " (the frame where the clip ends)"
                    try:  # the same checks as a job: a clear message, not ffmpeg's
                        tools.check_extend_source(src, end_at)
                    except ValueError as e:
                        raise assist.AssistError(str(e)) from None
                images.append(
                    (label, assist.still(src, src.with_suffix(".jpg"), video, end_at))
                )
            return assist.enhance(tool, field_name, values, images, req)

    try:
        with assist.track(request_id) as req:
            return await until_disconnect(request, lambda: run(req), req.cancel)
    except assist.Cancelled as e:
        raise HTTPException(409, str(e)) from None
    except assist.AssistError as e:
        raise HTTPException(400, str(e)) from None


class AssistCancel(BaseModel):
    request_id: str


@app.post("/api/assist/cancel")
def assist_cancel(body: AssistCancel) -> dict[str, str]:
    """Cancel a prompt-help request while it waits for memory, loads the model or runs."""
    return {"status": "cancelling" if assist.cancel(body.request_id) else "idle"}


def proof(nonce: str) -> str | None:
    """HMAC-SHA256 of the nonce under this run's token: shows that this server wrote the token file, without
    sending the token. None before start()."""
    if token is None:
        return None
    return hmac.new(token.encode(), nonce.encode(), "sha256").hexdigest()


def publish_token() -> None:
    """A new random token in outputs/.server-token, mode 0600, written atomically (a temp file, then a rename)."""
    global token
    token = secrets.token_hex(32)
    fd, tmp = tempfile.mkstemp(dir=OUT, prefix=TOKEN_NAME + ".", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OUT / TOKEN_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remove_token() -> None:
    """Remove the token file at a clean shutdown, when it still holds this run's token."""
    path = OUT / TOKEN_NAME
    try:
        if token is not None and path.read_text() == token:
            path.unlink()
    except OSError:
        pass


@app.get("/auth")
def auth(t: str = "", next: str = "/") -> Response:
    """The sign-in link start.py opens: with this run's token it sets the session cookie and goes to `next` (a path
    of this app only)."""
    if token is None or not hmac.compare_digest(t.encode(), token.encode()):
        return Response(SIGN_IN, status_code=403, media_type="text/html")
    if not NEXT.fullmatch(next):  # no other site, no header breaks
        next = "/"
        next = "/"
    response = Response(status_code=303, headers={"Location": next})
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/"
    )
    return response


@app.get("/api/system")
def system(request: Request, nonce: str | None = None) -> dict[str, Any]:
    """The memory, the queue and the profile. With ?nonce= (start.py), also `proof`: HMAC(token, nonce); without
    the session, only the proof and the profile."""
    if nonce is not None and not NONCE.fullmatch(nonce):
        raise HTTPException(400, "Bad nonce: 32 to 128 lowercase hex digits")
    if nonce is not None and not signed_in(request):
        return {"proof": proof(nonce), "profile": config.APP.profile}
    with lock:
        running = next(
            (j["id"] for j in jobs.values() if j["status"] == "running"), None
        )
        queued = sum(1 for j in jobs.values() if j["status"] == "queued")
    info = {
        **memory(),
        "running": running,
        "queued": queued,
        "profile": config.APP.profile,  # what the UI shows: "full" or "chat"
    }
    if nonce is not None:
        info["proof"] = proof(nonce)
    return info


def memory() -> dict[str, float]:
    """The Mac's memory in GB: in all, free (free, inactive and speculative pages) and the swap in use."""
    page = 16384
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False).stdout
    pages = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", vm)
    }
    total = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
        ).stdout
        or 0
    )
    swap = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False
    ).stdout
    used = re.search(r"used = ([\d.]+)M", swap)
    return {
        "mem_total_gb": round(total / 2**30, 1),
        "mem_free_gb": round(sum(pages.values()) * page / 2**30, 1),
        "swap_used_gb": round(float(used.group(1)) / 1024, 1) if used else 0.0,
    }


def start() -> None:
    """Load the history and start the worker. Called once by start.py after it knows the port is free. Writes the
    token file that a later start.py uses to check that the server on the port is this app."""
    OUT.mkdir(exist_ok=True)
    publish_token()
    load_jobs()
    # the [agent] settings apply from the start (a bad value stops here); the Settings screen shows the same values
    chat.settings = load_agent_settings()
    if hasattr(settings_api, "AGENT"):
        settings_api.AGENT = chat.settings
    chat.registry()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=llm.watch_idle, daemon=True).start()


settings_api.on_change.append(sync_awake)  # the keep-awake switch applies at once
# the media tools of agent chats (not offered in the chat profile)
chat.port = workstation
app.include_router(models_api.router)
app.include_router(chat.router)
app.include_router(settings_api.router)
app.include_router(library.router)
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
