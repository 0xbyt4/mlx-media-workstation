"""The job queue without HTTP: history at start, output listing, run_job with harmless commands, the worker loop and
process-group stops. The only processes started are short Python one-liners and `sleep`."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import config, llm, server, tools
from tests.conftest import MacEffects

Job = Callable[..., dict[str, Any]]
macos_time = pytest.mark.skipif(
    sys.platform != "darwin", reason="run_job uses macOS /usr/bin/time -l"
)


def saved(state: Path, job_id: str) -> dict[str, Any]:
    return json.loads((state / job_id / "job.json").read_text())


# History at start


def test_load_jobs_cancels_unfinished_jobs(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(server, "group_alive", lambda pgid: pgid == 777)
    monkeypatch.setattr(server, "stop_group", stopped.append)
    for job_id, status, extra in (
        ("a", "queued", {}),
        ("b", "running", {"pgid": 777}),  # its model is still running
        ("c", "running", {"pgid": 778}),  # its model is gone
        ("d", "done", {}),
    ):
        (state / job_id).mkdir()
        job = {"id": job_id, "status": status, **extra}
        (state / job_id / "job.json").write_text(json.dumps(job))
    (state / "broken").mkdir()
    (state / "broken" / "job.json").write_text("{not json")
    for name, text in (
        ("empty", "{}"),
        ("list", "[]"),
        ("x", '{"id": "..", "status": "done"}'),  # an id that is not its folder
    ):
        (state / name).mkdir()
        (state / name / "job.json").write_text(text)
    (state / "app.log").write_text("log")

    server.load_jobs()

    assert sorted(server.jobs) == ["a", "b", "c", "d"]
    assert stopped == [777]
    for job_id in ("a", "b", "c"):
        job = saved(state, job_id)
        assert job["status"] == "cancelled"
        assert job["error"] == "The app was closed before this job finished."
        assert server.jobs[job_id]["status"] == "cancelled"
    assert saved(state, "d") == {"id": "d", "status": "done"}


# Output listing and log parsing


def test_outputs_lists_results_only(state: Path) -> None:
    job_dir = state / "j1"
    job_dir.mkdir()
    for name in (
        "image.png",
        "take1.wav",
        "video.MP4",
        "start.png",
        "person.png",
        "voice_padded.wav",
        "source.mp4",
        "tail.mp4",
        "extended_raw.mp4",
        "in_image_0.png",
        "in_video_0.bin",
        "job.json",
        "job.log",
        "notes.json",
    ):
        (job_dir / name).write_bytes(b"x")
    (job_dir / "ace_raw").mkdir()
    assert server.outputs(job_dir, {"take1.wav": "seed 5"}) == [
        {
            "url": "/files/j1/image.png",
            "kind": "image",
            "name": "image.png",
            "note": "",
        },
        {
            "url": "/files/j1/take1.wav",
            "kind": "audio",
            "name": "take1.wav",
            "note": "seed 5",
        },
        {
            "url": "/files/j1/video.MP4",
            "kind": "video",
            "name": "video.MP4",
            "note": "",
        },
    ]


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        ("", None),
        ("done\n", None),
        ("        23622320128  peak memory footprint\n", 22.0),
        (
            "  1073741824  peak memory footprint\n  32212254720  peak memory footprint\n",
            30.0,
        ),
    ],
)
def test_peak_gb(log: str, expected: float | None) -> None:
    assert server.peak_gb(log) == expected


# The job view


def test_view_of_a_running_job(state: Path, make_job: Job) -> None:
    started = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    make_job("j1", "running", started=started, pgid=1, files={"x": []})
    v = server.view(server.jobs["j1"])
    assert 29 <= v["elapsed"] <= 40
    assert v["position"] == 0
    assert "pgid" not in v and "files" not in v


def test_view_of_a_running_job_tails_its_log(state: Path, make_job: Job) -> None:
    make_job("j1", "running", started=None)
    assert server.view(server.jobs["j1"])["log_tail"] == ""  # no log yet
    lines = [f"line {i}" for i in range(40)]
    (state / "j1" / "job.log").write_bytes(
        ("\n".join(lines) + "\nbad \xff byte\n").encode("latin-1")
    )
    tail = server.view(server.jobs["j1"])["log_tail"].splitlines()
    assert tail == [*lines[-24:], "bad \ufffd byte"]


def test_live_tail_reads_only_the_end(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "TAIL_BYTES", 10)
    log = state / "job.log"
    log.write_text("first line\nab\ncd\n")
    assert server.live_tail(log) == "ab\ncd"  # the cut line is dropped
    log.write_text("ab\ncd\n")
    assert server.live_tail(log) == "ab\ncd"


TIME_REPORT = """       18.07 real         0.00 user         0.01 sys
             2572288  maximum resident set size
                   0  average shared memory size
                2609  page reclaims
                  10  page faults
            37225838  instructions retired
             1671552  peak memory footprint
"""


def test_tails_leave_out_the_time_reports(state: Path) -> None:
    """/usr/bin/time -l adds about 18 lines after each command; they must not push the model's error out of the
    25 lines a failed job shows. peak_gb still reads them from the whole log."""
    model = [f"Traceback line {i}" for i in range(24)] + ["ValueError: bad shape"]
    log = (
        "$ step one\nloaded\n"
        + TIME_REPORT
        + "$ step two\n"
        + "\n".join(model)
        + "\n"
        + TIME_REPORT
    )
    assert server.tail(log).splitlines() == model
    assert server.peak_gb(log) == 0.0  # 1671552 bytes
    path = state / "job.log"
    path.write_text("$ step one\nloaded\n" + TIME_REPORT + "$ step two\nworking 3/10\n")
    assert server.live_tail(path) == "$ step one\nloaded\n$ step two\nworking 3/10"
    # a long log whose read window starts inside a times line ("8.07 real" of "18.07 real"): that cut line is
    # dropped, and no line of the model with it
    rest = TIME_REPORT[TIME_REPORT.index("18.07") + 1 :]
    width = (server.TAIL_BYTES - len(rest)) // 5 - 1
    model = [f"step {i}/5 ".ljust(width, "z") for i in range(1, 6)]
    model[-1] += "z" * (server.TAIL_BYTES - len(rest) - 5 * (width + 1))
    path.write_text(
        "x" * 1000
        + "\n"
        + TIME_REPORT[: TIME_REPORT.index("18.07") + 1]
        + rest
        + "\n".join(model)
        + "\n"
    )
    assert path.read_bytes()[-server.TAIL_BYTES :].startswith(b"8.07 real")
    assert server.live_tail(path).splitlines() == model


def test_view_of_queued_and_finished_jobs(state: Path, make_job: Job) -> None:
    make_job("late", "queued", created="2026-11-02T10:00:05")
    make_job("early", "queued", created="2026-11-02T10:00:01")
    make_job("done", "done", elapsed=12.5, outputs=None)
    assert server.view(server.jobs["early"])["position"] == 1
    assert server.view(server.jobs["late"])["position"] == 2
    done = server.view(server.jobs["done"])
    assert (done["position"], done["elapsed"], done["outputs"]) == (0, 12.5, [])


# run_job with harmless commands instead of a model


@pytest.fixture
def run(state: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict[str, Any]]:
    """Runs a job whose plan is the given Python snippets (each one command, run in the job folder)."""

    def go(*snippets: str, status: str = "running") -> dict[str, Any]:
        job_dir = state / "j1"
        job_dir.mkdir()
        job = {"id": "j1", "tool": "image.klein", "params": {}, "status": status}
        commands = [[sys.executable, "-c", s] for s in snippets]

        def plan(tool: str, params: dict, files: dict, out: Path) -> tools.Plan:
            assert (tool, out) == ("image.klein", job_dir)
            return tools.Plan(commands, {"WORKSTATION_TEST": "1"}, cwd=job_dir)

        monkeypatch.setattr(tools, "plan", plan)
        server.jobs["j1"] = job
        server.run_job(job)
        return job

    return go


@macos_time
def test_run_job_success(run: Callable[..., dict[str, Any]], state: Path) -> None:
    job = run(
        "import os; assert os.environ['WORKSTATION_TEST'] == '1'; print('step one')",
        "from pathlib import Path; Path('image.png').write_bytes(b'png');"
        "Path('notes.json').write_text('{\"image.png\": \"seed 3\"}')",
    )
    assert job["status"] == "done"
    assert job["outputs"] == [
        {
            "url": "/files/j1/image.png",
            "kind": "image",
            "name": "image.png",
            "note": "seed 3",
        }
    ]
    assert job["peak_gb"] is not None
    assert "pgid" not in job
    assert job["finished"] and job["elapsed"] >= 0
    log = (state / "j1" / "job.log").read_text()
    assert log.startswith(f"$ {sys.executable} -c import os")
    assert "step one" in log
    assert "peak memory footprint" in log  # /usr/bin/time -l, which peak_gb reads
    # the tail leaves the time reports out, so the model's own lines fill it
    assert job["log_tail"].splitlines()[1] == "step one"
    assert job["log_tail"].splitlines()[-1].startswith(f"$ {sys.executable} -c from")
    assert "real" not in job["log_tail"] and "footprint" not in job["log_tail"]
    assert saved(state, "j1")["status"] == "done"
    assert server.procs == {}


@macos_time
def test_run_job_stops_at_a_failed_command(
    run: Callable[..., dict[str, Any]], state: Path
) -> None:
    job = run(
        "raise SystemExit(3)",
        "from pathlib import Path; Path('image.png').write_bytes(b'png')",
    )
    assert job["status"] == "failed"
    assert job["error"] == "The model process exited with code 3. See the log."
    assert not (state / "j1" / "image.png").exists()
    assert saved(state, "j1")["status"] == "failed"


@macos_time
def test_run_job_without_output(run: Callable[..., dict[str, Any]]) -> None:
    job = run("pass")
    assert job["status"] == "failed"
    assert job["error"] == "The model finished but wrote no output."


def test_run_job_bad_input(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def plan(*args: Any) -> tools.Plan:
        raise ValueError("The file could not be read as a video.")

    monkeypatch.setattr(tools, "plan", plan)
    (state / "j1").mkdir()
    job = {"id": "j1", "tool": "video.extend", "params": {}, "status": "running"}
    server.run_job(job)
    assert job["status"] == "failed"
    assert job["error"] == "ValueError: The file could not be read as a video."
    assert (job["log_tail"], job["peak_gb"]) == ("", None)
    assert saved(state, "j1")["status"] == "failed"


def test_run_job_cancelled_before_start(
    run: Callable[..., dict[str, Any]], state: Path
) -> None:
    job = run(
        "from pathlib import Path; Path('image.png').write_bytes(b'png')",
        status="cancelled",
    )
    assert job["status"] == "cancelled"
    assert job.get("error") is None
    assert not (state / "j1" / "image.png").exists()


# The worker loop and shutdown


def test_worker_runs_queued_jobs_until_shutdown(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    ran: list[str] = []
    stopped: list[int] = []

    def fake_run(job: dict[str, Any]) -> None:
        ran.append(job["id"])
        started.set()

    monkeypatch.setattr(server, "run_job", fake_run)
    monkeypatch.setattr(server, "stop_group", stopped.append)
    make_job("late", "queued", created="2026-11-02T10:00:05")
    make_job("early", "queued", created="2026-11-02T10:00:01")
    worker = threading.Thread(target=server.worker, daemon=True)
    worker.start()
    server.wake.set()
    assert started.wait(5)
    early = saved(state, "early")
    assert early["status"] == "running" and early["started"]

    server.procs["late"] = SimpleNamespace(pid=999)
    server.shutdown()
    worker.join(5)
    assert not worker.is_alive()
    assert ran[0] == "early"
    assert stopped == [999]
    for job_id in ("early", "late"):  # the worker may have started "late" too
        job = saved(state, job_id)
        assert (job["status"], job["error"]) == ("cancelled", "The app was closed.")


def test_a_failed_job_json_write_fails_the_job_and_the_queue_goes_on(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "plan", lambda *a: tools.Plan([["/usr/bin/true"]], {}))
    monkeypatch.setattr(llm, "claim", lambda peak, abort=lambda: False: True)
    real_save = server.save

    def save(job: dict[str, Any]) -> None:
        if job["id"] == "full" and job.get(
            "finished"
        ):  # the last write of the job: the disk is full
            raise OSError(28, "No space left on device")
        real_save(job)

    monkeypatch.setattr(server, "save", save)
    full = make_job("full", "queued", order=1)
    make_job("next", "queued", order=2)
    worker = threading.Thread(target=server.worker, daemon=True)
    worker.start()
    server.wake.set()
    deadline = time.time() + 10
    while (
        server.jobs["next"]["status"] in ("queued", "running")
        and time.time() < deadline
    ):
        time.sleep(0.05)
    assert worker.is_alive()
    assert (
        server.jobs["next"]["status"] == "failed"
    )  # /usr/bin/true writes no output; it ran all the same
    assert full["status"] == "failed"
    server.stopping.set()
    server.wake.set()
    worker.join(5)


def test_a_model_left_running_by_a_failed_write_is_stopped(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tools, "plan", lambda *a: tools.Plan([["/bin/sleep", "30"]], {})
    )
    monkeypatch.setattr(llm, "claim", lambda peak, abort=lambda: False: True)
    started: list[int] = []

    def save(job: dict[str, Any]) -> None:
        if job.get("pgid") or job.get(
            "finished"
        ):  # the disk is full from the model's start on
            started.append(job.get("pgid") or 0)
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(server, "save", save)
    job = make_job("full", "queued")
    worker = threading.Thread(target=server.worker, daemon=True)
    worker.start()
    server.wake.set()
    deadline = time.time() + 20
    while (job["status"] != "failed" or server.procs) and time.time() < deadline:
        time.sleep(0.05)
    assert job["status"] == "failed" and not server.procs
    # the worker stops the group after it took the job out of procs (SIGTERM, then SIGKILL after KILL_AFTER for a
    # child that a fork hid from the SIGTERM): look once it is done
    server.stopping.set()
    server.wake.set()
    worker.join(server.KILL_AFTER + 10)
    assert not worker.is_alive()
    assert not server.group_alive(started[0])  # the model's process group is gone


def test_worker_takes_the_new_first_job_after_a_move_during_its_claim(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    started = threading.Event()
    claims: list[str] = []

    def claim(peak: float, abort: Callable[[], bool] = lambda: False) -> bool:
        claims.append(server.queued_jobs()[0]["id"])
        if (
            len(claims) == 1
        ):  # the user moves "b" to the top while "a" waits for its memory
            server.move_job("b", server.Move(to="top"))
        return True

    def fake_run(job: dict[str, Any]) -> None:
        ran.append(job["id"])
        job["status"] = "done"
        started.set()

    monkeypatch.setattr(llm, "claim", claim)
    monkeypatch.setattr(server, "run_job", fake_run)
    make_job("a", "queued", order=1)
    make_job("b", "queued", order=2)
    worker = threading.Thread(target=server.worker, daemon=True)
    worker.start()
    server.wake.set()
    assert started.wait(5)
    server.shutdown()
    worker.join(5)
    assert claims[:2] == ["a", "b"]
    assert ran[0] == "b"


@pytest.mark.parametrize(
    ("status", "elapsed", "note"),
    [
        ("done", 125.4, "FLUX.3 Klein is done. It took 2:05."),
        ("failed", 3.0, "FLUX.3 Klein failed. Open Jobs to see why."),
        ("cancelled", 3.0, None),
    ],
)
def test_ended_notifies_done_and_failed_jobs(
    make_job: Job,
    mac_effects: MacEffects,
    status: str,
    elapsed: float,
    note: str | None,
) -> None:
    job = make_job("j1", status, elapsed=elapsed)
    server.ended(job)
    assert mac_effects.notes == ([(note, "Workstation")] if note else [])
    assert mac_effects.awake.calls == [False]  # the queue is empty: the Mac may sleep


def test_ended_without_notifications(
    make_job: Job, mac_effects: MacEffects, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        config, "APP", config.App(**{**config.APP_DEFAULTS, "notify": False})
    )
    server.ended(make_job("j1", "done", elapsed=1.0))
    assert mac_effects.notes == []


def test_ended_keeps_awake_while_jobs_wait(
    make_job: Job, mac_effects: MacEffects
) -> None:
    make_job("next", "queued")
    server.ended(make_job("j1", "done", elapsed=1.0))
    assert mac_effects.awake.held


def test_worker_notifies_and_releases_after_the_last_job(
    state: Path, make_job: Job, mac_effects: MacEffects, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(job: dict[str, Any]) -> None:
        job["status"], job["elapsed"] = "done", 61.0

    monkeypatch.setattr(server, "run_job", fake_run)
    make_job("j1", "queued")
    worker = threading.Thread(target=server.worker, daemon=True)
    worker.start()
    server.wake.set()
    end = time.time() + 5
    while not mac_effects.notes and time.time() < end:
        time.sleep(0.02)
    server.shutdown()
    worker.join(5)
    assert mac_effects.notes == [("FLUX.3 Klein is done. It took 1:01.", "Workstation")]
    assert not mac_effects.awake.held


def test_shutdown_leaves_finished_jobs(state: Path, make_job: Job) -> None:
    make_job("j1", "done")
    make_job("j2", "queued")
    server.shutdown()
    assert server.stopping.is_set()
    assert saved(state, "j1")["status"] == "done"
    assert saved(state, "j2")["status"] == "cancelled"


# Process groups


def test_stop_group_ends_a_real_process_group() -> None:
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    reaper = threading.Thread(target=proc.wait, daemon=True)  # the worker's role
    reaper.start()
    t0 = time.monotonic()
    server.stop_group(proc.pid, timeout=5)
    reaper.join(5)
    assert proc.returncode == -signal.SIGTERM
    assert time.monotonic() - t0 < 5
    assert not server.group_alive(proc.pid)


def test_stop_group_of_a_gone_group(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(server.os, "killpg", killpg)
    server.stop_group(123)
    assert sent == [(123, signal.SIGTERM)]


@pytest.mark.parametrize(
    ("error", "alive"),
    [(None, True), (ProcessLookupError, False), (PermissionError, False)],
)
def test_group_alive(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception] | None, alive: bool
) -> None:
    def killpg(pgid: int, sig: int) -> None:
        assert sig == 0
        if error:
            raise error

    monkeypatch.setattr(server.os, "killpg", killpg)
    assert server.group_alive(os.getpid()) is alive


def test_the_chat_model_reload_waits_for_an_empty_queue(
    make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloads: list[str] = []

    def fake_run(job: dict[str, Any]) -> None:
        job["status"] = "done"

    monkeypatch.setattr(llm, "claim", lambda peak, abort=None: True)
    monkeypatch.setattr(llm, "reload_after_jobs", lambda: reloads.append("reload"))
    monkeypatch.setattr(server, "run_job", fake_run)
    monkeypatch.setattr(server, "ended", lambda job: None)
    make_job("a", "queued", order=1)
    make_job("b", "queued", order=2)
    server.run_next(server.jobs["a"])
    assert reloads == []  # "b" still waits
    server.run_next(server.jobs["b"])
    assert reloads == ["reload"]
