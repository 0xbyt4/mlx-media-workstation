"""Shared fixtures: fixed model paths, a stand-in for ffmpeg and ffprobe, and a server on a temp outputs folder.

No test runs a model or touches the network, starts caffeinate or shows a notification (mac_effects). The server's worker thread is never started (only start() starts it),
and the client fixture replaces run_job, so a job that a test creates stays queued.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config, llm, macos, server, tools

MODELS = Path("/models")


@pytest.fixture(autouse=True)
def model_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed model paths, so the command lines do not depend on this Mac's config.toml."""
    fixed = {
        "MFLUX": MODELS / "mflux" / "bin",
        "LTX": MODELS / "ltx" / "bin" / "ltx-2-mlx",
        "LTX_PY": MODELS / "ltx" / "bin" / "python",
        "LTX_MODEL": MODELS / "ltx-2.5",
        "DRAMA_PY": MODELS / "drama" / "bin" / "python",
        "DRAMA_MODEL": MODELS / "drama-3",
        "ACE_DIR": MODELS / "ace",
        "ACE_PY": MODELS / "ace" / ".venv" / "bin" / "python",
    }
    for name, path in fixed.items():
        monkeypatch.setattr(tools, name, path)


class FakeAwake:
    """Stands in for macos.Awake: records each sync's answer instead of starting caffeinate."""

    def __init__(self) -> None:
        self.held = False
        self.calls: list[bool] = []

    def sync(self, want: Callable[[], bool]) -> bool:
        self.held = want()
        self.calls.append(self.held)
        return self.held

    def release(self) -> None:
        self.held = False
        self.calls.append(False)


class MacEffects:
    def __init__(self) -> None:
        self.awake = FakeAwake()
        self.notes: list[tuple[str, str]] = []

    def notify(self, text: str, title: str = "Workstation") -> None:
        self.notes.append((text, title))


@pytest.fixture(autouse=True)
def mac_effects(monkeypatch: pytest.MonkeyPatch) -> MacEffects:
    """The default [app] settings, a fake keep-awake and recorded notifications."""
    fake = MacEffects()
    monkeypatch.setattr(config, "APP", config.App(**config.APP_DEFAULTS))
    monkeypatch.setattr(server, "awake", fake.awake)
    monkeypatch.setattr(macos, "notify", fake.notify)
    return fake


class FakeMedia:
    """Stands in for ffmpeg and ffprobe: canned probe answers by file name, and every prep command it got."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self.size = (704, 1280)
        self.frames = 121
        self.audio: dict[str, bool] = {}
        self.lufs: dict[str, float | None] = {}
        self.prepped: list[list[str]] = []

    def media_seconds(self, path: Path) -> float:
        return self.seconds[path.name]

    def video_size(self, path: Path) -> tuple[int, int]:
        return self.size

    def frame_count(self, path: Path) -> int:
        return self.frames

    def has_audio(self, path: Path) -> bool:
        return self.audio.get(path.name, True)

    def loudness(self, path: Path) -> float | None:
        return self.lufs.get(path.name)

    def prep(self, cmd: list[str]) -> None:
        self.prepped.append(cmd)


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch) -> FakeMedia:
    fake = FakeMedia()
    for name in (
        "media_seconds",
        "video_size",
        "frame_count",
        "has_audio",
        "loudness",
        "prep",
    ):
        monkeypatch.setattr(tools, name, getattr(fake, name))
    return fake


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty outputs folder and fresh server state (job list, processes, events)."""
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(server, "OUT", out)
    monkeypatch.setattr(server, "jobs", {})
    monkeypatch.setattr(server, "procs", {})
    monkeypatch.setattr(server, "stopping", threading.Event())
    monkeypatch.setattr(server, "wake", threading.Event())
    monkeypatch.setattr(server, "claiming", set())
    monkeypatch.setattr(llm, "closed", False)  # shutdown() closes the LLM for good
    return out


@pytest.fixture
def client(state: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app on 127.0.0.1:7870 with every model counted as installed; running a job fails the test."""

    def no_run(job: dict[str, Any]) -> None:
        raise AssertionError(f"a test started job {job['id']}")

    monkeypatch.setattr(server, "run_job", no_run)
    monkeypatch.setattr(tools, "REQUIRES", {})
    return TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")


@pytest.fixture
def make_job(state: Path) -> Callable[..., dict[str, Any]]:
    """Adds a job to the list and writes its folder and job.json."""

    def make(job_id: str, status: str, **extra: Any) -> dict[str, Any]:
        job = {
            "id": job_id,
            "tool": "image.klein",
            "tool_label": "FLUX.3 Klein",
            "status": status,
            "created": "2026-11-02T10:00:00",
            "started": None,
            "finished": None,
            "elapsed": None,
            "params": {"prompt": "a lighthouse"},
            "files": {},
            "outputs": [],
            "peak_gb": None,
            "error": None,
            "log_tail": "",
            **extra,
        }
        (state / job_id).mkdir()
        (state / job_id / "job.json").write_text(json.dumps(job))
        server.jobs[job_id] = job
        return job

    return make
