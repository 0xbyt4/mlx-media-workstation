"""The local LLM (Qwen3.8 27B, 4-bit) behind prompt help and chat: one worker process, started on demand.

The worker (app/llm_worker.py) runs with the LLM environment's python and keeps the model loaded between requests.
It stops after IDLE_STOP of no use, and before a media job whose peak memory would not fit next to it (the server
calls make_room). A request that does not fit next to the running job is refused with a message, not queued, so a
chat never pushes a video job into swap. Stopping kills the process: only an exit returns all of its memory.
"""

import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from app import models
from app.config import PATHS, ROOT

MODEL = models.BY_ID["qwen3.8-27b-4bit"]
LOG = ROOT / "outputs" / "llm.log"
LOAD_TIMEOUT = 300.0  # seconds to load the weights (about 16 GB from disk)
IDLE_STOP = 600.0  # seconds without a request before the model is unloaded
STOP_WAIT = 5.0  # seconds for a clean exit before SIGKILL
CRASHED = "The model stopped unexpectedly. See outputs/llm.log."
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


lock = threading.Lock()  # guards worker, last_used and each worker's pending
start_lock = threading.Lock()  # one start at a time
worker: Worker | None = None
last_used = 0.0
ids = itertools.count(1)
# the peak memory in GB of the running media job (0 when none); the server sets this
media_peak: Callable[[], float] = lambda: 0.0


def installed() -> bool:
    return models.installed(MODEL) and PATHS.qwen_python.exists()


def fits_with(job_peak: float) -> bool:
    return models.fits(MODEL.peak_gb + job_peak, models.mem_total_gb())


def status() -> dict[str, Any]:
    with lock:
        w = worker
        busy = bool(w and w.pending)
    if w is None or not w.alive():
        state = "off"
    else:
        state = "ready" if w.ready.is_set() else "loading"
    return {
        "installed": installed(),
        "state": state,
        "busy": busy,
        "model": MODEL.label,
        "peak_gb": MODEL.peak_gb,
        "idle_stop_s": IDLE_STOP,
    }


def start() -> Worker:
    """The running worker, started first if needed; waits until the model is loaded."""
    global worker
    with start_lock:
        with lock:
            w = worker
        if w is None or not w.alive():
            if not installed():
                raise LLMError(
                    f"{MODEL.label} is not installed. Install it on the Models screen."
                )
            if not fits_with(media_peak()):
                raise LLMError(
                    "A media job is using the memory. Prompt help and chat work again when it finishes."
                )
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
    """Run one request and yield its events: "started" (with the request id), reasoning and content deltas, then done
    or error.

    Closing the iterator early (a client that went away) cancels the request in the worker.
    """
    global last_used
    w = start()
    rid = f"r{next(ids)}"
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
        yield {"event": "started", "id": rid}
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
    """Stop request rid between tokens; False when it is not running."""
    with lock:
        w = worker
        known = w is not None and rid in w.pending
    if known:
        w.send({"op": "cancel", "id": rid})
    return known


def stop(reason: str = "The model was unloaded.") -> None:
    """Stop the worker; its waiting requests fail with reason."""
    global worker
    with lock:
        w, worker = worker, None
    if w is not None:
        w.stop(reason)


def make_room(job_peak: float) -> None:
    """Unload the model before a media job that would not fit next to it."""
    with lock:
        w = worker
    if w is not None and w.alive() and not fits_with(job_peak):
        stop("Unloaded to free the memory for a media job. Try again when it finishes.")


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
