"""The local LLM (Qwen3.8 27B, 4-bit) behind prompt help and chat: one worker process, started on demand.

The worker (app/llm_worker.py) runs with the LLM environment's python and keeps the model loaded between requests.
It stops after IDLE_STOP of no use, and before a media job whose peak memory would not fit next to it: the server
calls claim before each job and release after it. A request sent while such a job holds the memory waits (a "waiting"
event) and runs when the job ends, before the next job starts, so a chat never pushes a video job into swap. Stopping
kills the process: only an exit returns all of its memory.
"""

import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from typing import Any

from app import models
from app.config import PATHS, ROOT

MODEL = models.BY_ID["qwen3.8-27b-4bit"]
LOG = ROOT / "outputs" / "llm.log"
LOAD_TIMEOUT = 300.0  # seconds to load the weights (about 16 GB from disk)
IDLE_STOP = 600.0  # seconds without a request before the model is unloaded
STOP_WAIT = 5.0  # seconds for a clean exit before SIGKILL
JOB_WAIT = (
    600.0  # seconds a media job waits for the answers before it, then stops the model
)
CRASHED = "The model stopped unexpectedly. See outputs/llm.log."
WAITING = "Waiting for memory: a media job is running. This continues when it finishes."
WORKER = ["-m", "app.llm_worker"]  # run with the LLM environment's python


class LLMError(Exception):
    """A failure the user can act on; the message is shown in the app."""


class Worker:
    """One worker process and its requests; a new start makes a new Worker, so an old one cannot mix in."""

    def __init__(self) -> None:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as log:
            self.proc = subprocess.Popen(
                [str(PATHS.qwen_python), *WORKER, str(PATHS.qwen_model)],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                env={**os.environ, "HF_HUB_OFFLINE": "1"},
                start_new_session=True,
            )
        self.ready = (
            threading.Event()
        )  # set when the model is loaded, or when the process ended
        self.fatal: str | None = None  # the worker's own load error
        self.reason: str | None = (
            None  # why it was stopped, for the requests still waiting
        )
        self.pending: dict[str, queue.Queue] = {}  # request id -> its events
        self.write_lock = threading.Lock()
        threading.Thread(target=self.read_events, daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def read_events(self) -> None:
        """Route each event to its request; when the process ends, fail what is still waiting."""
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "ready":
                self.ready.set()
            elif ev.get("event") == "fatal":
                self.fatal = ev.get("message")
            else:
                with lock:
                    q = self.pending.get(ev.get("id", ""))
                if q is not None:
                    q.put(ev)
        self.proc.wait()
        with lock:
            message = self.reason or self.fatal or CRASHED
            for q in self.pending.values():
                q.put({"event": "error", "message": message})
            self.pending.clear()
        self.ready.set()

    def send(self, msg: dict[str, Any]) -> None:
        try:
            with self.write_lock:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise LLMError(self.reason or CRASHED) from None

    def stop(self, reason: str) -> None:
        self.reason = reason
        if self.alive():
            try:
                self.send({"op": "shutdown"})
                self.proc.stdin.close()
            except (LLMError, ValueError):
                pass
            try:
                self.proc.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                pass
        try:  # also after a clean exit: nothing it started may outlive it
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()


lock = (
    threading.Lock()
)  # guards worker, last_used, each worker's pending and the memory state below
room = threading.Condition(lock)  # notified when the memory state changes
start_lock = threading.Lock()  # one start at a time
worker: Worker | None = None
last_used = 0.0
ids = itertools.count(1)
job_peak = (
    0.0  # the peak memory in GB of the media job that holds the memory (0 when none)
)
active = 0  # requests past the memory check, until they end
cancels: dict[
    str, threading.Event
] = {}  # request id -> its cancel flag, from "started" to the end
waiting: set[str] = set()  # the requests waiting for memory


def installed() -> bool:
    return models.installed(MODEL) and PATHS.qwen_python.exists()


def fits_with(job_peak: float) -> bool:
    return models.fits(MODEL.peak_gb + job_peak, models.mem_total_gb())


def status() -> dict[str, Any]:
    with lock:
        w = worker
        busy = bool(w and w.pending)
        held = job_peak
        n_waiting = len(waiting)
    if w is None or not w.alive():
        state = "off"
    else:
        state = "ready" if w.ready.is_set() else "loading"
    return {
        "installed": installed(),
        "state": state,
        "busy": busy,
        "waiting": n_waiting,  # requests waiting for a media job to finish
        "media_peak_gb": held,
        "model": MODEL.label,
        "peak_gb": MODEL.peak_gb,
        "idle_stop_s": IDLE_STOP,
    }


def check_installed() -> None:
    if not installed():
        raise LLMError(
            f"{MODEL.label} is not installed. Install it on the Models screen."
        )
    if not fits_with(0.0):
        raise LLMError(
            f"{MODEL.label} needs about {MODEL.peak_gb:.0f} GB of memory plus {models.RESERVE_GB} GB for the "
            "system; this Mac has less."
        )


def start() -> Worker:
    """The running worker, started first if needed; waits until the model is loaded."""
    global worker
    with start_lock:
        with lock:
            w = worker
        if w is None or not w.alive():
            check_installed()
            w = Worker()
            with lock:
                worker = w
        if not w.ready.wait(LOAD_TIMEOUT) or not w.alive():
            message = (
                w.fatal or w.reason or "The model did not load. See outputs/llm.log."
            )
            stop(message)
            raise LLMError(message)
        return w


def generate(
    messages: list[dict[str, Any]],
    images: Sequence[str] = (),
    *,
    thinking: bool = False,
    schema: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    chat: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Run one request and yield its events: "started" (with the request id), "waiting" when a media job holds the
    memory, reasoning and content deltas, then done or error.

    Closing the iterator early (a client that went away) cancels the request in the worker.
    """
    global active
    check_installed()
    rid = f"r{next(ids)}"
    cancelled = threading.Event()
    with room:
        cancels[rid] = cancelled
        must_wait = not fits_with(job_peak)
        if must_wait:
            waiting.add(rid)
        else:
            active += 1
    try:
        yield {"event": "started", "id": rid}
        if must_wait:
            try:
                yield {"event": "waiting", "message": WAITING}
                with room:
                    room.wait_for(lambda: fits_with(job_peak) or cancelled.is_set())
                    if not cancelled.is_set():
                        active += 1
            finally:
                with room:
                    waiting.discard(rid)
                    room.notify_all()
            if cancelled.is_set():
                yield {
                    "event": "done",
                    "finish": "cancelled",
                    "reasoning": "",
                    "content": "",
                    "stats": {},
                }
                return
        try:
            yield from run(
                rid, cancelled, messages, images, thinking, schema, max_tokens, chat
            )
        finally:
            with room:
                active -= 1
                room.notify_all()
    finally:
        with room:
            cancels.pop(rid, None)


def run(
    rid: str,
    cancelled: threading.Event,
    messages: list[dict[str, Any]],
    images: Sequence[str],
    thinking: bool,
    schema: dict[str, Any] | None,
    max_tokens: int,
    chat: str | None,
) -> Iterator[dict[str, Any]]:
    """The worker part of generate: send the request and yield its events."""
    global last_used
    w = start()
    q: queue.Queue = queue.Queue()
    with lock:
        w.pending[rid] = q
        last_used = time.time()
    finished = False
    try:
        w.send(
            {
                "op": "generate",
                "id": rid,
                "messages": messages,
                "images": list(images),
                "thinking": thinking,
                "schema": schema,
                "max_tokens": max_tokens,
                "chat": chat,
            }
        )
        if cancelled.is_set():  # the cancel came before the worker knew the request
            w.send({"op": "cancel", "id": rid})
        while True:
            ev = q.get()
            finished = ev["event"] in ("done", "error")
            yield ev
            if finished:
                return
    finally:
        with lock:
            w.pending.pop(rid, None)
            last_used = time.time()
        if not finished and w.alive():
            try:
                w.send({"op": "cancel", "id": rid})
            except LLMError:
                pass


def complete(
    messages: list[dict[str, Any]], images: Sequence[str] = (), **kw: Any
) -> dict[str, Any]:
    """Run one request to the end and return its done event. Raises LLMError on failure."""
    for ev in generate(messages, images, **kw):
        if ev["event"] == "error":
            raise LLMError(ev["message"])
        if ev["event"] == "done":
            return ev
    raise LLMError(CRASHED)


def cancel(rid: str) -> bool:
    """Stop request rid between tokens (or while it waits); False when it is not running."""
    with room:
        flag = cancels.get(rid)
        if flag is None:
            return False
        flag.set()
        room.notify_all()
        w = worker
        sent = w is not None and rid in w.pending
    if sent:
        try:
            w.send({"op": "cancel", "id": rid})
        except LLMError:
            pass  # the worker is gone: the request fails on its own
    return True


def stop(reason: str = "The model was unloaded.") -> None:
    """Stop the worker; its waiting requests fail with reason."""
    global worker
    with lock:
        w, worker = worker, None
    if w is not None:
        w.stop(reason)


def claim(peak: float) -> None:
    """Hold the memory for a media job (before it starts): the requests waiting since the last job run first, then
    the model unloads if the job does not fit next to it. New requests wait until release."""
    global job_peak
    with room:
        room.wait_for(lambda: not waiting, JOB_WAIT)
        job_peak = peak
        if fits_with(peak):
            return
        drained = room.wait_for(lambda: active == 0, JOB_WAIT)
    stop(
        "Unloaded to free the memory for a media job."
        if drained
        else "Stopped: a media job could not wait longer for this answer."
    )


def release() -> None:
    """The media job ended: the waiting requests can run."""
    global job_peak
    with room:
        job_peak = 0.0
        room.notify_all()


def watch_idle() -> None:
    """Unload the model after IDLE_STOP without requests (a daemon thread the server starts)."""
    global worker
    while True:
        time.sleep(30)
        with lock:  # check and detach together, so a new request cannot slip in between
            w = worker
            idle = (
                w is not None and not w.pending and time.time() - last_used > IDLE_STOP
            )
            if idle:
                worker = None
        if idle:
            w.stop("Unloaded after being idle.")
