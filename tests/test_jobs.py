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

from app import config, llm, models, server, tools
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
def test_run_job_names_the_cause_of_a_failed_ffmpeg_step(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ffmpeg step shows ffmpeg's cause line without its [name @ 0x...] prefix, from that step's output
    only; a failed model keeps the exit code message (its last lines are mostly progress)."""
    fake = (
        tmp_path / "bin" / "ffmpeg"
    )  # writes what ffmpeg 9 writes when -c:v copy meets ProRes in an mp4
    fake.parent.mkdir()
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stderr.write('[mp4 @ 0x7cf0c0c500] Could not find tag for codec prores in stream #0, codec not "
        "currently supported in container\\n[out#0/mp4 @ 0x7cf1024180] Could not write header (incorrect codec "
        "parameters ?): Invalid argument\\n')\nsys.exit(234)\n"
    )
    fake.chmod(0o755)
    model = [sys.executable, "-c", "print('Error: an earlier step that worked')"]
    job_dir = state / "j1"
    job_dir.mkdir()

    def plan(*args: Any) -> tools.Plan:
        return tools.Plan([model, [str(fake), "-i", "in.mov", "out.mp4"]], {})

    monkeypatch.setattr(tools, "plan", plan)
    job = {"id": "j1", "tool": "edit.mix", "params": {}, "status": "running"}
    server.jobs["j1"] = job
    server.run_job(job)
    assert job["status"] == "failed"
    assert job["error"] == (
        "ffmpeg failed (exit code 234): Could not find tag for codec prores in stream #0, codec not currently "
        "supported in container"
    )
    # a model step: the message stays
    assert (
        server.step_failed(model, 3, job_dir / "job.log", 0)
        == "The model process exited with code 3. See the log."
    )
    # an ffmpeg step with no output of its own
    log = job_dir / "job.log"
    assert (
        server.step_failed([str(fake)], 1, log, log.stat().st_size)
        == "ffmpeg failed (exit code 1). See the log."
    )


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
    assert job["error"] == "The file could not be read as a video."  # no exception name
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


# The light lane: a job that loads no model runs beside the model job


def test_only_the_tools_without_a_model_take_the_light_lane() -> None:
    """The edit tools are plain ffmpeg. Every other tool loads a model, voice.transcribe too (Qwen3-ASR, about 3 GB)."""
    light = {
        t["id"] for t in tools.TOOLS if server.lane_of({"tool": t["id"]}) == "light"
    }
    assert light == {"edit.mix", "edit.concat", "edit.subtitle"}
    assert not light & set(tools.REQUIRES)  # no model files to find


def lanes() -> list[threading.Thread]:
    """The two workers, as start() starts them."""
    workers = [
        threading.Thread(target=server.worker, args=(lane,), daemon=True)
        for lane in ("model", "light")
    ]
    for w in workers:
        w.start()
    server.wake.set()
    server.wake_light.set()
    return workers


def until(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while not check() and time.time() < end:
        time.sleep(0.02)
    return check()


def test_a_light_job_runs_beside_a_model_job_and_claims_no_memory(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(models, "mem_total_gb", lambda: 48.0)
    monkeypatch.setattr(llm, "reload_after_jobs", lambda: False)
    claims: list[float] = []
    real_claim = llm.claim

    def claim(peak: float, abort: Callable[[], bool] = lambda: False) -> bool:
        claims.append(peak)
        return real_claim(peak, abort)

    monkeypatch.setattr(llm, "claim", claim)
    video_ends = threading.Event()
    seen: dict[str, Any] = {}

    def fake_run(job: dict[str, Any]) -> None:
        if job["tool"] == "edit.mix":
            seen.update(video=server.jobs["video"]["status"], job_peak=llm.job_peak)
        else:
            video_ends.wait(10)  # a video job of several minutes
        job["status"] = "done"

    monkeypatch.setattr(server, "run_job", fake_run)
    make_job("video", "queued", tool="video.t2v", order=1)
    workers = lanes()
    assert until(lambda: server.jobs["video"]["status"] == "running")
    mix = make_job("mix", "queued", tool="edit.mix", order=2)
    server.wake_light.set()
    assert until(lambda: mix["status"] == "done")
    # the mix ran while the video job held the memory, and left its claim as it was
    assert seen == {"video": "running", "job_peak": 35.0}
    assert claims == [35.0]
    video_ends.set()
    assert until(lambda: server.jobs["video"]["status"] == "done")
    assert until(lambda: llm.job_peak == 0.0)
    server.shutdown()
    for w in workers:
        w.join(5)
        assert not w.is_alive()


def test_model_jobs_stay_one_at_a_time_and_in_their_order(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm, "claim", lambda peak, abort=lambda: False: True)
    monkeypatch.setattr(llm, "reload_after_jobs", lambda: False)
    guard = threading.Lock()
    now: list[dict[str, Any]] = []
    ran: list[str] = []
    most = {"model": 0, "light": 0, "all": 0}

    def fake_run(job: dict[str, Any]) -> None:
        with guard:
            now.append(job)
            ran.append(job["id"])
            for lane in ("model", "light"):
                n = sum(server.lane_of(j) == lane for j in now)
                most[lane] = max(most[lane], n)
            most["all"] = max(most["all"], len(now))
        time.sleep(0.3 if server.lane_of(job) == "model" else 0.05)
        with guard:
            now.remove(job)
        job["status"] = "done"

    monkeypatch.setattr(server, "run_job", fake_run)
    for i, (job_id, tool) in enumerate(
        (
            ("a", "image.klein"),
            ("mix1", "edit.mix"),
            ("b", "video.t2v"),
            ("mix2", "edit.mix"),
            ("c", "image.klein"),
        ),
        1,
    ):
        make_job(job_id, "queued", tool=tool, order=i)
    workers = lanes()
    assert until(lambda: all(j["status"] == "done" for j in server.jobs.values()), 10)
    server.shutdown()
    for w in workers:
        w.join(5)
    assert [j for j in ran if server.lane_of(server.jobs[j]) == "model"] == [
        "a",
        "b",
        "c",
    ]
    assert [j for j in ran if j.startswith("mix")] == ["mix1", "mix2"]
    assert most == {"model": 1, "light": 1, "all": 2}
    assert ran.index("mix2") < ran.index("b")  # the mixes did not wait for "a"


@macos_time
def test_cancel_stops_a_running_light_job(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tools, "plan", lambda *a: tools.Plan([["/bin/sleep", "30"]], {})
    )
    monkeypatch.setattr(llm, "claim", lambda *a: pytest.fail("a light job claimed"))
    mix = make_job("mix", "queued", tool="edit.mix")
    worker = threading.Thread(target=server.worker, args=("light",), daemon=True)
    worker.start()
    server.wake_light.set()
    assert until(lambda: "mix" in server.procs)
    pgid = server.procs["mix"].pid
    assert server.cancel_job("mix") == {"status": "cancelled"}
    assert until(lambda: not server.procs, server.KILL_AFTER + 10)
    assert not server.group_alive(pgid)
    assert (mix["status"], mix["error"]) == ("cancelled", "Cancelled.")
    assert worker.is_alive()  # the lane goes on
    server.stopping.set()
    server.wake_light.set()
    worker.join(5)


@macos_time
def test_shutdown_stops_the_light_job_and_the_model_job(
    state: Path, make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tools, "plan", lambda *a: tools.Plan([["/bin/sleep", "30"]], {})
    )
    monkeypatch.setattr(llm, "claim", lambda peak, abort=lambda: False: True)
    make_job("video", "queued", tool="video.t2v", order=1)
    make_job("mix", "queued", tool="edit.mix", order=2)
    workers = lanes()
    assert until(lambda: set(server.procs) == {"video", "mix"})
    pgids = [p.pid for p in server.procs.values()]
    server.shutdown()
    assert not any(server.group_alive(pgid) for pgid in pgids)
    for w in workers:
        w.join(5)
        assert not w.is_alive()
    for job_id in ("video", "mix"):
        job = saved(state, job_id)
        assert (job["status"], job["error"]) == ("cancelled", "The app was closed.")


def test_a_queued_light_job_does_not_hold_the_chat_model_reload_back(
    make_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloads: list[str] = []
    monkeypatch.setattr(llm, "claim", lambda peak, abort=None: True)
    monkeypatch.setattr(llm, "reload_after_jobs", lambda: reloads.append("reload"))
    monkeypatch.setattr(server, "run_job", lambda job: job.update(status="done"))
    monkeypatch.setattr(server, "ended", lambda job: None)
    make_job("a", "queued", order=1)
    make_job("mix", "queued", tool="edit.mix", order=2)
    server.run_next(server.jobs["a"])
    assert reloads == ["reload"]
