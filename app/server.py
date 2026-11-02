"""The workstation server: the tool list, one job queue (one model at a time), output files and system info.

Listens on 127.0.0.1 only and answers only requests addressed to it (Host check) from its own page (Origin check), so
other web pages cannot reach it through the browser. Jobs live in outputs/<id>/ with job.json (state and parameters),
job.log (the model's output) and the result files; the list is rebuilt from those folders at start.

One model at a time: the worker starts a job's process (its own process group) under the lock, and does not take the
next job until that whole group is gone; a cancel stops the group (SIGTERM, then SIGKILL after 10 s). shutdown()
stops the queue and every model before the app exits; a group left over from a crash is killed at the next start.
"""

import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import models, models_api, tools

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
WEB = ROOT / "web"
PORT = 7870
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
KINDS = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".wav": "audio",
    ".mp3": "audio",
    ".flac": "audio",
    ".m4a": "audio",
    ".mp4": "video",
    ".mov": "video",
}
INPUT_NAMES = {
    "start.png",
    "person.png",
    "voice_padded.wav",
    "source.mp4",
    "tail.mp4",
    "extended_raw.mp4",
}  # files we make from the inputs, not results
MAX_UPLOAD = 200 * 1024 * 1024
MAX_REQUEST = 1024 * 1024 * 1024
KILL_AFTER = 10.0  # seconds between SIGTERM and SIGKILL

app = FastAPI(title="Workstation", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
lock = threading.Lock()
wake = threading.Event()
stopping = threading.Event()
jobs: dict[str, dict[str, Any]] = {}
procs: dict[str, subprocess.Popen] = {}


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Changes (POST, DELETE) only from this app's own page, and no oversized bodies."""
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ORIGINS:
            return JSONResponse(
                {"detail": "Cross-site request refused"}, status_code=403
            )
        if int(request.headers.get("content-length") or 0) > MAX_REQUEST:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save(job: dict[str, Any]) -> None:
    (OUT / job["id"] / "job.json").write_text(json.dumps(job, indent=1))


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


def run_job(job: dict[str, Any]) -> None:
    job_dir = OUT / job["id"]
    log_path = job_dir / "job.log"
    files = {k: [job_dir / n for n in v] for k, v in job.get("files", {}).items()}
    t0 = time.time()
    try:
        plan = tools.plan(job["tool"], job["params"], files, job_dir)
        with log_path.open("w") as log:
            for cmd in plan.commands:
                log.write("$ " + " ".join(cmd)[:2000] + "\n")
                log.flush()
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
                    raise RuntimeError(
                        f"The model process exited with code {code}. See the log."
                    )
        notes_file = job_dir / "notes.json"
        notes = json.loads(notes_file.read_text()) if notes_file.exists() else {}
        job["outputs"] = outputs(job_dir, notes)
        if not job["outputs"]:
            raise RuntimeError("The model finished but wrote no output.")
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - any failure belongs to this job; the worker must keep running
        if job["status"] != "cancelled":
            job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
    finally:
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        job["log_tail"] = "\n".join(log.splitlines()[-25:])
        job["peak_gb"] = peak_gb(log)
        job["finished"], job["elapsed"] = now(), round(time.time() - t0, 1)
        job.pop("pgid", None)
        save(job)


def worker() -> None:
    while not stopping.is_set():
        wake.wait(2)
        wake.clear()
        with lock:
            if stopping.is_set():
                return
            queued = sorted(
                (j for j in jobs.values() if j["status"] == "queued"),
                key=lambda j: j["created"],
            )
            job = queued[0] if queued else None
            if job:
                job["status"], job["started"] = "running", now()
                save(job)
        if job:
            run_job(job)
            wake.set()


def shutdown() -> None:
    """Stop taking jobs, cancel everything queued or running, and wait until every model process is gone."""
    stopping.set()
    with lock:
        for job in jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"], job["error"] = "cancelled", "The app was closed."
                save(job)
        pgids = [p.pid for p in procs.values()]
    for pgid in pgids:
        stop_group(pgid)
    models.stop()
    wake.set()


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
    v["outputs"] = v["outputs"] or []
    if job["status"] == "running" and job.get("started"):
        v["elapsed"] = round(
            (datetime.now() - datetime.fromisoformat(job["started"])).total_seconds(), 1
        )
    else:
        v["elapsed"] = job.get("elapsed")
    v["position"] = 0
    if job["status"] == "queued":
        queued = sorted(
            (j for j in jobs.values() if j["status"] == "queued"),
            key=lambda j: j["created"],
        )
        v["position"] = 1 + [j["id"] for j in queued].index(job["id"])
    return v


def check_value(f: dict[str, Any], value: str | None) -> str | None:
    """Validate one form value against its field; returns the value to store (None = use the default)."""
    if value is None or value == "":
        if f.get("required"):
            raise HTTPException(400, f"{f['label']} is required")
        return None
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
            raise HTTPException(
                400, f"{f['label']} must be between {f.get('min')} and {f.get('max')}"
            )
        if f["name"] == "seed" and not 0 <= x < 2**32:
            raise HTTPException(400, "Seed must be between 0 and 4294967295")
    if f["type"] == "select" and value not in {o["value"] for o in f["options"]}:
        raise HTTPException(400, f"{f['label']}: unknown option")
    if f["type"] == "checkbox" and value not in ("true", "false"):
        raise HTTPException(400, f"{f['label']} must be true or false")
    return value


@app.get("/api/tools")
def get_tools() -> list[dict[str, Any]]:
    return tools.TOOLS


@app.get("/api/jobs")
def get_jobs() -> list[dict[str, Any]]:
    with lock:
        return [
            view(j)
            for j in sorted(jobs.values(), key=lambda j: j["created"], reverse=True)
        ]


@app.post("/api/jobs")
async def create_job(request: Request) -> dict[str, str]:
    if stopping.is_set():
        raise HTTPException(503, "The app is closing")
    form = await request.form()
    tool_id = str(form.get("tool", ""))
    tool = tools.BY_ID.get(tool_id)
    if tool is None:
        raise HTTPException(400, f"Unknown tool {tool_id!r}")
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    job_dir = OUT / job_id
    job_dir.mkdir(parents=True)
    params: dict[str, Any] = {}
    files: dict[str, list[str]] = {}
    try:
        for f in tool["fields"]:
            name = f["name"]
            if f["type"] in ("file", "files"):
                uploads = [
                    u
                    for u in form.getlist(name)
                    if hasattr(u, "filename") and u.filename
                ]
                limit = 1 if f["type"] == "file" else f.get("max", 10)
                if len(uploads) > limit:
                    raise HTTPException(400, f"{f['label']}: at most {limit} file(s)")
                saved = []
                for i, up in enumerate(uploads):
                    if (up.size or 0) > MAX_UPLOAD:
                        raise HTTPException(
                            400, f"{f['label']}: file too large (max 200 MB)"
                        )
                    suffix = Path(up.filename).suffix.lower()
                    suffix = (
                        suffix if suffix in KINDS else ".bin"
                    )  # only media types are ever served back
                    fname = f"in_{name}_{i}{suffix}"  # never trust the uploaded name
                    (job_dir / fname).write_bytes(await up.read())
                    saved.append(fname)
                if f.get("required") and not saved:
                    raise HTTPException(400, f"{f['label']} is required")
                if saved:
                    files[name] = saved
            else:
                raw = form.get(name)
                value = (
                    None if raw is None else str(raw).replace("\r\n", "\n")
                )  # browsers send CRLF
                checked = check_value(f, value)
                params[name] = checked if checked is not None else f.get("default")
        tools.validate(
            tool_id, params, {k: [job_dir / n for n in v] for k, v in files.items()}
        )
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
    job = {
        "id": job_id,
        "tool": tool_id,
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
    with lock:
        jobs[job_id] = job
        save(job)
    wake.set()
    return {"id": job_id}


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
    return {"status": "cancelled"}


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


@app.get("/api/system")
def system() -> dict[str, Any]:
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
    with lock:
        running = next(
            (j["id"] for j in jobs.values() if j["status"] == "running"), None
        )
        queued = sum(1 for j in jobs.values() if j["status"] == "queued")
    return {
        "mem_total_gb": round(total / 2**30, 1),
        "mem_free_gb": round(sum(pages.values()) * page / 2**30, 1),
        "swap_used_gb": round(float(used.group(1)) / 1024, 1) if used else 0.0,
        "running": running,
        "queued": queued,
    }


def start() -> None:
    """Load the history and start the worker. Called once by start.py after it knows the port is free."""
    OUT.mkdir(exist_ok=True)
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()


app.include_router(models_api.router)
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
