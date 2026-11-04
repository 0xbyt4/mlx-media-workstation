"""The local LLM (Qwen3.8 27B, 4-bit) behind prompt help and chat: one worker process, started on demand.

The worker (app/llm_worker.py) runs with the LLM environment's python and keeps the model loaded between requests.
It stops after the [app] llm_idle_minutes of no use (app/config.py; 0 = never), and before a media job whose peak memory would not fit next to it: the server
calls claim before each job and release after it. A request sent while such a job holds the memory waits (a "waiting"
event) and runs when the job ends, before the next job starts, so a chat never pushes a video job into swap. Stopping
kills the process: only an exit returns all of its memory.

A multi-step turn (model, tool, model, ...) holds the memory between its requests with session(): a lease that a media
job's claim waits for like a running answer, so the model is not unloaded between the steps. Requests may carry tools
(OpenAI-style function schemas); the worker then holds the tool-call text back and returns the parsed calls in "done".
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
from contextlib import contextmanager
from typing import Any

from app import config, models
from app.config import PATHS, ROOT

MODEL = models.BY_ID["qwen3.8-27b-4bit"]
LOG = ROOT / "outputs" / "llm.log"
CANCEL_WAIT = 0.5  # seconds a stopping worker gives its cancelled answers to end between two tokens
LOAD_TIMEOUT = 300.0  # seconds to load the weights (about 16 GB from disk)
STOP_WAIT = 5.0  # seconds for a clean exit before SIGKILL
JOB_WAIT = 600.0  # seconds a media job waits for the answers before it; then it stops the model
SESSION_MAX = JOB_WAIT  # seconds a session holds the memory at most, so a stuck turn cannot block jobs for ever
# the reasoning efforts of the Qwen3.8 chat template (xhigh is its default)
EFFORTS = ("low", "medium", "xhigh")
CRASHED = "The model stopped unexpectedly. See outputs/llm.log."
WAITING = "Waiting for memory: a media job needs it. This continues when the job ends."
CLOSED = "The app was closed."
WORKER = ["-m", "app.llm_worker"]  # run with the LLM environment's python


def idle_stop() -> float | None:
    """Seconds without a request before the model is unloaded (None: never). Read on each use: Settings changes it."""
    minutes = config.APP.llm_idle_minutes
    return minutes * 60.0 if minutes > 0 else None


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
            with lock:
                running = list(self.pending)
            try:
                # the worker ends the current answer before it reads shutdown: cancel it first, or the exit waits
                # STOP_WAIT for the answer and then needs the SIGKILL
                for rid in running:
                    self.send({"op": "cancel", "id": rid})
            except LLMError:
                pass
            # a request cancelled just before (Stop, the app closing) ends as cancelled; the rest end with the reason
            end = time.monotonic() + CANCEL_WAIT
            while time.monotonic() < end and any(r in self.pending for r in running):
                time.sleep(0.02)
            with lock:
                for q in self.pending.values():
                    q.put({"event": "error", "message": reason})
                self.pending.clear()
            try:
                self.send({"op": "shutdown"})
                self.proc.stdin.close()
            except (LLMError, ValueError, OSError):
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
closed = False  # set when the app closes: no new worker starts
leases: set["Session"] = set()  # the sessions that hold the memory
reload_after_job = False  # a media job unloaded the model while it was in use: load it again when the queue empties
local = threading.local()  # .session: the session of the thread's with-block


class Session:
    """A memory lease for a multi-step turn; see session()."""

    def __init__(self, abort: Callable[[], bool], limit: float) -> None:
        self.abort = abort
        self.limit = limit
        # limit seconds after the lease is first held: time spent waiting for a media job before does not count
        self.end: float | None = None
        self.held = False  # counted in leases: its requests skip the memory check
        self.closed = False  # the with-block ended

    def alive(self) -> bool:
        return (
            not self.closed
            and (self.end is None or time.time() < self.end)
            and not self.abort()
        )


def holding() -> bool:
    """True when a live session holds the memory; drops the ones that ended (call with room held)."""
    for s in list(leases):
        if not s.alive():
            s.held = False
            leases.discard(s)
    return bool(leases)


def hold(s: Session | None) -> None:
    """A request of session s got the memory: the session holds it from now on (call with room held)."""
    if s is not None and not s.held and s.alive():
        s.held = True
        if s.end is None:  # the limit counts from here
            s.end = time.time() + s.limit
        leases.add(s)


@contextmanager
def session(
    abort: Callable[[], bool] = lambda: False, limit: float = SESSION_MAX
) -> Iterator[Session]:
    """Hold the model's memory for a whole multi-step turn, so a queued media job does not unload it between steps.

    The lease starts at once when the memory is free, or else with the first request of the session that gets it. A
    media job's claim then waits for the lease as for a running answer, and the session's requests skip the memory
    check, so they never wait for that job. The lease ends when the with-block ends, when abort() returns True or
    limit seconds after it was first held (a wait for memory before that does not count); requests after that behave
    like any other request. The block never waits here.

    Requests made in the with-block's thread use the session; pass session= to generate to use it from another
    thread. abort is called with the module lock held: it must be fast and must not call this module.
    """
    s = Session(abort, limit)
    with room:
        if not closed and fits_with(job_peak):
            hold(s)
    outer = getattr(local, "session", None)
    local.session = s
    try:
        yield s
    finally:
        local.session = outer
        with room:
            s.closed, s.held = True, False
            leases.discard(s)
            room.notify_all()


def model_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages as the chat template needs them; the input is not changed.

    Assistant tool_calls may be {"id", "name", "arguments"} (as in a "done" event) or OpenAI's {"id", "type",
    "function": {"name", "arguments"}}; arguments may be a dict or a JSON string. The template renders the arguments
    with |items, so they become a dict ({} when the string is not a JSON object). An assistant message with calls and
    no content gets "". Tool results are {"role": "tool", "content": str} plus any "tool_call_id" and "name" (the
    Qwen3.8 template uses only the content and the order); content that is not text becomes JSON.
    """
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m = {
                **m,
                "content": m.get("content") or "",
                "tool_calls": [tool_call(c) for c in m["tool_calls"]],
            }
        elif m.get("role") == "tool" and not isinstance(m.get("content"), (str, list)):
            m = {**m, "content": json.dumps(m.get("content"), ensure_ascii=False)}
        out.append(m)
    return out


def tool_call(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") or call
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    out = {k: v for k, v in call.items() if k != "function"}
    out.update(
        name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}
    )
    return out


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
        "idle_stop_s": idle_stop(),
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


def start(
    cancelled: threading.Event | None = None, background: bool = False
) -> Worker | None:
    """The running worker, started first if needed; waits until the model is loaded. None when cancelled is set
    during the load: the load goes on in the worker, so the next request finds the model ready. background: a load
    that no request asked for (preload); it starts no worker while a media job holds memory that the model does not
    fit next to, and it does not count as use for the idle time."""
    global worker, last_used
    with start_lock:
        with lock:
            w = worker
            if background and (w is not None or (job_peak and not fits_with(job_peak))):
                return None
        if w is None or not w.alive():
            check_installed()
            try:
                w = Worker()
            except OSError as e:  # a python that is not a program: a folder, no exec bit, the wrong CPU
                raise LLMError(
                    f"The LLM environment's python could not start ({PATHS.qwen_python}): {e.strerror}. "
                    "Check qwen_python in Settings."
                ) from e
            with lock:
                # a media job that claimed the memory during this background start: give it the memory
                blocked = background and bool(job_peak) and not fits_with(job_peak)
                if not closed and not blocked:
                    worker = w
                    if not background:
                        # the idle watch counts from here: a model that loads is not idle (also after a Stop that
                        # left it loading for the next request)
                        last_used = time.time()
            if (
                worker is not w
            ):  # the app closed, or a job took the memory, while it started
                w.stop(
                    CLOSED if closed else "Unloaded to free the memory for a media job."
                )
                if background:
                    return None
                raise LLMError(CLOSED)
        deadline = time.monotonic() + LOAD_TIMEOUT
        while not w.ready.wait(0.25) and w.alive() and time.monotonic() < deadline:
            if cancelled is not None and cancelled.is_set():
                return None
        if not w.ready.is_set() or not w.alive():
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
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    reasoning_effort: str | None = None,
    thinking_budget: int | None = None,
    allow_emoji: bool = False,
    session: Session | None = None,
) -> Iterator[dict[str, Any]]:
    """Run one request and yield its events: "started" (with the request id), "waiting" when a media job holds the
    memory, reasoning and content deltas, then done or error.

    tools: OpenAI-style function schemas ({"type": "function", "function": {"name", "description", "parameters"}}),
    given to the chat template. The content deltas then never contain tool-call markup, and "done" has "tool_calls":
    [{"id", "name", "arguments" (a dict, typed by the schema)}], with "error" (and "raw") on a call that did not
    parse; "finish" is "tool_calls" when the list is not empty. messages may hold assistant messages with tool_calls
    and {"role": "tool", "content"} results (see model_messages). reasoning_effort ("low", "medium" or "xhigh")
    applies with thinking. thinking_budget: with thinking, the thinking tokens after which the worker forces the end
    of the thinking, so the model must answer (app/effort.py); None: no limit. session: the lease the request runs
    under; by default the one of this thread's session() block.

    tool_choice "none": the tools stay in the prompt (so its cached start stays the same), but the worker bans the
    call marker, so the model answers with text. allow_emoji: the worker bans emoji tokens unless this is true
    (never inside a tool call's arguments).

    Closing the iterator early (a client that went away) cancels the request in the worker.
    """
    global active
    if reasoning_effort is not None and reasoning_effort not in EFFORTS:
        raise ValueError(f"reasoning_effort must be one of {EFFORTS}")
    if thinking_budget is not None and (
        type(thinking_budget) is not int or thinking_budget < 1
    ):
        raise ValueError("thinking_budget must be a positive whole number")
    if tool_choice not in (None, "none"):
        raise ValueError('tool_choice must be None or "none"')
    check_installed()
    lease = session if session is not None else getattr(local, "session", None)
    rid = f"r{next(ids)}"
    cancelled = threading.Event()
    counted = False  # counted in active

    def free() -> bool:  # with room held
        return (lease is not None and lease.held and lease.alive()) or fits_with(
            job_peak
        )

    try:
        with room:
            cancels[rid] = cancelled
            if free():
                active += 1
                counted = True
                hold(lease)
            else:
                waiting.add(rid)
        yield {"event": "started", "id": rid}
        if not counted:
            yield {"event": "waiting", "message": WAITING}
            with room:
                room.wait_for(lambda: free() or cancelled.is_set())
                waiting.discard(rid)
                if not cancelled.is_set():
                    active += 1
                    counted = True
                    hold(lease)
                room.notify_all()
        if cancelled.is_set():  # before the model loads for it
            done = {
                "event": "done",
                "finish": "cancelled",
                "reasoning": "",
                "content": "",
                "stats": {},
            }
            yield {**done, "tool_calls": []} if tools else done
            return
        req = {
            "op": "generate",
            "id": rid,
            "messages": model_messages(messages),
            "images": list(images),
            "thinking": thinking,
            "schema": schema,
            "max_tokens": max_tokens,
            "chat": chat,
            "tools": tools or None,
            "tool_choice": tool_choice,
            "reasoning_effort": reasoning_effort,
            "thinking_budget": thinking_budget,
            "allow_emoji": allow_emoji,
        }
        for ev in run(rid, cancelled, req):
            if tools and ev["event"] == "done":
                ev.setdefault("tool_calls", [])
            yield ev
    finally:  # also when the iterator is closed at any yield
        with room:
            cancels.pop(rid, None)
            waiting.discard(rid)
            if counted:
                active -= 1
            room.notify_all()


def run(
    rid: str, cancelled: threading.Event, req: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """The worker part of generate: send the request and yield its events."""
    global last_used
    w = start(cancelled)
    if w is None:  # Stop while the model loaded
        yield {
            "event": "done",
            "finish": "cancelled",
            "reasoning": "",
            "content": "",
            "stats": {},
        }
        return
    q: queue.Queue = queue.Queue()
    with lock:
        w.pending[rid] = q
        last_used = time.time()
    finished = False
    try:
        w.send(req)
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
    messages: list[dict[str, Any]],
    images: Sequence[str] = (),
    on_start: Callable[[str], None] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """Run one request to the end and return its done event. Raises LLMError on failure.

    on_start gets the request id at "started", before the request waits or runs, so a caller can cancel it.
    """
    for ev in generate(messages, images, **kw):
        if ev["event"] == "started" and on_start is not None:
            on_start(ev["id"])
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


def close(reason: str = CLOSED) -> None:
    """The app closes: cancel every request, also the waiting ones, end the leases, stop the worker and start none
    again."""
    global closed, reload_after_job
    with room:
        closed = True
        reload_after_job = False
        for flag in cancels.values():
            flag.set()
        for s in leases:
            s.held = False
        leases.clear()
        room.notify_all()
    stop(reason)


def wait_room(
    ready: Callable[[], bool], abort: Callable[[], bool], end: float
) -> bool | None:
    """Wait (holding room) until ready: True; False at the time limit; None when abort says the job is off."""
    while not ready():
        if abort():
            return None
        left = end - time.time()
        if left <= 0:
            return False
        room.wait(min(left, 1.0))
    return True


def claim(peak: float, abort: Callable[[], bool] = lambda: False) -> bool:
    """Hold the memory for a media job (before it starts): the requests waiting since the last job run first, then
    the model unloads if the job does not fit next to it, once the running requests and sessions end. New requests
    wait until release (a held session's requests do not). Returns False when abort (the job was cancelled, or the
    app closes) ended the wait; the memory is then free again."""
    global job_peak, reload_after_job
    end = time.time() + JOB_WAIT
    with room:
        if wait_room(lambda: not waiting, abort, end) is None:
            return False
        job_peak = peak
        if fits_with(peak):
            return True
        drained = wait_room(lambda: active == 0 and not holding(), abort, end)
        if drained is None:
            job_peak = 0.0
            room.notify_all()
            return False
        for s in leases:  # after JOB_WAIT: a session's next request waits for the job
            s.held = False
        leases.clear()
        if worker is not None:
            reload_after_job = True
    stop(
        "Unloaded to free the memory for a media job."
        if drained
        else "Stopped: a media job could not wait longer for this answer."
    )
    return True


def release() -> None:
    """The media job ended: the waiting requests can run."""
    global job_peak
    with room:
        job_peak = 0.0
        room.notify_all()


def reload_after_jobs() -> bool:
    """The queue is empty: load the model again in the background when a media job unloaded it and its last request
    is within the idle time (it would still be loaded without the job). The next chat answer then starts at once
    instead of after the load. A job queued during the load unloads it again. True when a load started."""
    global reload_after_job
    limit = idle_stop()
    with lock:
        go = (
            reload_after_job
            and not closed
            and worker is None
            and job_peak == 0.0
            and (limit is None or time.time() - last_used < limit)
        )
        if (
            job_peak == 0.0
        ):  # decided: a job that still holds the memory leaves it for later
            reload_after_job = False
    if go:
        threading.Thread(target=preload, daemon=True).start()
    return go


def preload() -> None:
    """Load the model without a request; a failure shows on the next request."""
    try:
        start(background=True)
    except LLMError:
        pass


def stop_if_idle() -> bool:
    """Unload the model when it has had no request for idle_stop(); True when it did."""
    global worker
    limit = idle_stop()
    with lock:  # check and detach together, so a new request cannot slip in between
        w = worker
        idle = (
            limit is not None
            and w is not None
            and w.ready.is_set()  # still loading: not idle
            and not w.pending
            and not holding()  # a turn between its steps
            and time.time() - last_used > limit
        )
        if idle:
            worker = None
    if idle:
        w.stop("Unloaded after being idle.")
    return idle


def watch_idle() -> None:
    """Check for an idle model every 30 s (a daemon thread the server starts)."""
    while True:
        time.sleep(30)
        stop_if_idle()
